"""Strictly scoped DingTalk notifications and compensation-appeal services.

Sandbox remains the default.  Live mode renders a short-lived message from the
immutable payroll result at dispatch time; neither message bodies nor provider
recipient ids are copied into delivery/audit records.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from urllib.parse import urlencode

from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.approval import service as approvals
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.core.config import DingTalkMode, Settings, get_settings
from app.db.session import SessionLocal
from app.dingtalk.client import DingTalkClient, DingTalkClientError, get_dingtalk_client
from app.models.approval import ApprovalActionType, ApprovalBusinessType, ApprovalInstance
from app.models.auth import Permission, RolePermission, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    AppealCorrectionWorkStatus,
    AppealStatus,
    CompAppeal,
    CompAppealCorrectionWorkItem,
    DingTalkDelivery,
    DingTalkDeliveryKind,
    DingTalkDeliveryStatus,
)
from app.models.employee import Department, Employee
from app.models.org import OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import BatchConfirmation, PayrollResult


class DingTalkError(Exception):
    """A safe operational notification error."""


class AppealNotFound(DingTalkError):
    """Use a not-found response to avoid disclosing a different manager's scope."""


@dataclass(frozen=True)
class DeliveryStageSummary:
    routed: int
    configuration_failures: int
    existing: int
    pending_delivery_ids: tuple[int, ...] = ()


def _now(session: Session) -> datetime:
    return session.scalar(select(func.now()))  # type: ignore[return-value]


def _review_recipient_ids(
    session: Session, *, org_unit_id: int, department: Department
) -> list[int]:
    """Return active users with both an explicit review scope and action grant."""

    return list(
        session.scalars(
            select(UserReviewScope.user_id)
            .join(User, User.id == UserReviewScope.user_id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(RolePermission, RolePermission.role_id == UserRole.role_id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                UserReviewScope.org_unit_id == org_unit_id,
                UserReviewScope.department == department,
                User.is_deleted.is_(False),
                User.status == "ACTIVE",
                Permission.code == Perm.PAYROLL_REVIEW,
            )
            .distinct()
            .order_by(UserReviewScope.user_id)
        ).all()
    )


def _review_delivery_key(
    *, batch: PayrollBatch, org_unit_id: int, department: Department, recipient_user_id: int | None
) -> str:
    recipient = str(recipient_user_id) if recipient_user_id is not None else "UNROUTABLE"
    return f"review:{batch.id}:{batch.version}:{org_unit_id}:{department.value}:{recipient}"


def _add_delivery_if_missing(
    session: Session,
    *,
    batch: PayrollBatch,
    org_unit_id: int,
    department: Department,
    recipient_user_id: int | None,
    kind: DingTalkDeliveryKind,
    idempotency_key: str,
    initial_status: DingTalkDeliveryStatus,
    error_code: str | None = None,
    batch_version: int | None = None,
) -> tuple[DingTalkDelivery, bool]:
    existing = session.scalars(
        select(DingTalkDelivery)
        .where(DingTalkDelivery.idempotency_key == idempotency_key)
        .with_for_update()
    ).first()
    if existing is not None:
        return existing, False
    result_version = batch.version if batch_version is None else batch_version
    sandboxed = initial_status is DingTalkDeliveryStatus.SANDBOXED
    delivery = DingTalkDelivery(
        batch_id=batch.id,
        batch_version=result_version,
        org_unit_id=org_unit_id,
        department=department,
        recipient_user_id=recipient_user_id,
        kind=kind,
        status=initial_status,
        error_code=error_code,
        attempt_count=1 if sandboxed else 0,
        dispatched_at=_now(session) if sandboxed else None,
        idempotency_key=idempotency_key,
    )
    session.add(delivery)
    return delivery, True


def _initial_delivery_state(
    session: Session,
    *,
    recipient_user_id: int | None,
    settings: Settings,
    missing_recipient_error: str | None,
) -> tuple[DingTalkDeliveryStatus, str | None]:
    if recipient_user_id is None:
        return DingTalkDeliveryStatus.FAILED, missing_recipient_error
    if settings.dingtalk_mode is DingTalkMode.SANDBOX:
        return DingTalkDeliveryStatus.SANDBOXED, None
    recipient = session.get(User, recipient_user_id)
    if recipient is None or not recipient.dingtalk_user_id:
        return DingTalkDeliveryStatus.FAILED, "MISSING_DINGTALK_USER_ID"
    return DingTalkDeliveryStatus.PENDING, None


def stage_review_deliveries(
    session: Session,
    *,
    batch_id: int,
    settings: Settings | None = None,
) -> DeliveryStageSummary:
    """Create one delivery per current confirmation scope.

    Exact ``UserReviewScope`` assignments are the routing configuration.  Zero
    or multiple eligible users fail closed as an operationally visible delivery
    failure instead of selecting a broader recipient.
    """

    active_settings = settings or get_settings()
    batch = session.scalars(
        select(PayrollBatch).where(PayrollBatch.id == batch_id).with_for_update()
    ).first()
    if batch is None:
        raise DingTalkError("Payroll batch not found")
    if batch.status in {BatchStatus.DRAFT, BatchStatus.CALCULATING}:
        raise DingTalkError("Payroll batch has no reviewable result round")
    confirmations = list(
        session.scalars(
            select(BatchConfirmation)
            .where(
                BatchConfirmation.batch_id == batch.id,
                BatchConfirmation.batch_version == batch.version,
            )
            .order_by(BatchConfirmation.org_unit_id, BatchConfirmation.department)
            .with_for_update()
        ).all()
    )
    if not confirmations:
        raise DingTalkError("Payroll batch has no review scopes to notify")

    routed = configuration_failures = existing = 0
    pending_deliveries: list[DingTalkDelivery] = []
    for confirmation in confirmations:
        recipients = _review_recipient_ids(
            session,
            org_unit_id=confirmation.org_unit_id,
            department=confirmation.department,
        )
        recipient_id = recipients[0] if len(recipients) == 1 else None
        error_code = (
            None
            if recipient_id is not None
            else (
                "MISSING_ELIGIBLE_RECIPIENT" if not recipients else "AMBIGUOUS_ELIGIBLE_RECIPIENT"
            )
        )
        initial_status, error_code = _initial_delivery_state(
            session,
            recipient_user_id=recipient_id,
            settings=active_settings,
            missing_recipient_error=error_code,
        )
        delivery, created = _add_delivery_if_missing(
            session,
            batch=batch,
            org_unit_id=confirmation.org_unit_id,
            department=confirmation.department,
            recipient_user_id=recipient_id,
            kind=DingTalkDeliveryKind.PAYROLL_REVIEW,
            idempotency_key=_review_delivery_key(
                batch=batch,
                org_unit_id=confirmation.org_unit_id,
                department=confirmation.department,
                recipient_user_id=recipient_id,
            ),
            initial_status=initial_status,
            error_code=error_code,
        )
        if not created:
            existing += 1
        elif delivery.status is DingTalkDeliveryStatus.FAILED:
            configuration_failures += 1
        else:
            routed += 1
            if delivery.status is DingTalkDeliveryStatus.PENDING:
                pending_deliveries.append(delivery)
    session.flush()
    return DeliveryStageSummary(
        routed=routed,
        configuration_failures=configuration_failures,
        existing=existing,
        pending_delivery_ids=tuple(delivery.id for delivery in pending_deliveries),
    )


_DEPARTMENT_LABEL = {
    Department.DINING: "厅面",
    Department.KITCHEN: "厨房",
    Department.OTHER: "其他",
}


def _markdown_plain(value: str) -> str:
    """Escape provider Markdown metacharacters and collapse control whitespace."""

    normalized = " ".join(value.split())
    for marker in ("\\", "`", "*", "_", "[", "]", "(", ")", "#", ">"):
        normalized = normalized.replace(marker, f"\\{marker}")
    return normalized


def _review_action_card(
    session: Session,
    *,
    delivery: DingTalkDelivery,
    settings: Settings,
) -> tuple[str, str, str]:
    batch = session.get(PayrollBatch, delivery.batch_id)
    organization = session.get(OrgUnit, delivery.org_unit_id)
    if batch is None or organization is None:
        raise DingTalkError("The delivery source record is incomplete")

    latest = aliased(PayrollResult)
    latest_version = (
        select(func.max(latest.version))
        .where(
            latest.batch_id == PayrollResult.batch_id,
            latest.batch_version == PayrollResult.batch_version,
            latest.employee_id == PayrollResult.employee_id,
        )
        .correlate(PayrollResult)
        .scalar_subquery()
    )
    results = list(
        session.execute(
            select(Employee.name, PayrollResult.gross, PayrollResult.net)
            .join(Employee, Employee.id == PayrollResult.employee_id)
            .where(
                PayrollResult.batch_id == delivery.batch_id,
                PayrollResult.batch_version == delivery.batch_version,
                PayrollResult.org_unit_id == delivery.org_unit_id,
                PayrollResult.department == delivery.department,
                PayrollResult.version == latest_version,
                Employee.is_deleted.is_(False),
            )
            .order_by(Employee.name, Employee.id)
        ).all()
    )
    if not results:
        raise DingTalkError("The delivery has no payroll results")

    title = f"{batch.period} 薪资复核"
    lines = [
        f"### {_markdown_plain(title)}",
        f"门店：{_markdown_plain(organization.name)}",
        f"部门：{_DEPARTMENT_LABEL[delivery.department]}",
        f"人数：{len(results)}",
        "",
    ]
    omitted = 0
    for name, gross, net in results:
        line = f"- {_markdown_plain(name)}：应发 {gross:.2f}，实发 {net:.2f}"
        if len("\n".join([*lines, line]).encode("utf-8")) > 1200:
            omitted += 1
        else:
            lines.append(line)
    if omitted:
        lines.append(f"- 另有 {omitted} 人，请进入系统查看")
    lines.extend(["", "> 薪资敏感信息，仅限本门店本部门授权负责人查看。"])

    public_base_url = settings.dingtalk_public_base_url
    if public_base_url is None:
        raise DingTalkError("DingTalk public base URL is not configured")
    query = urlencode({"delivery_id": delivery.id})
    action_url = f"{str(public_base_url).rstrip('/')}/comp-appeals?{query}"
    return title, "\n".join(lines), action_url


def _appeal_status_action_card(
    *, delivery: DingTalkDelivery, settings: Settings
) -> tuple[str, str, str]:
    title = "薪资申诉状态更新"
    markdown = "### 薪资申诉状态已更新\n请进入薪酬系统查看处理结论。"
    public_base_url = settings.dingtalk_public_base_url
    if public_base_url is None:
        raise DingTalkError("DingTalk public base URL is not configured")
    action_url = f"{str(public_base_url).rstrip('/')}/comp-appeals"
    return title, markdown, action_url


def dispatch_live_delivery(
    session: Session,
    *,
    delivery_id: int,
    settings: Settings | None = None,
    client: DingTalkClient | None = None,
) -> DingTalkDelivery:
    """Serialize one pending/failed delivery attempt under a database row lock."""

    active_settings = settings or get_settings()
    if active_settings.dingtalk_mode is not DingTalkMode.LIVE:
        raise DingTalkError("Live DingTalk delivery is disabled")
    delivery = session.scalars(
        select(DingTalkDelivery).where(DingTalkDelivery.id == delivery_id).with_for_update()
    ).first()
    if delivery is None:
        raise DingTalkError("DingTalk delivery not found")
    if delivery.status is DingTalkDeliveryStatus.SENT:
        return delivery
    if delivery.status is DingTalkDeliveryStatus.SANDBOXED:
        raise DingTalkError("A sandbox delivery cannot be promoted to a live notification")
    recipient = (
        session.get(User, delivery.recipient_user_id)
        if delivery.recipient_user_id is not None
        else None
    )
    if recipient is None or not recipient.dingtalk_user_id:
        delivery.status = DingTalkDeliveryStatus.FAILED
        delivery.error_code = "MISSING_DINGTALK_USER_ID"
        session.flush()
        return delivery

    try:
        if delivery.kind is DingTalkDeliveryKind.PAYROLL_REVIEW:
            title, markdown, action_url = _review_action_card(
                session, delivery=delivery, settings=active_settings
            )
        else:
            title, markdown, action_url = _appeal_status_action_card(
                delivery=delivery, settings=active_settings
            )
    except DingTalkError:
        delivery.status = DingTalkDeliveryStatus.FAILED
        delivery.error_code = "SOURCE_DATA_INVALID"
        session.flush()
        return delivery

    delivery.attempt_count += 1
    delivery.dispatched_at = _now(session)
    try:
        result = (client or get_dingtalk_client()).send_action_card(
            recipient_user_id=recipient.dingtalk_user_id,
            title=title,
            markdown=markdown,
            action_url=action_url,
        )
    except DingTalkClientError:
        delivery.status = DingTalkDeliveryStatus.FAILED
        delivery.error_code = "PROVIDER_SEND_FAILED"
    else:
        delivery.status = DingTalkDeliveryStatus.SENT
        delivery.error_code = None
        delivery.provider_task_id = result.task_id
    session.flush()
    return delivery


def dispatch_live_deliveries(delivery_ids: tuple[int, ...]) -> None:
    """Background-task entrypoint; each status transition commits independently."""

    if not delivery_ids:
        return
    settings = get_settings()
    if settings.dingtalk_mode is not DingTalkMode.LIVE:
        return
    for delivery_id in delivery_ids:
        with SessionLocal() as session:
            try:
                dispatch_live_delivery(session, delivery_id=delivery_id, settings=settings)
                session.commit()
            except DingTalkError:
                session.rollback()


def retry_sandbox_delivery(session: Session, *, delivery_id: int) -> DingTalkDelivery:
    """Requeue a sandbox delivery without creating another notification payload."""

    delivery = session.scalars(
        select(DingTalkDelivery).where(DingTalkDelivery.id == delivery_id).with_for_update()
    ).first()
    if delivery is None:
        raise DingTalkError("DingTalk delivery not found")
    if delivery.recipient_user_id is None:
        raise DingTalkError("Fix the delivery routing configuration before retrying")
    if delivery.status is DingTalkDeliveryStatus.SENT:
        raise DingTalkError("A sent delivery cannot be retried in sandbox")
    delivery.status = DingTalkDeliveryStatus.SANDBOXED
    delivery.error_code = None
    delivery.attempt_count += 1
    delivery.dispatched_at = _now(session)
    session.flush()
    return delivery


def _delivery_for_appeal(
    session: Session, *, delivery_id: int, principal: Principal
) -> DingTalkDelivery:
    delivery = session.scalars(
        select(DingTalkDelivery).where(DingTalkDelivery.id == delivery_id).with_for_update()
    ).first()
    if (
        delivery is None
        or delivery.kind is not DingTalkDeliveryKind.PAYROLL_REVIEW
        or delivery.recipient_user_id != principal.user_id
    ):
        raise AppealNotFound("Delivery not found")
    if delivery.status not in {
        DingTalkDeliveryStatus.SANDBOXED,
        DingTalkDeliveryStatus.SENT,
    }:
        raise DingTalkError("The review notification is not available for an appeal")
    return delivery


def _validate_appeal_employee(
    session: Session, *, delivery: DingTalkDelivery, employee_id: int | None
) -> None:
    if employee_id is None:
        return
    latest = aliased(PayrollResult)
    latest_version = (
        select(func.max(latest.version))
        .where(
            latest.batch_id == PayrollResult.batch_id,
            latest.batch_version == PayrollResult.batch_version,
            latest.employee_id == PayrollResult.employee_id,
        )
        .correlate(PayrollResult)
        .scalar_subquery()
    )
    visible = session.scalar(
        select(PayrollResult.id)
        .join(Employee, Employee.id == PayrollResult.employee_id)
        .where(
            PayrollResult.batch_id == delivery.batch_id,
            PayrollResult.batch_version == delivery.batch_version,
            PayrollResult.employee_id == employee_id,
            PayrollResult.org_unit_id == delivery.org_unit_id,
            PayrollResult.department == delivery.department,
            PayrollResult.version == latest_version,
            Employee.is_deleted.is_(False),
        )
        .with_for_update()
        .limit(1)
    )
    if visible is None:
        raise AppealNotFound("Employee not found in the delivered review scope")


def create_appeal(
    session: Session,
    *,
    delivery_id: int,
    employee_id: int | None,
    reason: str,
    principal: Principal,
) -> CompAppeal:
    """Open one approval-backed appeal for a manager's own delivery scope."""

    delivery = _delivery_for_appeal(session, delivery_id=delivery_id, principal=principal)
    _validate_appeal_employee(session, delivery=delivery, employee_id=employee_id)
    dedupe_key = (
        f"delivery:{delivery.id}:employee:{employee_id if employee_id is not None else 'ALL'}"
    )
    existing = session.scalars(
        select(CompAppeal).where(CompAppeal.dedupe_key == dedupe_key).with_for_update()
    ).first()
    if existing is not None:
        raise DingTalkError("An appeal for this delivered scope is already in progress")
    try:
        flow, steps = approvals.select_flow(
            session,
            business_type=ApprovalBusinessType.COMP_APPEAL,
            org_unit_id=delivery.org_unit_id,
            amount=Decimal(0),
        )
    except approvals.ApprovalError as exc:
        raise DingTalkError(str(exc)) from None
    appeal = CompAppeal(
        delivery_id=delivery.id,
        batch_id=delivery.batch_id,
        batch_version=delivery.batch_version,
        org_unit_id=delivery.org_unit_id,
        department=delivery.department,
        employee_id=employee_id,
        requester_id=principal.user_id,
        reason=reason,
        status=AppealStatus.PENDING,
        dedupe_key=dedupe_key,
    )
    session.add(appeal)
    session.flush()
    instance = approvals.start_instance(
        session,
        flow=flow,
        steps=steps,
        business_type=ApprovalBusinessType.COMP_APPEAL,
        business_id=appeal.id,
        requester_id=principal.user_id,
        org_unit_id=appeal.org_unit_id,
        amount=Decimal(0),
    )
    appeal.approval_instance_id = instance.id
    session.flush()
    return appeal


def lock_appeal_for_instance(session: Session, *, instance: ApprovalInstance) -> CompAppeal:
    appeal = session.scalars(
        select(CompAppeal).where(CompAppeal.id == instance.business_id).with_for_update()
    ).first()
    if appeal is None or appeal.approval_instance_id != instance.id:
        raise DingTalkError("Approval instance appeal document is inconsistent")
    if appeal.status is not AppealStatus.PENDING:
        raise DingTalkError("Compensation appeal is no longer pending")
    return appeal


def _correction_work_status(
    *, batch: PayrollBatch, appeal: CompAppeal
) -> AppealCorrectionWorkStatus:
    """Classify an approved appeal without mutating payroll state.

    An approval for an immutable historical review round must never be applied
    to the current round.  A locked current round similarly needs the existing
    explicit unlock path before any source value can be changed.  Neither case
    silently changes payroll or produces a fake settlement.
    """

    if batch.version != appeal.batch_version:
        return AppealCorrectionWorkStatus.HISTORICAL_SETTLEMENT_REQUIRED
    if batch.status is BatchStatus.LOCKED:
        return AppealCorrectionWorkStatus.PENDING_REOPEN
    return AppealCorrectionWorkStatus.PENDING_TRIAGE


def queue_appeal_correction_work_item(
    session: Session,
    *,
    appeal: CompAppeal,
    approved_by: int,
) -> CompAppealCorrectionWorkItem:
    """Atomically create the fail-closed handoff after a final approval.

    The appeal lacks a mandatory source-data patch: it may cover an entire
    store/department notification and its reason is free text.  Therefore this
    function intentionally does *not* guess an employee, mutate attendance,
    reopen a batch, or call ``recompute_employee``.  A payroll-correction
    operator must subsequently select the verified source data and use the
    established unlock/reopen -> correction -> rerun workflow.
    """

    existing = session.scalars(
        select(CompAppealCorrectionWorkItem)
        .where(CompAppealCorrectionWorkItem.appeal_id == appeal.id)
        .with_for_update()
    ).first()
    if existing is not None:
        return existing
    batch = session.scalars(
        select(PayrollBatch).where(PayrollBatch.id == appeal.batch_id).with_for_update()
    ).first()
    if batch is None:
        raise DingTalkError("Appeal payroll batch no longer exists")
    work_item = CompAppealCorrectionWorkItem(
        appeal_id=appeal.id,
        batch_id=appeal.batch_id,
        source_batch_version=appeal.batch_version,
        org_unit_id=appeal.org_unit_id,
        department=appeal.department,
        employee_id=appeal.employee_id,
        status=_correction_work_status(batch=batch, appeal=appeal),
        created_by=approved_by,
    )
    session.add(work_item)
    session.flush()
    return work_item


def apply_appeal_approval_outcome(
    session: Session,
    *,
    appeal: CompAppeal,
    action: ApprovalActionType,
    is_final_approval: bool,
    comment: str | None,
    approved_by: int,
    settings: Settings | None = None,
) -> DingTalkDelivery | None:
    """Persist the business outcome and queue its configured status notification."""

    if action is ApprovalActionType.REJECT:
        appeal.status = AppealStatus.UPHELD
    elif is_final_approval:
        queue_appeal_correction_work_item(session, appeal=appeal, approved_by=approved_by)
        appeal.status = AppealStatus.CORRECTION_REQUIRED
    else:
        return None
    appeal.resolution = comment
    batch = session.get(PayrollBatch, appeal.batch_id)
    if batch is None:
        raise DingTalkError("Appeal payroll batch no longer exists")
    initial_status, error_code = _initial_delivery_state(
        session,
        recipient_user_id=appeal.requester_id,
        settings=settings or get_settings(),
        missing_recipient_error="MISSING_ELIGIBLE_RECIPIENT",
    )
    delivery, _created = _add_delivery_if_missing(
        session,
        batch=batch,
        org_unit_id=appeal.org_unit_id,
        department=appeal.department,
        recipient_user_id=appeal.requester_id,
        kind=DingTalkDeliveryKind.APPEAL_STATUS,
        idempotency_key=f"appeal:{appeal.id}:outcome:{appeal.status.value}",
        initial_status=initial_status,
        error_code=error_code,
        batch_version=appeal.batch_version,
    )
    session.flush()
    return delivery
