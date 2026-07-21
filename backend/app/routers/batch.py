from __future__ import annotations

import calendar
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Annotated, Literal, NoReturn

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import func, select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, aliased

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import (
    Principal,
    resolve_payroll_read_scope,
    resolve_permission_org_scope,
    resolve_review_scope,
)
from app.core.config import DingTalkMode, Settings, get_settings
from app.core.urls import optional_http_url, require_http_url
from app.db.session import get_session
from app.dingtalk import service as dingtalk
from app.models.employee import Department, Employee
from app.models.payroll_batch import PayrollBatch
from app.models.payroll_result import (
    AdjustmentRecord,
    BatchConfirmation,
    CompDispute,
    DisputeEvent,
    DisputeStatus,
    PayrollResult,
)
from app.payroll.batch_service import (
    BatchError,
    allowed_attendance_fields,
    approve_batch,
    confirm_scope,
    dispute_correction_options,
    lock_batch,
    raise_dispute,
    reopen_batch,
    resolve_dispute,
    run_batch,
    supplement_dispute,
    unlock_batch,
)

router = APIRouter(prefix="/api/batches", tags=["batch"])


class BatchCreate(BaseModel):
    period: str = Field(pattern=r"^\d{4}-\d{2}$")
    attendance_start: date
    attendance_end: date

    @model_validator(mode="after")
    def validate_period_and_attendance_range(self) -> BatchCreate:
        year, month = (int(value) for value in self.period.split("-"))
        if not 1 <= month <= 12:
            raise ValueError("薪资月份必须是有效的 YYYY-MM")
        if self.attendance_start > self.attendance_end:
            raise ValueError("考勤开始日期不能晚于结束日期")
        expected_start = date(year, month, 1)
        expected_end = date(year, month, calendar.monthrange(year, month)[1])
        if (self.attendance_start, self.attendance_end) != (expected_start, expected_end):
            raise ValueError("当前核算规则仅支持薪资月份对应的完整自然月考勤区间")
        return self


class BatchOut(BaseModel):
    id: int
    period: str
    attendance_start: date
    attendance_end: date
    status: str
    calculation_status: str
    store_confirmation_status: str
    hr_review_status: str
    lock_status: str
    calculated_at: datetime | None
    hr_reviewed_by: int | None
    hr_reviewed_at: datetime | None
    locked_by: int | None
    locked_at: datetime | None
    version: int

    model_config = {"from_attributes": True}


def _batch_or_404(session: Session, batch_id: int) -> PayrollBatch:
    batch = session.get(PayrollBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


def _require_global_batch_lifecycle_permission(
    session: Session, principal: Principal, permission: str
) -> None:
    """Allow global batch transitions only to a global role granting ``permission``.

    Payroll batches span all organizations, so a scoped permission must never be
    applied to their lifecycle even when the caller has a matching org assignment.
    Resolve before loading a batch to keep existing and unknown batch identifiers
    indistinguishable to scoped callers.
    """
    if resolve_permission_org_scope(session, principal, permission) is not None:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Batch lifecycle operations require a global permission role.",
        )


def _correctable_dispute_or_404(
    session: Session, dispute_id: int, principal: Principal
) -> CompDispute:
    """Return a dispute only when payroll correction scope covers its result snapshot.

    The result's persisted organization is the historical payroll boundary.  The
    employee's current organization can change after a batch run and must not widen
    a correction grant.  Scope filtering happens in the lookup so an out-of-scope
    dispute is indistinguishable from a nonexistent one.
    """
    permission_scope = resolve_permission_org_scope(session, principal, Perm.PAYROLL_CORRECT)
    if permission_scope is None:
        dispute = session.get(CompDispute, dispute_id)
    elif not permission_scope:
        dispute = None
    else:
        latest_result = aliased(PayrollResult)
        latest_version = (
            select(func.max(latest_result.version))
            .where(
                latest_result.batch_id == CompDispute.batch_id,
                latest_result.batch_version == CompDispute.batch_version,
                latest_result.employee_id == CompDispute.employee_id,
            )
            .correlate(CompDispute)
            .scalar_subquery()
        )
        dispute = session.scalars(
            select(CompDispute)
            .join(
                PayrollResult,
                (PayrollResult.batch_id == CompDispute.batch_id)
                & (PayrollResult.batch_version == CompDispute.batch_version)
                & (PayrollResult.employee_id == CompDispute.employee_id)
                & (PayrollResult.version == latest_version),
            )
            .where(
                CompDispute.id == dispute_id,
                PayrollResult.org_unit_id.in_(list(permission_scope)),
            )
            .limit(1)
        ).first()
    if dispute is None:
        raise HTTPException(status_code=404, detail="Dispute not found")
    return dispute


def _reviewable_dispute_or_404(
    session: Session, dispute_id: int, principal: Principal
) -> CompDispute:
    """Resolve a dispute against the reviewer's historical store/department scope."""
    review_scope = resolve_review_scope(session, principal)
    if not review_scope:
        _raise_dispute_target_not_found()
    latest_result = aliased(PayrollResult)
    latest_version = (
        select(func.max(latest_result.version))
        .where(
            latest_result.batch_id == CompDispute.batch_id,
            latest_result.batch_version == CompDispute.batch_version,
            latest_result.employee_id == CompDispute.employee_id,
        )
        .correlate(CompDispute)
        .scalar_subquery()
    )
    dispute = session.scalars(
        select(CompDispute)
        .join(
            PayrollResult,
            (PayrollResult.batch_id == CompDispute.batch_id)
            & (PayrollResult.batch_version == CompDispute.batch_version)
            & (PayrollResult.employee_id == CompDispute.employee_id)
            & (PayrollResult.version == latest_version),
        )
        .where(
            CompDispute.id == dispute_id,
            tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(list(review_scope)),
        )
        .limit(1)
    ).first()
    if dispute is None:
        _raise_dispute_target_not_found()
    return dispute


def _visible_batch_or_404(session: Session, batch_id: int, principal: Principal) -> PayrollBatch:
    """Return a batch only when its result scope is visible to the caller."""
    batch = _batch_or_404(session, batch_id)
    read_scope = resolve_payroll_read_scope(session, principal)
    if read_scope is None:
        return batch
    if not read_scope:
        raise HTTPException(status_code=404, detail="批次不存在")
    visible = session.scalar(
        select(PayrollBatch.id)
        .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
        .where(
            PayrollBatch.id == batch_id,
            PayrollResult.batch_version == PayrollBatch.version,
            tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(list(read_scope)),
        )
        .limit(1)
    )
    if visible is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    return batch


def _review_scope_or_404(
    session: Session, principal: Principal, org_unit_id: int, department: Department
) -> None:
    """Enforce the specification's organization-and-department review boundary."""
    review_scope = resolve_review_scope(session, principal)
    if review_scope is not None and (org_unit_id, department) not in review_scope:
        # 404 avoids leaking other department/store payroll scope existence.
        raise HTTPException(status_code=404, detail="门店或部门不在可复核范围内")


def _apply_result_scope(stmt, scope):
    """Constrain a statement's ``PayrollResult`` rows to an optional result scope."""
    if scope is None:
        return stmt
    if not scope:
        # Empty IN clauses are backend-dependent; a sentinel org id is deterministic
        # and fail-closed.
        return stmt.where(PayrollResult.org_unit_id == -1)
    return stmt.where(tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(list(scope)))


def _apply_result_review_scope(stmt, session: Session, principal: Principal):
    """Restrict non-global payroll reads to explicitly assigned review scopes."""
    return _apply_result_scope(stmt, resolve_payroll_read_scope(session, principal))


def _raise_dispute_target_not_found() -> NoReturn:
    """Hide dispute target existence whenever its result is not reviewable."""
    raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispute target not found")


@router.post("", response_model=BatchOut, status_code=status.HTTP_201_CREATED)
def create_batch(
    body: BatchCreate,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_RUN)),
    session: Session = Depends(get_session),
) -> PayrollBatch:
    _require_global_batch_lifecycle_permission(session, principal, Perm.PAYROLL_RUN)
    batch = PayrollBatch(
        period=body.period,
        attendance_start=body.attendance_start,
        attendance_end=body.attendance_end,
    )
    session.add(batch)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="该月份批次已存在") from None
    audit.record(
        session,
        action="batch.create",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"period": batch.period},
    )
    session.commit()
    return batch


@router.get("", response_model=list[BatchOut])
def list_batches(
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> list[PayrollBatch]:
    """List only batches containing payroll results visible to the caller.

    A non-global reviewer without explicit department assignment receives an empty list,
    which is deliberately fail-closed.
    """
    stmt = select(PayrollBatch).order_by(PayrollBatch.period.desc())
    read_scope = resolve_payroll_read_scope(session, principal)
    if read_scope is not None and read_scope:
        stmt = (
            stmt.join(
                PayrollResult,
                (PayrollResult.batch_id == PayrollBatch.id)
                & (PayrollResult.batch_version == PayrollBatch.version),
            )
            .where(
                tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(list(read_scope))
            )
            .distinct()
        )
    response = (
        [] if read_scope is not None and not read_scope else list(session.scalars(stmt).all())
    )
    audit.record(
        session,
        action="batch.list.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        detail={"returned": len(response)},
    )
    session.commit()
    return response


@router.post("/{batch_id}/run", response_model=dict)
def run(
    batch_id: int,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_RUN)),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> dict:
    _require_global_batch_lifecycle_permission(session, principal, Perm.PAYROLL_RUN)
    batch = _batch_or_404(session, batch_id)
    try:
        count = run_batch(session, batch)
        notification_summary = dingtalk.stage_review_deliveries(
            session, batch_id=batch.id, settings=settings
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except dingtalk.DingTalkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.run",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"employees": count},
    )
    audit.record(
        session,
        action="dingtalk.review.stage",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={
            "sandbox": settings.dingtalk_mode is DingTalkMode.SANDBOX,
            "routed": notification_summary.routed,
            "configuration_failures": notification_summary.configuration_failures,
            "existing": notification_summary.existing,
        },
    )
    session.commit()
    if settings.dingtalk_mode is DingTalkMode.LIVE and notification_summary.pending_delivery_ids:
        background_tasks.add_task(
            dingtalk.dispatch_live_deliveries,
            notification_summary.pending_delivery_ids,
        )
    return {"employees": count, "status": batch.status.value}


class ResultOut(BaseModel):
    employee_id: int
    emp_no: str
    employee_name: str
    org_unit_id: int | None
    version: int
    batch_version: int
    department: str
    actual_attendance_days: Decimal
    statutory_holiday_days: Decimal
    statutory_holiday_worked_days: Decimal
    statutory_holiday_pay: Decimal
    gross: Decimal
    deposit: Decimal
    net: Decimal
    carry_forward: Decimal
    deferred_deductions: Decimal
    deferred_deposit: Decimal
    has_error: bool
    lines: list
    exceptions: list
    warnings: list
    rule_version: str


def _result_line_amount(lines: list, code: str) -> Decimal:
    """Project a named persisted calculation line into an explicit money field."""
    total = Decimal("0")
    for line in lines:
        if not isinstance(line, dict) or line.get("code") != code:
            continue
        try:
            amount = Decimal(str(line.get("amount", "0")))
        except (InvalidOperation, TypeError, ValueError):
            # Keep legacy/malformed line details visible without letting one
            # derived convenience field make the whole result unreadable.
            continue
        if amount.is_finite():
            total += amount
    return total.quantize(Decimal("0.01"))


@router.get("/{batch_id}/results", response_model=list[ResultOut])
def list_results(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> list[ResultOut]:
    batch = _visible_batch_or_404(session, batch_id, principal)
    stmt = (
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch_id,
            PayrollResult.batch_version == batch.version,
        )
        .order_by(PayrollResult.employee_id, PayrollResult.version.desc())
    )
    stmt = _apply_result_review_scope(stmt, session, principal)
    # 仅取每员工最新版本
    rows = session.scalars(stmt).all()
    latest: dict[int, PayrollResult] = {}
    for r in rows:
        if r.employee_id not in latest or r.version > latest[r.employee_id].version:
            latest[r.employee_id] = r
    response = [
        ResultOut(
            employee_id=result.employee_id,
            emp_no=result.emp_no_snapshot or f"[历史工号快照缺失:{result.employee_id}]",
            employee_name=result.employee_name_snapshot or "[历史姓名快照缺失]",
            org_unit_id=result.org_unit_id,
            version=result.version,
            batch_version=result.batch_version,
            department=result.department.value,
            actual_attendance_days=result.actual_attendance_days,
            statutory_holiday_days=result.statutory_holiday_days,
            statutory_holiday_worked_days=result.statutory_holiday_worked_days,
            statutory_holiday_pay=_result_line_amount(result.lines, "HOLIDAY"),
            gross=result.gross,
            deposit=result.deposit,
            net=result.net,
            carry_forward=result.carry_forward,
            deferred_deductions=result.deferred_deductions,
            deferred_deposit=result.deferred_deposit,
            has_error=result.has_error,
            lines=result.lines,
            exceptions=result.exceptions,
            warnings=result.warnings,
            rule_version=result.rule_version,
        )
        for result in latest.values()
    ]
    audit.record(
        session,
        action="batch.results.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"batch_version": batch.version, "returned": len(response)},
    )
    session.commit()
    return response


class AdjustmentOut(BaseModel):
    id: int
    batch_id: int
    batch_version: int
    is_current_version: bool
    employee_id: int
    dispute_id: int | None
    item: str
    before_value: dict
    after_value: dict
    reason: str
    applicant_id: int | None
    approver_id: int
    attachment_url: str | None
    recompute_result: dict | None
    created_at: datetime


@router.get("/{batch_id}/adjustments", response_model=list[AdjustmentOut])
def list_adjustments(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> list[AdjustmentOut]:
    """Return current and historical correction records within payroll read scope."""
    batch = _visible_batch_or_404(session, batch_id, principal)
    read_scope = resolve_payroll_read_scope(session, principal)
    statement = (
        select(AdjustmentRecord)
        .where(AdjustmentRecord.batch_id == batch_id)
        .order_by(AdjustmentRecord.created_at.desc(), AdjustmentRecord.id.desc())
    )
    if read_scope is not None:
        if not read_scope:
            records: list[AdjustmentRecord] = []
        else:
            latest_result = aliased(PayrollResult)
            latest_version = (
                select(func.max(latest_result.version))
                .where(
                    latest_result.batch_id == AdjustmentRecord.batch_id,
                    latest_result.batch_version == AdjustmentRecord.batch_version,
                    latest_result.employee_id == AdjustmentRecord.employee_id,
                )
                .correlate(AdjustmentRecord)
                .scalar_subquery()
            )
            scoped_statement = statement.join(
                PayrollResult,
                (PayrollResult.batch_id == AdjustmentRecord.batch_id)
                & (PayrollResult.batch_version == AdjustmentRecord.batch_version)
                & (PayrollResult.employee_id == AdjustmentRecord.employee_id)
                & (PayrollResult.version == latest_version),
            ).where(
                tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(list(read_scope))
            )
            records = list(session.scalars(scoped_statement).all())
    else:
        records = list(session.scalars(statement).all())

    response = [
        AdjustmentOut(
            id=record.id,
            batch_id=record.batch_id,
            batch_version=record.batch_version,
            is_current_version=record.batch_version == batch.version,
            employee_id=record.employee_id,
            dispute_id=record.dispute_id,
            item=record.item,
            before_value=record.before_value,
            after_value=record.after_value,
            reason=record.reason,
            applicant_id=record.applicant_id,
            approver_id=record.approver_id,
            attachment_url=record.attachment_url,
            recompute_result=record.recompute_result,
            created_at=record.created_at,
        )
        for record in records
    ]
    audit.record(
        session,
        action="batch.adjustments.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"batch_version": batch.version, "returned": len(response)},
    )
    session.commit()
    return response


class ConfirmationOut(BaseModel):
    org_unit_id: int
    department: str
    status: str
    confirmed_by: int | None
    confirmed_at: datetime | None

    model_config = {"from_attributes": True}


@router.get("/{batch_id}/confirmations", response_model=list[ConfirmationOut])
def list_confirmations(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> list[BatchConfirmation]:
    batch = _visible_batch_or_404(session, batch_id, principal)
    stmt = select(BatchConfirmation).where(
        BatchConfirmation.batch_id == batch_id,
        BatchConfirmation.batch_version == batch.version,
    )
    read_scope = resolve_payroll_read_scope(session, principal)
    if read_scope is not None and read_scope:
        stmt = stmt.where(
            tuple_(BatchConfirmation.org_unit_id, BatchConfirmation.department).in_(
                list(read_scope)
            )
        )
    response = (
        [] if read_scope is not None and not read_scope else list(session.scalars(stmt).all())
    )
    audit.record(
        session,
        action="batch.confirmations.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"batch_version": batch.version, "returned": len(response)},
    )
    session.commit()
    return response


class ConfirmBody(BaseModel):
    org_unit_id: int
    department: Department


@router.post("/{batch_id}/confirm", response_model=dict)
def confirm(
    batch_id: int,
    body: ConfirmBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_REVIEW)),
    session: Session = Depends(get_session),
) -> dict:
    batch = _visible_batch_or_404(session, batch_id, principal)
    _review_scope_or_404(session, principal, body.org_unit_id, body.department)
    try:
        conf = confirm_scope(session, batch, body.org_unit_id, body.department, principal.user_id)
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.confirm",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"org_unit_id": body.org_unit_id, "department": body.department.value},
    )
    session.commit()
    return {"status": conf.status.value, "batch_status": batch.status.value}


class DisputeBody(BaseModel):
    employee_id: int
    salary_item: str = Field(min_length=1, max_length=64)
    opinion: str = Field(min_length=1, max_length=1000)

    @field_validator("opinion")
    @classmethod
    def require_nonblank_opinion(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("异议意见不能为空")
        return value


class DisputeEventOut(BaseModel):
    id: int
    event_type: str
    note: str
    actor_id: int
    attachment_url: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class DisputeOut(BaseModel):
    id: int
    employee_id: int
    org_unit_id: int | None
    department: str
    salary_item: str
    opinion: str
    raised_by: int
    status: str
    resolution: str | None
    resolved_by: int | None
    resolved_at: datetime | None
    created_at: datetime
    allowed_attendance_fields: list[str]
    correction_options: list[dict[str, object]]
    events: list[DisputeEventOut]


@router.get("/{batch_id}/disputes", response_model=list[DisputeOut])
def list_disputes(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_READ)),
    session: Session = Depends(get_session),
) -> list[DisputeOut]:
    batch = _visible_batch_or_404(session, batch_id, principal)
    read_scope = resolve_payroll_read_scope(session, principal)
    response: list[DisputeOut] = []
    if read_scope is None or read_scope:
        latest_result = aliased(PayrollResult)
        latest_version = (
            select(func.max(latest_result.version))
            .where(
                latest_result.batch_id == CompDispute.batch_id,
                latest_result.batch_version == CompDispute.batch_version,
                latest_result.employee_id == CompDispute.employee_id,
            )
            .correlate(CompDispute)
            .scalar_subquery()
        )
        # Join only the active employee result used to annotate a dispute.  This
        # keeps both result scope enforcement and latest-version selection in SQL,
        # rather than materializing every batch result/foreign dispute for a
        # store-level reviewer.
        statement = (
            select(CompDispute, PayrollResult)
            .join(
                PayrollResult,
                (PayrollResult.batch_id == CompDispute.batch_id)
                & (PayrollResult.batch_version == CompDispute.batch_version)
                & (PayrollResult.employee_id == CompDispute.employee_id)
                & (PayrollResult.version == latest_version),
            )
            .where(
                CompDispute.batch_id == batch_id,
                CompDispute.batch_version == batch.version,
            )
            .order_by(CompDispute.created_at.desc())
        )
        if read_scope is not None:
            statement = statement.where(
                tuple_(PayrollResult.org_unit_id, PayrollResult.department).in_(list(read_scope))
            )
        dispute_rows = session.execute(statement).all()
        dispute_ids = [dispute.id for dispute, _result in dispute_rows]
        events_by_dispute: dict[int, list[DisputeEventOut]] = {
            dispute_id: [] for dispute_id in dispute_ids
        }
        if dispute_ids:
            for event in session.scalars(
                select(DisputeEvent)
                .where(DisputeEvent.dispute_id.in_(dispute_ids))
                .order_by(DisputeEvent.created_at, DisputeEvent.id)
            ).all():
                events_by_dispute[event.dispute_id].append(DisputeEventOut.model_validate(event))
        response = [
            DisputeOut(
                id=dispute.id,
                employee_id=dispute.employee_id,
                org_unit_id=result.org_unit_id,
                department=result.department.value,
                salary_item=dispute.salary_item,
                opinion=dispute.opinion,
                raised_by=dispute.raised_by,
                status=dispute.status.value,
                resolution=dispute.resolution,
                resolved_by=dispute.resolved_by,
                resolved_at=dispute.resolved_at,
                created_at=dispute.created_at,
                allowed_attendance_fields=list(
                    allowed_attendance_fields(result, dispute.salary_item)
                ),
                correction_options=dispute_correction_options(
                    session,
                    result,
                    dispute.salary_item,
                ),
                events=events_by_dispute[dispute.id],
            )
            for dispute, result in dispute_rows
        ]
    audit.record(
        session,
        action="batch.disputes.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"batch_version": batch.version, "returned": len(response)},
    )
    session.commit()
    return response


@router.post("/{batch_id}/disputes", response_model=dict, status_code=status.HTTP_201_CREATED)
def create_dispute(
    batch_id: int,
    body: DisputeBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_REVIEW)),
    session: Session = Depends(get_session),
) -> dict:
    # Select the active result's latest snapshot first, then apply the review
    # scope to that same row.  Filtering before selecting the latest version
    # would allow a historical in-scope result to authorize a newer out-of-scope
    # snapshot; loading it without a scope would reveal its existence.
    latest_result = aliased(PayrollResult)
    latest_version = (
        select(func.max(latest_result.version))
        .where(
            latest_result.batch_id == PayrollResult.batch_id,
            latest_result.batch_version == PayrollResult.batch_version,
            latest_result.employee_id == PayrollResult.employee_id,
        )
        .correlate(PayrollResult)
        .scalar_subquery()
    )
    statement = (
        select(PayrollBatch)
        .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
        .where(
            PayrollBatch.id == batch_id,
            PayrollResult.batch_version == PayrollBatch.version,
            PayrollResult.employee_id == body.employee_id,
            PayrollResult.version == latest_version,
        )
        .limit(1)
    )
    batch = session.scalars(
        _apply_result_scope(statement, resolve_review_scope(session, principal))
    ).first()
    if batch is None:
        _raise_dispute_target_not_found()

    emp = session.get(Employee, body.employee_id)
    if emp is None:
        _raise_dispute_target_not_found()
    try:
        dispute = raise_dispute(
            session,
            batch,
            emp,
            body.salary_item,
            body.opinion,
            principal.user_id,
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.dispute",
        actor=(principal.user_id, principal.username),
        target_type="comp_dispute",
        target_id=dispute.id,
        detail={"employee_id": emp.id, "item": body.salary_item},
    )
    session.commit()
    return {"dispute_id": dispute.id, "batch_status": batch.status.value}


class AttendanceChanges(BaseModel):
    """异议同意时允许改动的源考勤字段（禁止任意 dict 直写数据库）。"""

    model_config = ConfigDict(extra="forbid")

    expected_days: Decimal | None = Field(default=None, ge=0, le=31, max_digits=6, decimal_places=2)
    actual_days: Decimal | None = Field(default=None, ge=0, le=31, max_digits=6, decimal_places=2)
    worked_hours: Decimal | None = Field(default=None, ge=0, le=744, max_digits=6, decimal_places=2)
    rest_days: Decimal | None = Field(default=None, ge=0, le=31, max_digits=6, decimal_places=2)
    overtime_hours: Decimal | None = Field(
        default=None, ge=0, le=744, max_digits=6, decimal_places=2
    )

    @model_validator(mode="after")
    def has_at_least_one_change(self) -> AttendanceChanges:
        if not any(getattr(self, field_name) is not None for field_name in type(self).model_fields):
            raise ValueError("同意异议时必须提供至少一项源考勤修改")
        return self


class HolidayWorkSourceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["HOLIDAY_WORK"]
    holiday_date: date
    worked: bool


class PerformanceSourceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["PERFORMANCE"]
    coefficient: Decimal = Field(ge=0, le=5, max_digits=5, decimal_places=3)
    score: Decimal | None = Field(default=None, ge=0, le=100, max_digits=6, decimal_places=2)
    remark: str | None = Field(default=None, max_length=255)

    @field_validator("remark", mode="before")
    @classmethod
    def normalize_remark(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class MonthlyAdjustmentSourceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["MONTHLY_ADJUSTMENT"]
    amount: Decimal = Field(gt=0, max_digits=14, decimal_places=2)
    taxable: bool
    in_social_base: bool
    in_housing_base: bool


class SalaryStructureSourceCorrection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["SALARY_STRUCTURE"]
    component_id: int = Field(gt=0)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


SourceCorrection = Annotated[
    HolidayWorkSourceCorrection
    | PerformanceSourceCorrection
    | MonthlyAdjustmentSourceCorrection
    | SalaryStructureSourceCorrection,
    Field(discriminator="kind"),
]


class ResolveBody(BaseModel):
    decision: DisputeStatus
    resolution: str = Field(min_length=1, max_length=1000)
    attendance_changes: AttendanceChanges | None = None
    source_correction: SourceCorrection | None = None
    attachment_url: str | None = Field(default=None, max_length=512)

    @model_validator(mode="after")
    def validate_source_changes(self) -> ResolveBody:
        correction_count = sum(
            value is not None for value in (self.attendance_changes, self.source_correction)
        )
        if self.decision == DisputeStatus.APPROVED and correction_count != 1:
            raise ValueError("同意异议必须且只能提供一种基础来源更正")
        if self.decision == DisputeStatus.APPROVED and self.attachment_url is None:
            raise ValueError("同意异议并修改源数据时必须上传证明附件")
        if self.decision != DisputeStatus.APPROVED and correction_count:
            raise ValueError("仅同意异议时允许修改基础来源数据")
        return self

    @field_validator("resolution")
    @classmethod
    def require_nonblank_resolution(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("处理说明不能为空")
        return value

    @field_validator("attachment_url", mode="before")
    @classmethod
    def validate_attachment_url(cls, value: object) -> object:
        return optional_http_url(value)


@router.post("/disputes/{dispute_id}/resolve", response_model=dict)
def resolve(
    dispute_id: int,
    body: ResolveBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> dict:
    dispute = _correctable_dispute_or_404(session, dispute_id, principal)
    try:
        resolved = resolve_dispute(
            session,
            dispute,
            decision=body.decision,
            resolution=body.resolution,
            approver_id=principal.user_id,
            attendance_changes=(
                body.attendance_changes.model_dump(exclude_none=True)
                if body.attendance_changes is not None
                else None
            ),
            source_correction=(
                body.source_correction.model_dump(exclude_none=False)
                if body.source_correction is not None
                else None
            ),
            attachment_url=body.attachment_url,
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="dispute.resolve",
        actor=(principal.user_id, principal.username),
        target_type="comp_dispute",
        target_id=dispute.id,
        detail={"decision": body.decision.value},
    )
    session.commit()
    return {"status": resolved.status.value}


class SupplementBody(BaseModel):
    note: str = Field(min_length=1, max_length=1000)
    attachment_url: str = Field(min_length=1, max_length=512)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("attachment_url", mode="before")
    @classmethod
    def validate_attachment_url(cls, value: object) -> object:
        if isinstance(value, str):
            return require_http_url(value)
        return value


@router.post("/disputes/{dispute_id}/supplements", response_model=dict)
def add_dispute_supplement(
    dispute_id: int,
    body: SupplementBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_REVIEW)),
    session: Session = Depends(get_session),
) -> dict:
    dispute = _reviewable_dispute_or_404(session, dispute_id, principal)
    try:
        supplemented = supplement_dispute(
            session,
            dispute,
            note=body.note,
            attachment_url=body.attachment_url,
            actor_id=principal.user_id,
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="dispute.supplement",
        actor=(principal.user_id, principal.username),
        target_type="comp_dispute",
        target_id=dispute.id,
        detail={"event": "SUPPLEMENTED"},
    )
    session.commit()
    return {"status": supplemented.status.value}


@router.post("/{batch_id}/approve", response_model=dict)
def approve(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_APPROVE)),
    session: Session = Depends(get_session),
) -> dict:
    _require_global_batch_lifecycle_permission(session, principal, Perm.PAYROLL_APPROVE)
    batch = _batch_or_404(session, batch_id)
    try:
        approve_batch(session, batch, principal.user_id)
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.approve",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"version": batch.version},
    )
    session.commit()
    return {"status": batch.status.value}


@router.post("/{batch_id}/lock", response_model=dict)
def lock(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_APPROVE)),
    session: Session = Depends(get_session),
) -> dict:
    _require_global_batch_lifecycle_permission(session, principal, Perm.PAYROLL_APPROVE)
    batch = _batch_or_404(session, batch_id)
    try:
        lock_batch(session, batch, principal.user_id)
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.lock",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
    )
    session.commit()
    return {"status": batch.status.value}


class UnlockBody(BaseModel):
    reason: str = Field(min_length=1, max_length=500)

    @field_validator("reason")
    @classmethod
    def require_nonblank_reason(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("原因不能为空")
        return value


@router.post("/{batch_id}/unlock", response_model=dict)
def unlock(
    batch_id: int,
    body: UnlockBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> dict:
    _require_global_batch_lifecycle_permission(session, principal, Perm.PAYROLL_CORRECT)
    batch = _batch_or_404(session, batch_id)
    try:
        unlock_batch(session, batch, principal.user_id, body.reason)
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.unlock",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"reason": body.reason, "version": batch.version},
    )
    session.commit()
    return {"status": batch.status.value, "version": batch.version}


@router.post("/{batch_id}/reopen", response_model=dict)
def reopen(
    batch_id: int,
    body: UnlockBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> dict:
    _require_global_batch_lifecycle_permission(session, principal, Perm.PAYROLL_CORRECT)
    batch = _batch_or_404(session, batch_id)
    try:
        reopen_batch(session, batch, principal.user_id, body.reason)
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="batch.reopen",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch.id,
        detail={"reason": body.reason, "version": batch.version},
    )
    session.commit()
    return {"status": batch.status.value, "version": batch.version}
