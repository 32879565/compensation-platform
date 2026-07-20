from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.auth.deps import principal_scope, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.payroll.service import preview
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api", tags=["payroll"])


class LineItemOut(BaseModel):
    code: str
    component_type: str
    input_amount: Decimal
    formula: str
    amount: Decimal


class PreviewOut(BaseModel):
    employee_id: int
    period: str
    rule_version: str
    lines: list[LineItemOut]
    gross: Decimal
    exceptions: list[str]
    has_error: bool


@router.get("/employees/{employee_id}/payroll-preview", response_model=PreviewOut)
def payroll_preview(
    employee_id: int,
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> PreviewOut:
    emp = EmployeeRepository(session, org_scope=principal_scope(principal)).get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    result = preview(session, emp, period)
    return PreviewOut(
        employee_id=result.employee_id,
        period=result.period,
        rule_version=result.rule_version,
        lines=[
            LineItemOut(
                code=li.code,
                component_type=li.component_type.value,
                input_amount=li.input_amount,
                formula=li.formula,
                amount=li.amount,
            )
            for li in result.lines
        ],
        gross=result.gross,
        exceptions=result.exceptions,
        has_error=result.has_error,
    )
