from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import principal_scope, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.employee import Employee
from app.repositories.employee import EmployeeRepository
from app.repositories.org import OrgUnitRepository
from app.schemas.employee import (
    EmployeeCreate,
    EmployeeOut,
    EmployeePage,
    EmployeeUpdate,
)

router = APIRouter(prefix="/api/employees", tags=["employees"])


def _repo(session: Session, principal: Principal) -> EmployeeRepository:
    return EmployeeRepository(session, org_scope=principal_scope(principal))


def _org_visible(session: Session, principal: Principal, org_unit_id: int) -> bool:
    return (
        OrgUnitRepository(session, org_scope=principal_scope(principal)).get(org_unit_id)
        is not None
    )


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
    result = _repo(session, principal).search(
        name=name, emp_no=emp_no, org_unit_id=org_unit_id, page=page, page_size=page_size
    )
    reveal = principal.has_permission(Perm.EMPLOYEE_PII)
    return EmployeePage(
        items=[EmployeeOut.from_employee(e, reveal_pii=reveal) for e in result.items],
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
    emp = _repo(session, principal).get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    return EmployeeOut.from_employee(emp, reveal_pii=principal.has_permission(Perm.EMPLOYEE_PII))


@router.post("", response_model=EmployeeOut, status_code=status.HTTP_201_CREATED)
def create_employee(
    body: EmployeeCreate,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_WRITE)),
    session: Session = Depends(get_session),
) -> EmployeeOut:
    if not _org_visible(session, principal, body.org_unit_id):
        raise HTTPException(status_code=404, detail="所属组织不存在或不可见")
    emp = Employee(
        emp_no=body.emp_no,
        name=body.name,
        org_unit_id=body.org_unit_id,
        job_grade_id=body.job_grade_id,
        employment_type=body.employment_type,
        hire_date=body.hire_date,
        probation_end=body.probation_end,
        social_city=body.social_city,
        id_card=body.id_card,
        bank_account=body.bank_account,
    )
    session.add(emp)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="工号已存在") from None
    audit.record(
        session,
        action="employee.create",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=emp.id,
        detail={"emp_no": emp.emp_no, "org_unit_id": emp.org_unit_id},
    )
    session.commit()
    return EmployeeOut.from_employee(emp, reveal_pii=principal.has_permission(Perm.EMPLOYEE_PII))


@router.patch("/{employee_id}", response_model=EmployeeOut)
def update_employee(
    employee_id: int,
    body: EmployeeUpdate,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_WRITE)),
    session: Session = Depends(get_session),
) -> EmployeeOut:
    repo = _repo(session, principal)
    emp = repo.get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    data = body.model_dump(exclude_unset=True)
    # 转移组织时，目标组织也必须在可见范围内（防越权把员工移出/移入不可见组织）
    if "org_unit_id" in data and data["org_unit_id"] is not None:
        if not _org_visible(session, principal, data["org_unit_id"]):
            raise HTTPException(status_code=404, detail="目标组织不存在或不可见")
    for field, value in data.items():
        setattr(emp, field, value)
    session.flush()
    audit.record(
        session,
        action="employee.update",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=emp.id,
        detail={"changed": sorted(data.keys())},
    )
    session.commit()
    return EmployeeOut.from_employee(emp, reveal_pii=principal.has_permission(Perm.EMPLOYEE_PII))


@router.delete("/{employee_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_employee(
    employee_id: int,
    principal: Principal = Depends(require_permission(Perm.EMPLOYEE_WRITE)),
    session: Session = Depends(get_session),
) -> None:
    repo = _repo(session, principal)
    emp = repo.get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    repo.soft_delete(emp)
    audit.record(
        session,
        action="employee.delete",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
    )
    session.commit()
