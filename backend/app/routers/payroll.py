from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_payroll_read_scope
from app.db.session import get_session
from app.payroll.service import preview
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api", tags=["payroll"])


class LineItemOut(BaseModel):
    code: str
    category: str
    formula: str
    amount: Decimal


class PreviewOut(BaseModel):
    employee_id: int
    period: str
    rule_version: str
    actual_attendance_days: Decimal
    statutory_holiday_days: Decimal
    statutory_holiday_worked_days: Decimal
    lines: list[LineItemOut]
    gross: Decimal
    deposit: Decimal
    net: Decimal
    carry_forward: Decimal
    deferred_deductions: Decimal
    deferred_deposit: Decimal
    exceptions: list[str]
    warnings: list[str]
    has_error: bool


@router.get("/employees/{employee_id}/payroll-preview", response_model=PreviewOut)
def payroll_preview(
    employee_id: int,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> PreviewOut:
    # Payroll-review assignment is the authoritative boundary for this
    # sensitive endpoint.  Do not accidentally require a redundant org-tree
    # assignment that could disagree with an explicit (store, department) grant.
    emp = EmployeeRepository(session, org_scope=None).get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    read_scope = resolve_payroll_read_scope(session, principal)
    if read_scope is not None and (emp.org_unit_id, emp.department) not in read_scope:
        # Do not disclose a salary preview outside the explicit assignment.
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    r = preview(session, emp, period)
    response = PreviewOut(
        employee_id=r.employee_id,
        period=r.period,
        rule_version=r.rule_version,
        actual_attendance_days=r.actual_attendance_days,
        statutory_holiday_days=r.statutory_holiday_days,
        statutory_holiday_worked_days=r.statutory_holiday_worked_days,
        lines=[
            LineItemOut(code=li.code, category=li.category, formula=li.formula, amount=li.amount)
            for li in r.lines
        ],
        gross=r.gross,
        deposit=r.deposit,
        net=r.net,
        carry_forward=r.carry_forward,
        deferred_deductions=r.deferred_deductions,
        deferred_deposit=r.deferred_deposit,
        exceptions=r.exceptions,
        warnings=r.warnings,
        has_error=r.has_error,
    )
    audit.record(
        session,
        action="payroll.preview.view",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=emp.id,
        detail={"period": period, "has_error": r.has_error},
    )
    session.commit()
    return response
