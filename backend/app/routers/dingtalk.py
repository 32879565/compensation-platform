"""Sandbox DingTalk notification operations and scoped compensation appeals."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import get_current_principal, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.core.config import DingTalkMode, Settings, get_settings
from app.db.session import get_session
from app.dingtalk import service as dingtalk
from app.dingtalk.client import DingTalkClientError, get_dingtalk_client
from app.models.dingtalk import (
    AppealCorrectionWorkStatus,
    AppealStatus,
    CompAppeal,
    CompAppealCorrectionWorkItem,
    DingTalkDelivery,
    DingTalkDeliveryKind,
    DingTalkDeliveryStatus,
)
from app.models.employee import Department

router = APIRouter(tags=["dingtalk"])


def _require_global_notification_manager(
    principal: Principal = Depends(require_permission(Perm.NOTIFICATION_MANAGE)),
    session: Session = Depends(get_session),
) -> Principal:
    if resolve_permission_org_scope(session, principal, Perm.NOTIFICATION_MANAGE) is not None:
        raise HTTPException(
            status_code=403, detail="Notification management requires group organization scope"
        )
    return principal


class DeliveryOut(BaseModel):
    id: int
    batch_id: int
    batch_version: int
    org_unit_id: int
    department: Department
    recipient_user_id: int | None
    kind: DingTalkDeliveryKind
    status: str
    error_code: str | None
    attempt_count: int
    dispatched_at: datetime | None
    # This is intentionally a capability flag, not a recipient identifier.
    # A global notification operator can inspect routing records but may not
    # file an appeal for another manager's delivery.
    can_appeal: bool

    model_config = {"from_attributes": True}


def _delivery_out(delivery: DingTalkDelivery, principal: Principal) -> DeliveryOut:
    return DeliveryOut(
        id=delivery.id,
        batch_id=delivery.batch_id,
        batch_version=delivery.batch_version,
        org_unit_id=delivery.org_unit_id,
        department=delivery.department,
        recipient_user_id=delivery.recipient_user_id,
        kind=delivery.kind,
        status=delivery.status.value,
        error_code=delivery.error_code,
        attempt_count=delivery.attempt_count,
        dispatched_at=delivery.dispatched_at,
        can_appeal=(
            principal.has_permission(Perm.PAYROLL_REVIEW)
            and delivery.kind is DingTalkDeliveryKind.PAYROLL_REVIEW
            and delivery.recipient_user_id == principal.user_id
            and delivery.status in {DingTalkDeliveryStatus.SANDBOXED, DingTalkDeliveryStatus.SENT}
        ),
    )


class DeliveryStageOut(BaseModel):
    routed: int
    configuration_failures: int
    existing: int
    sandbox: bool = True


class DingTalkIntegrationOut(BaseModel):
    mode: DingTalkMode
    credentials_configured: bool
    app_id_configured: bool
    public_base_url_configured: bool
    ready_for_live: bool
    read_sync_enabled: bool
    read_sync_ready: bool


class DingTalkModeOut(BaseModel):
    mode: DingTalkMode


class DingTalkConnectionTestOut(BaseModel):
    connected: bool
    token_expires_in_seconds: int


def _integration_out(settings: Settings) -> DingTalkIntegrationOut:
    public_url_configured = settings.dingtalk_public_base_url is not None
    return DingTalkIntegrationOut(
        mode=settings.dingtalk_mode,
        credentials_configured=settings.dingtalk_credentials_configured,
        app_id_configured=settings.dingtalk_app_id is not None,
        public_base_url_configured=public_url_configured,
        ready_for_live=(settings.dingtalk_credentials_configured and public_url_configured),
        read_sync_enabled=settings.dingtalk_read_sync_enabled,
        read_sync_ready=(
            settings.dingtalk_read_sync_enabled and settings.dingtalk_credentials_configured
        ),
    )


@router.get("/api/dingtalk/mode", response_model=DingTalkModeOut)
def get_dingtalk_mode(
    _principal: Principal = Depends(require_permission(Perm.PAYROLL_REVIEW)),
    settings: Settings = Depends(get_settings),
) -> DingTalkModeOut:
    """Expose only the delivery mode needed for honest reviewer UI messaging."""

    return DingTalkModeOut(mode=settings.dingtalk_mode)


@router.get("/api/dingtalk/integration", response_model=DingTalkIntegrationOut)
def get_dingtalk_integration(
    _principal: Principal = Depends(_require_global_notification_manager),
    settings: Settings = Depends(get_settings),
) -> DingTalkIntegrationOut:
    """Expose readiness flags only; never return application identifiers or secrets."""

    return _integration_out(settings)


@router.post("/api/dingtalk/integration/test", response_model=DingTalkConnectionTestOut)
def test_dingtalk_integration(
    principal: Principal = Depends(_require_global_notification_manager),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> DingTalkConnectionTestOut:
    if not settings.dingtalk_credentials_configured:
        raise HTTPException(
            status_code=409, detail="DingTalk application credentials are incomplete"
        )
    try:
        connection = get_dingtalk_client().check_connection()
    except DingTalkClientError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from None
    audit.record(
        session,
        action="dingtalk.integration.test",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_integration",
        detail={"connected": True},
    )
    session.commit()
    return DingTalkConnectionTestOut(
        connected=True,
        token_expires_in_seconds=connection.expires_in_seconds,
    )


@router.post(
    "/api/dingtalk/batches/{batch_id}/review-deliveries",
    response_model=DeliveryStageOut,
    status_code=status.HTTP_201_CREATED,
)
def stage_batch_review_deliveries(
    batch_id: int,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(_require_global_notification_manager),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> DeliveryStageOut:
    try:
        summary = dingtalk.stage_review_deliveries(session, batch_id=batch_id, settings=settings)
    except dingtalk.DingTalkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="dingtalk.review.stage",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=batch_id,
        detail={
            "sandbox": settings.dingtalk_mode is DingTalkMode.SANDBOX,
            "routed": summary.routed,
            "configuration_failures": summary.configuration_failures,
            "existing": summary.existing,
        },
    )
    session.commit()
    if settings.dingtalk_mode is DingTalkMode.LIVE and summary.pending_delivery_ids:
        background_tasks.add_task(dingtalk.dispatch_live_deliveries, summary.pending_delivery_ids)
    return DeliveryStageOut(
        routed=summary.routed,
        configuration_failures=summary.configuration_failures,
        existing=summary.existing,
        sandbox=settings.dingtalk_mode is DingTalkMode.SANDBOX,
    )


def _visible_deliveries_statement(session: Session, principal: Principal, *, batch_id: int | None):
    statement = select(DingTalkDelivery).order_by(DingTalkDelivery.id.desc()).limit(500)
    if batch_id is not None:
        statement = statement.where(DingTalkDelivery.batch_id == batch_id)
    is_global_manager = principal.has_permission(Perm.NOTIFICATION_MANAGE) and (
        resolve_permission_org_scope(session, principal, Perm.NOTIFICATION_MANAGE) is None
    )
    if not is_global_manager:
        if not principal.has_permission(Perm.PAYROLL_REVIEW):
            raise HTTPException(status_code=403, detail="Notification delivery access is required")
        statement = statement.where(DingTalkDelivery.recipient_user_id == principal.user_id)
    return statement


@router.get("/api/dingtalk/deliveries", response_model=list[DeliveryOut])
def list_deliveries(
    batch_id: int | None = Query(default=None, gt=0),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> list[DeliveryOut]:
    rows = list(
        session.scalars(_visible_deliveries_statement(session, principal, batch_id=batch_id)).all()
    )
    audit.record(
        session,
        action="dingtalk.delivery.list.view",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_delivery",
        detail={"returned": len(rows), "batch_filtered": batch_id is not None},
    )
    session.commit()
    return [_delivery_out(row, principal) for row in rows]


@router.post("/api/dingtalk/deliveries/{delivery_id}/retry", response_model=DeliveryOut)
def retry_delivery(
    delivery_id: int,
    principal: Principal = Depends(_require_global_notification_manager),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> DeliveryOut:
    try:
        if settings.dingtalk_mode is DingTalkMode.LIVE:
            delivery = dingtalk.dispatch_live_delivery(
                session, delivery_id=delivery_id, settings=settings
            )
        else:
            delivery = dingtalk.retry_sandbox_delivery(session, delivery_id=delivery_id)
    except dingtalk.DingTalkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    retry_detail: dict[str, object] = {
        "sandbox": settings.dingtalk_mode is DingTalkMode.SANDBOX,
        "attempt_count": delivery.attempt_count,
    }
    if settings.dingtalk_mode is DingTalkMode.LIVE:
        retry_detail["status"] = delivery.status.value
    audit.record(
        session,
        action="dingtalk.delivery.retry",
        actor=(principal.user_id, principal.username),
        target_type="dingtalk_delivery",
        target_id=delivery.id,
        detail=retry_detail,
    )
    session.commit()
    return _delivery_out(delivery, principal)


class AppealCreateBody(BaseModel):
    delivery_id: int = Field(gt=0)
    employee_id: int | None = Field(default=None, gt=0)
    reason: str = Field(min_length=1, max_length=2000)

    @field_validator("reason")
    @classmethod
    def strip_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Appeal reason must not be blank")
        return normalized


class AppealOut(BaseModel):
    id: int
    delivery_id: int
    batch_id: int
    batch_version: int
    org_unit_id: int
    department: Department
    employee_id: int | None
    requester_id: int
    reason: str
    status: AppealStatus
    resolution: str | None
    approval_instance_id: int | None
    created_at: datetime

    model_config = {"from_attributes": True}


class AppealCorrectionWorkItemOut(BaseModel):
    """Low-sensitivity HR queue item for an approved appeal.

    Free-text reason/resolution, employee identifiers, and salary information
    remain in their separately scoped source records.  This list only tells a
    payroll-correction operator which immutable review round needs attention.
    """

    id: int
    appeal_id: int
    batch_id: int
    source_batch_version: int
    org_unit_id: int
    department: Department
    status: AppealCorrectionWorkStatus
    created_at: datetime

    model_config = {"from_attributes": True}


@router.post("/api/comp-appeals", response_model=AppealOut, status_code=status.HTTP_201_CREATED)
def create_comp_appeal(
    body: AppealCreateBody,
    principal: Principal = Depends(require_permission(Perm.PAYROLL_REVIEW)),
    session: Session = Depends(get_session),
) -> CompAppeal:
    try:
        appeal = dingtalk.create_appeal(
            session,
            delivery_id=body.delivery_id,
            employee_id=body.employee_id,
            reason=body.reason,
            principal=principal,
        )
    except dingtalk.AppealNotFound:
        raise HTTPException(status_code=404, detail="Delivered review scope not found") from None
    except dingtalk.DingTalkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="comp_appeal.create",
        actor=(principal.user_id, principal.username),
        target_type="comp_appeal",
        target_id=appeal.id,
        detail={
            "delivery_id": appeal.delivery_id,
            "batch_id": appeal.batch_id,
            "batch_version": appeal.batch_version,
            "org_unit_id": appeal.org_unit_id,
            "department": appeal.department.value,
            "employee_targeted": appeal.employee_id is not None,
            "approval_instance_id": appeal.approval_instance_id,
        },
    )
    session.commit()
    return appeal


def _visible_appeal_or_404(session: Session, principal: Principal, appeal_id: int) -> CompAppeal:
    appeal = session.get(CompAppeal, appeal_id)
    if appeal is None:
        raise HTTPException(status_code=404, detail="Compensation appeal not found")
    if appeal.requester_id == principal.user_id:
        return appeal
    if principal.has_permission(Perm.ADJUSTMENT_READ):
        scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_READ)
        if scope is None or appeal.org_unit_id in scope:
            return appeal
    raise HTTPException(status_code=404, detail="Compensation appeal not found")


@router.get("/api/comp-appeals", response_model=list[AppealOut])
def list_comp_appeals(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> list[CompAppeal]:
    statement = select(CompAppeal)
    if principal.has_permission(Perm.ADJUSTMENT_READ):
        scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_READ)
        if scope is None:
            pass
        elif scope:
            statement = statement.where(
                or_(
                    CompAppeal.requester_id == principal.user_id,
                    CompAppeal.org_unit_id.in_(scope),
                )
            )
        else:
            statement = statement.where(CompAppeal.requester_id == principal.user_id)
    elif principal.has_permission(Perm.PAYROLL_REVIEW):
        statement = statement.where(CompAppeal.requester_id == principal.user_id)
    else:
        raise HTTPException(status_code=403, detail="Compensation appeal access is required")
    appeals = list(session.scalars(statement.order_by(CompAppeal.id.desc()).limit(200)).all())
    audit.record(
        session,
        action="comp_appeal.list.view",
        actor=(principal.user_id, principal.username),
        target_type="comp_appeal",
        detail={"returned": len(appeals)},
    )
    session.commit()
    return appeals


@router.get("/api/comp-appeals/{appeal_id}", response_model=AppealOut)
def get_comp_appeal(
    appeal_id: int,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> CompAppeal:
    appeal = _visible_appeal_or_404(session, principal, appeal_id)
    audit.record(
        session,
        action="comp_appeal.view",
        actor=(principal.user_id, principal.username),
        target_type="comp_appeal",
        target_id=appeal.id,
        detail={"status": appeal.status.value, "employee_targeted": appeal.employee_id is not None},
    )
    session.commit()
    return appeal


@router.get(
    "/api/comp-appeal-corrections",
    response_model=list[AppealCorrectionWorkItemOut],
)
def list_appeal_correction_work_items(
    principal: Principal = Depends(require_permission(Perm.PAYROLL_CORRECT)),
    session: Session = Depends(get_session),
) -> list[CompAppealCorrectionWorkItem]:
    """List only work items within the correction operator's organization scope."""

    scope = resolve_permission_org_scope(session, principal, Perm.PAYROLL_CORRECT)
    statement = (
        select(CompAppealCorrectionWorkItem)
        .order_by(CompAppealCorrectionWorkItem.id.desc())
        .limit(200)
    )
    if scope is not None:
        statement = statement.where(CompAppealCorrectionWorkItem.org_unit_id.in_(scope))
    rows = list(session.scalars(statement).all())
    audit.record(
        session,
        action="comp_appeal.correction_work_item.list.view",
        actor=(principal.user_id, principal.username),
        target_type="comp_appeal_correction_work_item",
        detail={"returned": len(rows)},
    )
    session.commit()
    return rows
