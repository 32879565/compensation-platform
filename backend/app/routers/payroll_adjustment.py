"""Permission- and scope-controlled monthly payroll adjustment sources."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.core.urls import require_http_url
from app.db.session import get_session
from app.models.employee import Employee
from app.models.payroll_adjustment import (
    MonthlyPayrollAdjustment,
    MonthlyPayrollAdjustmentRevision,
    PayrollAdjustmentType,
)
from app.models.payroll_batch import PayrollBatch
from app.models.payroll_result import AdjustmentRecord, PayrollResult
from app.payroll.guards import PayrollSourceLockedError, assert_period_mutable
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api/payroll-adjustments", tags=["payroll-adjustments"])

_PERIOD = r"^\d{4}-\d{2}$"


def _validate_calendar_period(period: str) -> None:
    try:
        year, month = (int(value) for value in period.split("-"))
        date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422,
            detail="Period must be a valid calendar month.",
        ) from exc


class MonthlyAdjustmentBody(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    reason: str = Field(min_length=1, max_length=2000)
    attachment_url: str = Field(min_length=1, max_length=512)
    taxable: bool
    in_social_base: bool
    in_housing_base: bool

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_required_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("attachment_url", mode="before")
    @classmethod
    def validate_attachment_url(cls, value: object) -> object:
        if isinstance(value, str):
            return require_http_url(value)
        return value


class MonthlyAdjustmentOut(BaseModel):
    id: int
    employee_id: int
    org_unit_id: int
    period: str
    adjustment_type: PayrollAdjustmentType
    amount: Decimal
    reason: str
    attachment_url: str
    taxable: bool | None
    in_social_base: bool | None
    in_housing_base: bool | None
    created_by: int
    updated_by: int
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class MonthlyAdjustmentRevisionOut(BaseModel):
    id: int
    adjustment_id: int
    revision: int
    employee_id: int
    org_unit_id: int
    period: str
    adjustment_type: PayrollAdjustmentType
    amount: Decimal
    reason: str
    attachment_url: str
    taxable: bool | None
    in_social_base: bool | None
    in_housing_base: bool | None
    changed_by: int
    created_at: datetime

    model_config = {"from_attributes": True}


def _period_mutable_or_error(
    session: Session, principal: Principal, period: str
) -> PayrollBatch | None:
    try:
        correction_round = assert_period_mutable(session, period)
    except PayrollSourceLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if correction_round and not principal.has_permission(Perm.PAYROLL_CORRECT):
        raise HTTPException(
            status_code=403,
            detail="Only HR payroll correction may change a reopened payroll source.",
        )
    if not correction_round:
        return None
    batch = session.scalars(
        select(PayrollBatch).where(PayrollBatch.period == period).with_for_update()
    ).one()
    return batch


def _visible_employee(
    session: Session,
    principal: Principal,
    permission: str,
    employee_id: int,
) -> Employee:
    scope = resolve_permission_org_scope(session, principal, permission)
    employee = EmployeeRepository(session, org_scope=scope).get(employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee not found or outside scope.")
    return employee


@router.get("", response_model=list[MonthlyAdjustmentOut])
def list_monthly_adjustments(
    period: str = Query(..., pattern=_PERIOD),
    employee_id: int | None = Query(default=None, gt=0),
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> list[MonthlyPayrollAdjustment]:
    _validate_calendar_period(period)
    scope = resolve_permission_org_scope(session, principal, Perm.PAYROLL_CORRECT)
    stmt = select(MonthlyPayrollAdjustment).where(MonthlyPayrollAdjustment.period == period)
    if employee_id is not None:
        stmt = stmt.where(MonthlyPayrollAdjustment.employee_id == employee_id)
    if scope is not None:
        stmt = stmt.where(MonthlyPayrollAdjustment.org_unit_id.in_(scope))
    records = list(
        session.scalars(
            stmt.order_by(
                MonthlyPayrollAdjustment.employee_id,
                MonthlyPayrollAdjustment.adjustment_type,
            )
        ).all()
    )
    audit.record(
        session,
        action="payroll_adjustment.view",
        actor=(principal.user_id, principal.username),
        target_type="employee" if employee_id is not None else "payroll_period",
        target_id=employee_id,
        detail={"period": period, "employee_filter": employee_id, "returned_count": len(records)},
    )
    session.commit()
    return records


@router.get(
    "/{employee_id}/{period}/{adjustment_type}/history",
    response_model=list[MonthlyAdjustmentRevisionOut],
)
def list_monthly_adjustment_history(
    employee_id: int = Path(gt=0),
    period: str = Path(pattern=_PERIOD),
    adjustment_type: PayrollAdjustmentType = Path(),
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> list[MonthlyPayrollAdjustmentRevision]:
    """Return immutable snapshots authorized by each revision's historical org."""

    _validate_calendar_period(period)
    scope = resolve_permission_org_scope(session, principal, Perm.PAYROLL_CORRECT)
    stmt = select(MonthlyPayrollAdjustmentRevision).where(
        MonthlyPayrollAdjustmentRevision.employee_id == employee_id,
        MonthlyPayrollAdjustmentRevision.period == period,
        MonthlyPayrollAdjustmentRevision.adjustment_type == adjustment_type,
    )
    if scope is not None:
        stmt = stmt.where(MonthlyPayrollAdjustmentRevision.org_unit_id.in_(scope))
    records = list(session.scalars(stmt.order_by(MonthlyPayrollAdjustmentRevision.revision)).all())
    audit.record(
        session,
        action="payroll_adjustment.history.view",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={
            "period": period,
            "adjustment_type": adjustment_type.value,
            "returned_count": len(records),
        },
    )
    session.commit()
    return records


def _append_revision(
    session: Session,
    record: MonthlyPayrollAdjustment,
    *,
    changed_by: int,
) -> MonthlyPayrollAdjustmentRevision:
    latest_revision = session.scalar(
        select(func.max(MonthlyPayrollAdjustmentRevision.revision)).where(
            MonthlyPayrollAdjustmentRevision.adjustment_id == record.id
        )
    )
    revision = MonthlyPayrollAdjustmentRevision(
        adjustment_id=record.id,
        revision=int(latest_revision or 0) + 1,
        employee_id=record.employee_id,
        org_unit_id=record.org_unit_id,
        period=record.period,
        adjustment_type=record.adjustment_type,
        amount=record.amount,
        reason=record.reason,
        attachment_url=record.attachment_url,
        taxable=record.taxable,
        in_social_base=record.in_social_base,
        in_housing_base=record.in_housing_base,
        changed_by=changed_by,
    )
    session.add(revision)
    return revision


@router.put(
    "/{employee_id}/{period}/{adjustment_type}",
    response_model=MonthlyAdjustmentOut,
)
def upsert_monthly_adjustment(
    body: MonthlyAdjustmentBody,
    employee_id: int = Path(gt=0),
    period: str = Path(pattern=_PERIOD),
    adjustment_type: PayrollAdjustmentType = Path(),
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> MonthlyPayrollAdjustment:
    _validate_calendar_period(period)
    correction_batch = _period_mutable_or_error(session, principal, period)
    employee = _visible_employee(
        session,
        principal,
        Perm.PAYROLL_CORRECT,
        employee_id,
    )
    if correction_batch is not None:
        prior_result_id = session.scalar(
            select(PayrollResult.id)
            .where(
                PayrollResult.batch_id == correction_batch.id,
                PayrollResult.employee_id == employee.id,
                PayrollResult.batch_version < correction_batch.version,
            )
            .limit(1)
        )
        if prior_result_id is None:
            raise HTTPException(
                status_code=409,
                detail="The reopened payroll batch did not previously include this employee.",
            )
    record = session.scalars(
        select(MonthlyPayrollAdjustment)
        .where(
            MonthlyPayrollAdjustment.employee_id == employee.id,
            MonthlyPayrollAdjustment.period == period,
            MonthlyPayrollAdjustment.adjustment_type == adjustment_type,
        )
        .with_for_update()
    ).first()
    if record is None:
        before: dict[str, object] = {"record_exists": False}
        record = MonthlyPayrollAdjustment(
            employee_id=employee.id,
            org_unit_id=employee.org_unit_id,
            period=period,
            adjustment_type=adjustment_type,
            amount=body.amount,
            reason=body.reason,
            attachment_url=body.attachment_url,
            taxable=body.taxable,
            in_social_base=body.in_social_base,
            in_housing_base=body.in_housing_base,
            created_by=principal.user_id,
            updated_by=principal.user_id,
        )
        session.add(record)
        action = "payroll_adjustment.create"
    else:
        write_scope = resolve_permission_org_scope(session, principal, Perm.PAYROLL_CORRECT)
        if write_scope is not None and record.org_unit_id not in write_scope:
            raise HTTPException(status_code=404, detail="Payroll adjustment source not found.")
        if correction_batch is not None and (
            record.amount,
            record.taxable,
            record.in_social_base,
            record.in_housing_base,
        ) == (
            body.amount,
            body.taxable,
            body.in_social_base,
            body.in_housing_base,
        ):
            raise HTTPException(
                status_code=422,
                detail="A reopened payroll correction must change the payroll amount.",
            )
        before = {
            "record_exists": True,
            "amount": str(record.amount),
            "reason": record.reason,
            "attachment_url": record.attachment_url,
            "taxable": record.taxable,
            "in_social_base": record.in_social_base,
            "in_housing_base": record.in_housing_base,
        }
        record.amount = body.amount
        record.reason = body.reason
        record.attachment_url = body.attachment_url
        record.taxable = body.taxable
        record.in_social_base = body.in_social_base
        record.in_housing_base = body.in_housing_base
        record.updated_by = principal.user_id
        action = "payroll_adjustment.update"
    try:
        # Flush the natural-key row first so a create has an adjustment_id;
        # the row and its immutable snapshot still commit atomically.
        session.flush()
        _append_revision(session, record, changed_by=principal.user_id)
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="The payroll adjustment was changed concurrently; reload and retry.",
        ) from None
    after = {
        "record_exists": True,
        "amount": str(record.amount),
        "reason": record.reason,
        "attachment_url": record.attachment_url,
        "taxable": record.taxable,
        "in_social_base": record.in_social_base,
        "in_housing_base": record.in_housing_base,
    }
    if correction_batch is not None:
        session.add(
            AdjustmentRecord(
                batch_id=correction_batch.id,
                batch_version=correction_batch.version,
                employee_id=employee.id,
                dispute_id=None,
                item=f"{adjustment_type.value}_SOURCE",
                before_value=before,
                after_value=after,
                reason=body.reason,
                applicant_id=principal.user_id,
                approver_id=principal.user_id,
                attachment_url=body.attachment_url,
                recompute_result={
                    "status": "PENDING_RERUN",
                    "batch_version": correction_batch.version,
                },
            )
        )
    audit.record(
        session,
        action=action,
        actor=(principal.user_id, principal.username),
        target_type="monthly_payroll_adjustment",
        target_id=record.id,
        detail={
            "employee_id": employee.id,
            "period": period,
            "adjustment_type": adjustment_type.value,
            "before": before,
            "after": after,
        },
    )
    session.commit()
    return record
