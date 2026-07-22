from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.db.session import get_session
from app.dingtalk.org_freshness import (
    invalidate_reviewer_authorization,
    lock_reviewer_authorization_users,
    reviewer_authorization_user_ids,
)
from app.dingtalk.org_sync import take_organization_sync_lock
from app.models.employee import Employee, requires_approved_attendance_days
from app.models.grade import JobGrade
from app.models.org import OrgType, OrgUnit
from app.payroll.guards import (
    PayrollSourceLockedError,
    assert_employee_history_mutable,
    assert_new_employee_cohort_mutable,
)
from app.repositories.employee import EmployeeRepository
from app.repositories.org import OrgUnitRepository
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeOut,
    EmployeePage,
    EmployeeUpdate,
    validate_employee_lifecycle_dates,
)

router = APIRouter(prefix="/api/employees", tags=["employees"])

# Only these fields can change a current/future payroll calculation.  Identity,
# contact, and encrypted payment details remain maintainable after payroll has
# started because result snapshots already preserve historical inputs.
_PAYROLL_INPUT_FIELDS = frozenset(
    {
        "org_unit_id",
        "employment_type",
        "department",
        "position_title",
        "is_special_position",
        "status",
        "hire_date",
        "probation_end",
        "leave_date",
        "social_city",
    }
)


def _lock_employee_org_units(session: Session, org_unit_ids: set[int]) -> dict[int, OrgUnit]:
    rows = session.scalars(
        select(OrgUnit)
        .where(OrgUnit.id.in_(org_unit_ids))
        .order_by(OrgUnit.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    return {row.id: row for row in rows}


def _repo(session: Session, principal: Principal, permission: str) -> EmployeeRepository:
    return EmployeeRepository(
        session,
        org_scope=resolve_permission_org_scope(session, principal, permission),
    )


def _pii_scope(session: Session, principal: Principal) -> frozenset[int] | None:
    """Return the scope that may receive unmasked employee identifiers.

    Employee read and PII access are intentionally separate permissions.  A
    principal can hold a global employee-read role alongside a locally scoped
    PII role, so using ``Principal.org_scope`` or a role-union boolean here
    would disclose identities outside the PII grant.
    """
    if not principal.has_permission(Perm.EMPLOYEE_PII):
        return frozenset()
    return resolve_permission_org_scope(session, principal, Perm.EMPLOYEE_PII)


def _reveal_pii(org_unit_id: int, pii_scope: frozenset[int] | None) -> bool:
    return pii_scope is None or org_unit_id in pii_scope


def _require_pii_write(session: Session, principal: Principal, org_unit_id: int) -> None:
    """Require the dedicated PII grant within the target employee scope."""

    if not _reveal_pii(org_unit_id, _pii_scope(session, principal)):
        raise HTTPException(
            status_code=403,
            detail="Writing identity or bank PII requires employee:pii permission in scope.",
        )


def _visible_store_or_error(
    session: Session, org_scope: frozenset[int] | None, org_unit_id: int
) -> None:
    org_unit = OrgUnitRepository(session, org_scope=org_scope).get(org_unit_id)
    if org_unit is None:
        raise HTTPException(status_code=404, detail="所属组织不存在或不可见")
    if org_unit.type != OrgType.STORE:
        raise HTTPException(status_code=422, detail="员工必须归属有效门店组织")


def _visible_grade_or_error(session: Session, job_grade_id: int | None) -> None:
    """Lock and validate a grade that is about to receive a new assignment."""

    if job_grade_id is None:
        return
    grade = session.scalars(
        select(JobGrade)
        .where(JobGrade.id == job_grade_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()
    if grade is None:
        raise HTTPException(status_code=404, detail="Job grade does not exist.")
    if grade.is_deleted:
        raise HTTPException(status_code=409, detail="Inactive job grades cannot be assigned.")


def _validate_merged_lifecycle(
    *,
    hire_date: date | None,
    probation_end: date | None,
    leave_date: date | None,
) -> None:
    try:
        validate_employee_lifecycle_dates(
            hire_date=hire_date,
            probation_end=probation_end,
            leave_date=leave_date,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


def _is_employee_number_conflict(exc: IntegrityError) -> bool:
    diag = getattr(exc.orig, "diag", None)
    return getattr(diag, "constraint_name", None) == "ix_employee_emp_no"


def _ensure_employee_history_mutable(
    session: Session,
    principal: Principal,
    employee_id: int,
    *,
    hire_date: date | None,
    leave_date: date | None,
) -> bool:
    try:
        correction_round = assert_employee_history_mutable(
            session,
            employee_id,
            hire_date=hire_date,
            leave_date=leave_date,
        )
    except PayrollSourceLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if correction_round:
        # Employee master data has no period/effective-range correction payload,
        # so it cannot produce a safe before/after/recompute audit record here.
        # Keep the reopened correction path limited to audited attendance and
        # performance changes until the S8 effective-dated adjustment flow owns it.
        raise HTTPException(
            status_code=409,
            detail="已解锁批次的人员计薪信息请通过受审计的调薪流程更正",
        )
    return correction_round


@router.get("", response_model=EmployeePage)
def list_employees(
    name: str | None = None,
    emp_no: str | None = None,
    org_unit_id: int | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_READ)),
    session: Session = Depends(get_session),
) -> EmployeePage:
    result = _repo(session, principal, Perm.EMPLOYEE_READ).search(
        name=name, emp_no=emp_no, org_unit_id=org_unit_id, page=page, page_size=page_size
    )
    pii_scope = _pii_scope(session, principal)
    return EmployeePage(
        items=[
            EmployeeOut.from_employee(e, reveal_pii=_reveal_pii(e.org_unit_id, pii_scope))
            for e in result.items
        ],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.get("/{employee_id}", response_model=EmployeeOut)
def get_employee(
    employee_id: int,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_READ)),
    session: Session = Depends(get_session),
) -> EmployeeOut:
    emp = _repo(session, principal, Perm.EMPLOYEE_READ).get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    return EmployeeOut.from_employee(
        emp,
        reveal_pii=_reveal_pii(emp.org_unit_id, _pii_scope(session, principal)),
    )


@router.post("", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
def create_employee(
    body: EmployeeCreate,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_WRITE)),
    session: Session = Depends(get_session),
) -> EmployeeOut:
    # A newly created employee may join the cohort of a concurrent payroll run
    # or backfill an already-calculated cohort.  Both paths share the payroll
    # source lock and reject historical omissions outside the audit workflow.
    try:
        assert_new_employee_cohort_mutable(session, body.hire_date)
    except PayrollSourceLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    org_scope = resolve_permission_org_scope(session, principal, Perm.EMPLOYEE_WRITE)
    _visible_store_or_error(session, org_scope, body.org_unit_id)
    if body.id_card is not None or body.bank_account is not None:
        _require_pii_write(session, principal, body.org_unit_id)
    take_organization_sync_lock(session)
    _visible_grade_or_error(session, body.job_grade_id)
    locked_store = _lock_employee_org_units(session, {body.org_unit_id}).get(body.org_unit_id)
    if locked_store is None or locked_store.is_deleted or locked_store.type != OrgType.STORE:
        raise HTTPException(status_code=409, detail="所属门店已被其他操作修改，请刷新后重试")
    emp = Employee(
        emp_no=body.emp_no,
        name=body.name,
        org_unit_id=body.org_unit_id,
        job_grade_id=body.job_grade_id,
        employment_type=body.employment_type,
        department=body.department,
        position_title=body.position_title,
        is_special_position=(
            body.is_special_position or requires_approved_attendance_days(body.position_title)
        ),
        hire_date=body.hire_date,
        probation_end=body.probation_end,
        leave_date=body.leave_date,
        social_city=body.social_city,
        id_card=body.id_card,
        bank_account=body.bank_account,
    )
    session.add(emp)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if not _is_employee_number_conflict(exc):
            raise
        raise HTTPException(status_code=409, detail="工号已存在") from None
    audit.record(
        session,
        action="employee.create",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=emp.id,
        detail={
            "emp_no": emp.emp_no,
            "org_unit_id": emp.org_unit_id,
            "job_grade_id": emp.job_grade_id,
        },
    )
    session.commit()
    return EmployeeOut.from_employee(
        emp,
        reveal_pii=_reveal_pii(emp.org_unit_id, _pii_scope(session, principal)),
    )


@router.patch("/{employee_id}", response_model=EmployeeOut)
def update_employee(
    employee_id: int,
    body: EmployeeUpdate,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_WRITE)),
    session: Session = Depends(get_session),
) -> EmployeeOut:
    write_scope = resolve_permission_org_scope(session, principal, Perm.EMPLOYEE_WRITE)
    statement = select(Employee).where(
        Employee.id == employee_id,
        Employee.is_deleted.is_(False),
    )
    if write_scope is not None:
        statement = statement.where(Employee.org_unit_id.in_(write_scope))
    emp = session.scalars(statement.execution_options(populate_existing=True)).first()
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    observed_version = emp.version
    data = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    target_position_title = data.get("position_title", emp.position_title)
    if requires_approved_attendance_days(target_position_title):
        data["is_special_position"] = True
    target_org_unit_id = data.get("org_unit_id") or emp.org_unit_id
    previous_job_grade_id = emp.job_grade_id
    grade_assignment_changed = "job_grade_id" in data and data["job_grade_id"] != emp.job_grade_id
    if grade_assignment_changed and body.expected_version is None:
        raise HTTPException(
            status_code=422,
            detail="Job grade assignment requires expected_version.",
        )
    if body.expected_version is not None and emp.version != body.expected_version:
        raise HTTPException(
            status_code=409,
            detail="Employee changed by another user; refresh and retry.",
        )
    _validate_merged_lifecycle(
        hire_date=data.get("hire_date", emp.hire_date),
        probation_end=data.get("probation_end", emp.probation_end),
        leave_date=data.get("leave_date", emp.leave_date),
    )
    if {"id_card", "bank_account"}.intersection(data):
        _require_pii_write(session, principal, target_org_unit_id)
    if _PAYROLL_INPUT_FIELDS.intersection(data):
        _ensure_employee_history_mutable(
            session,
            principal,
            emp.id,
            hire_date=data.get("hire_date", emp.hire_date),
            leave_date=data.get("leave_date", emp.leave_date),
        )
    # 转移组织时，目标组织也必须在可见范围内（防越权把员工移出/移入不可见组织）
    if "org_unit_id" in data and data["org_unit_id"] is not None:
        _visible_store_or_error(
            session,
            resolve_permission_org_scope(session, principal, Perm.EMPLOYEE_WRITE),
            data["org_unit_id"],
        )
    routing_changed = any(
        field in data and data[field] != getattr(emp, field) for field in ("org_unit_id", "status")
    )
    take_organization_sync_lock(session)
    refreshed_emp = session.scalars(statement.execution_options(populate_existing=True)).first()
    if refreshed_emp is None or refreshed_emp.version != observed_version:
        raise HTTPException(status_code=409, detail="员工已被其他操作修改，请刷新后重试")
    emp = refreshed_emp
    # Grade is a current master-data assignment used by compa analysis, not a
    # payroll calculation input.  Validate only actual reassignment and do not
    # make promotions impossible merely because the employee has prior payroll.
    if grade_assignment_changed:
        _visible_grade_or_error(session, data["job_grade_id"])
    invalidated_proof_count = 0
    targeted_session_user_count = 0
    affected_user_ids = (
        reviewer_authorization_user_ids(session, employee_ids={emp.id}) if routing_changed else ()
    )
    lock_reviewer_authorization_users(session, affected_user_ids)
    locked_orgs = _lock_employee_org_units(
        session,
        {emp.org_unit_id, target_org_unit_id},
    )
    if set(locked_orgs) != {emp.org_unit_id, target_org_unit_id}:
        raise HTTPException(status_code=409, detail="员工组织已被其他操作修改，请刷新后重试")
    locked_emp = session.scalars(
        statement.with_for_update().execution_options(populate_existing=True)
    ).first()
    if locked_emp is None or locked_emp.version != observed_version:
        raise HTTPException(status_code=409, detail="员工已被其他操作修改，请刷新后重试")
    emp = locked_emp
    if routing_changed:
        invalidation = invalidate_reviewer_authorization(
            session,
            employee_ids={emp.id},
            locked_user_ids=affected_user_ids,
        )
        invalidated_proof_count = invalidation.invalidated_proof_count
        targeted_session_user_count = invalidation.targeted_session_user_count
    for field, value in data.items():
        setattr(emp, field, value)
    previous_version = emp.version
    emp.version += 1
    session.flush()
    audit_detail: dict[str, object] = {
        "changed": sorted(data.keys()),
        "from_version": previous_version,
        "to_version": emp.version,
        "invalidated_sync_proof_count": invalidated_proof_count,
        "targeted_session_user_count": targeted_session_user_count,
    }
    if grade_assignment_changed:
        audit_detail["job_grade_assignment"] = {
            "before_grade_id": previous_job_grade_id,
            "after_grade_id": emp.job_grade_id,
        }
    audit.record(
        session,
        action="employee.update",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=emp.id,
        detail=audit_detail,
    )
    session.commit()
    return EmployeeOut.from_employee(
        emp,
        reveal_pii=_reveal_pii(emp.org_unit_id, _pii_scope(session, principal)),
    )


@router.delete("/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_employee(
    employee_id: int,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_WRITE)),
    session: Session = Depends(get_session),
) -> None:
    write_scope = resolve_permission_org_scope(session, principal, Perm.EMPLOYEE_WRITE)
    statement = select(Employee).where(
        Employee.id == employee_id,
        Employee.is_deleted.is_(False),
    )
    if write_scope is not None:
        statement = statement.where(Employee.org_unit_id.in_(write_scope))
    emp = session.scalars(statement.execution_options(populate_existing=True)).first()
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    observed_version = emp.version
    _ensure_employee_history_mutable(
        session,
        principal,
        emp.id,
        hire_date=emp.hire_date,
        leave_date=emp.leave_date,
    )
    take_organization_sync_lock(session)
    refreshed_emp = session.scalars(statement.execution_options(populate_existing=True)).first()
    if refreshed_emp is None or refreshed_emp.version != observed_version:
        raise HTTPException(status_code=409, detail="员工已被其他操作修改，请刷新后重试")
    emp = refreshed_emp
    affected_user_ids = reviewer_authorization_user_ids(session, employee_ids={emp.id})
    lock_reviewer_authorization_users(session, affected_user_ids)
    locked_store = _lock_employee_org_units(session, {emp.org_unit_id}).get(emp.org_unit_id)
    if locked_store is None or locked_store.is_deleted:
        raise HTTPException(status_code=409, detail="员工组织已被其他操作修改，请刷新后重试")
    locked_emp = session.scalars(
        statement.with_for_update().execution_options(populate_existing=True)
    ).first()
    if locked_emp is None or locked_emp.version != observed_version:
        raise HTTPException(status_code=409, detail="员工已被其他操作修改，请刷新后重试")
    emp = locked_emp
    invalidation = invalidate_reviewer_authorization(
        session,
        employee_ids={emp.id},
        locked_user_ids=affected_user_ids,
    )
    EmployeeRepository(session, org_scope=write_scope).soft_delete(emp)
    audit.record(
        session,
        action="employee.delete",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={
            "invalidated_sync_proof_count": invalidation.invalidated_proof_count,
            "targeted_session_user_count": invalidation.targeted_session_user_count,
        },
    )
    session.commit()
