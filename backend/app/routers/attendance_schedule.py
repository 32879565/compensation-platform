"""HR configuration and generation endpoint for expected attendance days."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.attendance.schedule import (
    ExpectedDaysError,
    load_active_rules,
    period_bounds,
    resolve_expected_days,
)
from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.db.session import get_session
from app.models.attendance import AttendanceRecord, ExpectedAttendanceRule
from app.models.employee import Department, Employee, EmploymentType
from app.payroll.guards import PayrollSourceLockedError, assert_period_mutable
from app.repositories.org import OrgUnitRepository

router = APIRouter(prefix="/api/attendance-schedules", tags=["attendance-schedules"])

_PERIOD = r"^\d{4}-\d{2}$"


class ScheduleRuleBody(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    org_unit_id: int | None = None
    employment_type: EmploymentType | None = None
    department: Department | None = None
    position_title: str | None = Field(default=None, max_length=64)
    is_special_position: bool | None = None
    weekly_rest_days: list[int] = Field(default_factory=list)
    monthly_expected_days: Decimal | None = Field(
        default=None, gt=0, le=31, max_digits=6, decimal_places=2
    )
    effective_from: date
    effective_to: date | None = None
    priority: int = Field(default=0, ge=-1000, le=1000)
    is_active: bool = True

    @field_validator("name", "position_title", mode="before")
    @classmethod
    def strip_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("weekly_rest_days")
    @classmethod
    def validate_rest_days(cls, value: list[int]) -> list[int]:
        if any(day < 0 or day > 6 for day in value) or len(set(value)) != len(value):
            raise ValueError("每周休息日必须是不重复的 0-6（0=周一）")
        return value

    @model_validator(mode="after")
    def validate_schedule(self) -> ScheduleRuleBody:
        if not self.name:
            raise ValueError("规则名称不能为空")
        if not self.weekly_rest_days and self.monthly_expected_days is None:
            raise ValueError("必须配置每周休息日或固定月应出勤天数")
        if self.effective_to is not None and self.effective_to <= self.effective_from:
            raise ValueError("失效日期必须晚于生效日期")
        return self


class ScheduleRuleOut(ScheduleRuleBody):
    id: int

    model_config = {"from_attributes": True}


class GenerateOut(BaseModel):
    period: str
    generated: int
    adjusted_preserved: int


def _assert_org_visible(session: Session, principal: Principal, org_unit_id: int | None) -> None:
    scope = resolve_permission_org_scope(session, principal, Perm.ATTENDANCE_SCHEDULE_WRITE)
    if org_unit_id is None:
        if scope is not None:
            raise HTTPException(
                status_code=403,
                detail="Scoped schedule writers cannot create global attendance rules",
            )
        return
    if OrgUnitRepository(session, org_scope=scope).get(org_unit_id) is None:
        raise HTTPException(status_code=404, detail="组织不存在或不可见")


def _assert_rule_writable(
    principal: Principal, session: Session, rule: ExpectedAttendanceRule
) -> None:
    scope = resolve_permission_org_scope(session, principal, Perm.ATTENDANCE_SCHEDULE_WRITE)
    if scope is not None and (rule.org_unit_id is None or rule.org_unit_id not in scope):
        raise HTTPException(status_code=404, detail="应出勤规则不存在")


def _period_mutable_or_error(session: Session, period: str) -> None:
    try:
        correction_round = assert_period_mutable(session, period)
    except PayrollSourceLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if correction_round:
        raise HTTPException(
            status_code=409,
            detail=(
                "Bulk schedule generation is not allowed in a reopened payroll round; "
                "use the audited employee attendance correction workflow"
            ),
        )


@router.get("", response_model=list[ScheduleRuleOut])
def list_schedule_rules(
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_SCHEDULE_READ)),
    session: Session = Depends(get_session),
) -> list[ExpectedAttendanceRule]:
    scope = resolve_permission_org_scope(session, principal, Perm.ATTENDANCE_SCHEDULE_READ)
    if scope is not None and not scope:
        return []
    statement = select(ExpectedAttendanceRule)
    if scope is not None:
        statement = statement.where(
            or_(
                ExpectedAttendanceRule.org_unit_id.is_(None),
                ExpectedAttendanceRule.org_unit_id.in_(scope),
            )
        )
    return list(session.scalars(statement.order_by(ExpectedAttendanceRule.id)).all())


@router.post("", response_model=ScheduleRuleOut, status_code=status.HTTP_201_CREATED)
def create_schedule_rule(
    body: ScheduleRuleBody,
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_SCHEDULE_WRITE)),
    session: Session = Depends(get_session),
) -> ExpectedAttendanceRule:
    _assert_org_visible(session, principal, body.org_unit_id)
    rule = ExpectedAttendanceRule(**body.model_dump())
    session.add(rule)
    session.flush()
    audit.record(
        session,
        action="attendance_schedule.create",
        actor=(principal.user_id, principal.username),
        target_type="expected_attendance_rule",
        target_id=rule.id,
        detail={"name": rule.name, "effective_from": rule.effective_from.isoformat()},
    )
    session.commit()
    return rule


@router.put("/{rule_id}", response_model=ScheduleRuleOut)
def replace_schedule_rule(
    rule_id: int,
    body: ScheduleRuleBody,
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_SCHEDULE_WRITE)),
    session: Session = Depends(get_session),
) -> ExpectedAttendanceRule:
    rule = session.get(ExpectedAttendanceRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="应出勤规则不存在")
    _assert_rule_writable(principal, session, rule)
    _assert_org_visible(session, principal, body.org_unit_id)
    before = {"name": rule.name, "effective_from": rule.effective_from.isoformat()}
    for field, value in body.model_dump().items():
        setattr(rule, field, value)
    session.flush()
    audit.record(
        session,
        action="attendance_schedule.update",
        actor=(principal.user_id, principal.username),
        target_type="expected_attendance_rule",
        target_id=rule.id,
        detail={
            "before": before,
            "after": {
                "name": rule.name,
                "effective_from": rule.effective_from.isoformat(),
            },
        },
    )
    session.commit()
    return rule


@router.post("/generate", response_model=GenerateOut)
def generate_expected_days(
    period: str = Query(..., pattern=_PERIOD),
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_SCHEDULE_WRITE)),
    session: Session = Depends(get_session),
) -> GenerateOut:
    _period_mutable_or_error(session, period)
    try:
        period_start, period_end = period_bounds(period)
    except ExpectedDaysError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    scope = resolve_permission_org_scope(session, principal, Perm.ATTENDANCE_SCHEDULE_WRITE)
    employee_statement = select(Employee).where(
        Employee.is_deleted.is_(False),
        Employee.hire_date.is_not(None),
        Employee.hire_date <= period_end,
        or_(Employee.leave_date.is_(None), Employee.leave_date >= period_start),
    )
    if scope is not None:
        employee_statement = employee_statement.where(Employee.org_unit_id.in_(scope))
    employees = list(session.scalars(employee_statement).all())
    rules = load_active_rules(session, period)
    generated_by_employee: dict[int, tuple[int, Decimal]] = {}
    errors: list[str] = []
    for employee in employees:
        try:
            generated = resolve_expected_days(session, employee, period, rules=rules)
        except ExpectedDaysError as exc:
            errors.append(f"{employee.emp_no}: {exc}")
        else:
            generated_by_employee[employee.id] = (generated.rule_id, generated.days)
    if errors:
        raise HTTPException(status_code=422, detail={"message": "应出勤生成失败", "errors": errors})

    existing_by_employee = {
        record.employee_id: record
        for record in session.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id.in_(set(generated_by_employee) or {-1}),
                AttendanceRecord.period == period,
            )
        ).all()
    }
    adjusted_preserved = 0
    for employee in employees:
        rule_id, expected_days = generated_by_employee[employee.id]
        record = existing_by_employee.get(employee.id)
        before: dict[str, object]
        legacy_without_provenance = False
        if record is None:
            record = AttendanceRecord(
                employee_id=employee.id,
                period=period,
                generated_expected_days=expected_days,
                expected_days_rule_id=rule_id,
                expected_days=expected_days,
                actual_days=Decimal("0"),
            )
            session.add(record)
            before = {"record_exists": False}
        else:
            legacy_without_provenance = (
                record.generated_expected_days is None or record.expected_days_rule_id is None
            )
            before = {
                "generated_expected_days": (
                    str(record.generated_expected_days)
                    if record.generated_expected_days is not None
                    else None
                ),
                "expected_days": str(record.expected_days),
                "expected_days_adjust_reason": record.expected_days_adjust_reason,
            }
            record.generated_expected_days = expected_days
            record.expected_days_rule_id = rule_id
            # h6 added provenance after older attendance data existed.  A
            # legacy free-text reason was not protected by the newer dedicated
            # HR permission, so it cannot be treated as an approved exception.
            # Rebase it to the generated value; HR may then record a fresh,
            # separately audited exception through the current workflow.
            if legacy_without_provenance or record.expected_days_adjust_reason is None:
                record.expected_days = expected_days
                if legacy_without_provenance:
                    record.expected_days_adjust_reason = None
            else:
                adjusted_preserved += 1
        audit.record(
            session,
            action="attendance.expected_days.generate",
            actor=(principal.user_id, principal.username),
            target_type="employee",
            target_id=employee.id,
            detail={
                "period": period,
                "before": before,
                "generated_expected_days": str(expected_days),
                "rule_id": rule_id,
                "legacy_adjustment_reset": legacy_without_provenance,
            },
        )
    session.commit()
    return GenerateOut(
        period=period,
        generated=len(generated_by_employee),
        adjusted_preserved=adjusted_preserved,
    )
