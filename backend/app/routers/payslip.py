"""Employee self-service access to finalized payslips."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ValidationError, field_serializer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.core.decimal import decimal_text
from app.db.session import get_session
from app.models.auth import User
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import PayrollResult

router = APIRouter(prefix="/api/payslips", tags=["payslips"])


class PayslipLineOut(BaseModel):
    code: str
    category: str
    formula: str
    amount: Decimal

    @field_serializer("amount")
    def serialize_amount(self, value: Decimal) -> str:
        return decimal_text(value)


class PayslipPeriodOut(BaseModel):
    period: str
    locked_at: datetime | None


class PayslipOut(BaseModel):
    period: str
    locked_at: datetime | None
    actual_attendance_days: Decimal
    gross: Decimal
    deposit: Decimal
    net: Decimal
    carry_forward: Decimal
    rule_version: str
    lines: list[PayslipLineOut]
    warnings: list[str]

    @field_serializer("actual_attendance_days", "gross", "deposit", "net", "carry_forward")
    def serialize_decimal(self, value: Decimal) -> str:
        return decimal_text(value)


def _current_employee_id(session: Session, principal: Principal) -> int:
    """Resolve only the authenticated user's employee record.

    This deliberately does not use an organization-scoped employee repository:
    employee self-service is bound to the durable ``app_user.employee_id``
    relationship, never to a caller-supplied employee id.
    """

    user = session.get(User, principal.user_id)
    if user is None or user.is_deleted or user.employee_id is None:
        raise HTTPException(status_code=404, detail="当前账号未绑定员工档案")
    return user.employee_id


def _locked_result_statement(employee_id: int, period: str | None = None):
    statement = (
        select(PayrollResult, PayrollBatch.period, PayrollBatch.locked_at)
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .where(
            PayrollResult.employee_id == employee_id,
            PayrollBatch.status == BatchStatus.LOCKED,
            PayrollResult.batch_version == PayrollBatch.version,
        )
        .order_by(PayrollBatch.period.desc(), PayrollResult.version.desc())
    )
    if period is not None:
        statement = statement.where(PayrollBatch.period == period)
    return statement


def _payslip_lines(result: PayrollResult) -> list[PayslipLineOut]:
    try:
        return [PayslipLineOut.model_validate(line) for line in result.lines]
    except (TypeError, ValidationError):
        # A finalized result with malformed persisted lines must not be
        # displayed as a partial/ambiguous payslip.
        raise HTTPException(
            status_code=409, detail="工资单明细数据无效，请联系薪酬管理员"
        ) from None


@router.get("/me/periods", response_model=list[PayslipPeriodOut])
def list_my_payslip_periods(
    principal: Principal = Depends(require_permission(Perm.PAYSLIP_READ_SELF)),
    session: Session = Depends(get_session),
) -> list[PayslipPeriodOut]:
    employee_id = _current_employee_id(session, principal)
    # A payslip history can be long. Select only the two list fields and let
    # the database collapse historical employee-result revisions instead of
    # loading every JSON input snapshot and line-item payload into Python.
    period_rows = session.execute(
        select(PayrollBatch.period, PayrollBatch.locked_at)
        .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
        .where(
            PayrollResult.employee_id == employee_id,
            PayrollBatch.status == BatchStatus.LOCKED,
            PayrollResult.batch_version == PayrollBatch.version,
        )
        .distinct()
        .order_by(PayrollBatch.period.desc())
    ).all()
    periods = [
        PayslipPeriodOut(period=period, locked_at=locked_at) for period, locked_at in period_rows
    ]

    audit.record(
        session,
        action="payslip.periods.view",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={"count": len(periods)},
    )
    session.commit()
    return periods


@router.get("/me", response_model=PayslipOut)
def get_my_payslip(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.PAYSLIP_READ_SELF)),
    session: Session = Depends(get_session),
) -> PayslipOut:
    employee_id = _current_employee_id(session, principal)
    row = session.execute(_locked_result_statement(employee_id, period).limit(1)).first()
    if row is None:
        # Do not distinguish a missing payroll result from an unlocked batch.
        raise HTTPException(status_code=404, detail="该周期暂无可查看的已锁定工资单")
    result, _batch_period, locked_at = row
    lines = _payslip_lines(result)

    audit.record(
        session,
        action="payslip.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_result",
        target_id=result.id,
        detail={"period": period, "batch_id": result.batch_id, "result_version": result.version},
    )
    session.commit()
    return PayslipOut(
        period=period,
        locked_at=locked_at,
        actual_attendance_days=result.actual_attendance_days,
        gross=result.gross,
        deposit=result.deposit,
        net=result.net,
        carry_forward=result.carry_forward,
        rule_version=result.rule_version,
        lines=lines,
        warnings=[str(warning) for warning in result.warnings],
    )
