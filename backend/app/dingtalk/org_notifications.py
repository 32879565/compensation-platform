"""Privacy-safe DingTalk summaries for organization-sync previews.

Only local recipient identifiers and stable provider outcomes are persisted.
The encrypted DingTalk user identifier is read at the outbound provider boundary
and is never copied into this notification table, an error, or an audit detail.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.permissions import Perm
from app.auth.service import load_global_permissions
from app.core.config import DingTalkMode, Settings, get_settings
from app.dingtalk.client import (
    DingTalkClient,
    DingTalkClientError,
    DingTalkSendOutcomeUnknown,
    get_dingtalk_client,
)
from app.models.auth import User
from app.models.dingtalk import (
    DingTalkDeliveryStatus,
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncNotification,
)


class DingTalkOrgSyncNotificationError(Exception):
    """A notification row required for dispatch does not exist."""


_REQUIRED_GLOBAL_PERMISSIONS = frozenset((Perm.DINGTALK_ORG_SYNC, Perm.NOTIFICATION_MANAGE))
_UNKNOWN_OUTCOME = "PROVIDER_SEND_OUTCOME_UNKNOWN"


def _idempotency_key(batch: DingTalkOrgSyncBatch, user_id: int) -> str:
    return f"org-sync:{batch.public_id}:user:{user_id}"


def _is_eligible_global_recipient(session: Session, user_id: int) -> bool:
    return _REQUIRED_GLOBAL_PERMISSIONS.issubset(load_global_permissions(session, user_id))


def stage_org_sync_notifications(
    session: Session,
    *,
    batch: DingTalkOrgSyncBatch,
    settings: Settings | None = None,
) -> tuple[int, ...]:
    """Stage one deterministic notification per eligible global HR recipient.

    A savepoint absorbs a concurrent unique-key winner without rolling back
    unrelated work in the caller's transaction.  Repeated staging returns the
    existing identifiers and never creates duplicate rows.
    """

    active_settings = settings or get_settings()
    candidate_ids = session.scalars(
        select(User.id)
        .where(
            User.status == "ACTIVE",
            User.is_deleted.is_(False),
            User.login_enabled.is_(True),
            User.dingtalk_user_id.is_not(None),
        )
        .order_by(User.id)
    ).all()
    status = (
        DingTalkDeliveryStatus.SANDBOXED
        if active_settings.dingtalk_mode is DingTalkMode.SANDBOX
        else DingTalkDeliveryStatus.PENDING
    )
    notification_ids: list[int] = []

    for user_id in candidate_ids:
        if not _is_eligible_global_recipient(session, user_id):
            continue
        key = _idempotency_key(batch, user_id)
        existing = session.scalars(
            select(DingTalkOrgSyncNotification.id).where(
                DingTalkOrgSyncNotification.idempotency_key == key
            )
        ).first()
        if existing is not None:
            notification_ids.append(existing)
            continue

        notification = DingTalkOrgSyncNotification(
            batch_id=batch.id,
            recipient_user_id=user_id,
            status=status,
            idempotency_key=key,
        )
        try:
            # Never call ``session.rollback`` here: staging may be part of a
            # larger caller-owned transaction.
            with session.begin_nested():
                session.add(notification)
                session.flush()
        except IntegrityError:
            existing = session.scalars(
                select(DingTalkOrgSyncNotification.id).where(
                    DingTalkOrgSyncNotification.idempotency_key == key
                )
            ).first()
            if existing is None:
                raise
            notification_ids.append(existing)
        else:
            notification_ids.append(notification.id)

    return tuple(notification_ids)


def _mark_failed(notification: DingTalkOrgSyncNotification, error_code: str) -> None:
    notification.status = DingTalkDeliveryStatus.FAILED
    notification.error_code = error_code
    notification.provider_task_id = None


def _current_eligible_recipient(session: Session, recipient_user_id: int) -> bool:
    """Recheck only non-PII state before decrypting the provider user id."""

    state = session.execute(
        select(User.id, User.status, User.is_deleted, User.login_enabled).where(
            User.id == recipient_user_id
        )
    ).one_or_none()
    if state is None:
        return False
    _id, status, is_deleted, login_enabled = state
    return (
        status == "ACTIVE"
        and not is_deleted
        and login_enabled
        and _is_eligible_global_recipient(session, recipient_user_id)
    )


def _action_card(batch: DingTalkOrgSyncBatch, settings: Settings) -> tuple[str, str, str]:
    public_base_url = settings.dingtalk_public_base_url
    if public_base_url is None:
        raise ValueError("missing public base URL")
    ready_count = batch.ready_region_count + batch.ready_store_count + batch.ready_reviewer_count
    conflict_count = (
        batch.region_conflict_count + batch.store_conflict_count + batch.reviewer_conflict_count
    )
    return (
        "组织同步待确认",
        f"发现 {ready_count} 项待应用变更，{conflict_count} 项冲突。请由集团 HR 进入薪酬平台核对。",
        f"{str(public_base_url).rstrip('/')}/org",
    )


def dispatch_org_sync_notification(
    session: Session,
    *,
    notification_id: int,
    settings: Settings | None = None,
    client: DingTalkClient | None = None,
) -> DingTalkOrgSyncNotification:
    """Deliver one staged summary, failing closed after an unknown outcome."""

    active_settings = settings or get_settings()
    notification = session.scalars(
        select(DingTalkOrgSyncNotification)
        .where(DingTalkOrgSyncNotification.id == notification_id)
        .with_for_update()
    ).first()
    if notification is None:
        raise DingTalkOrgSyncNotificationError("DingTalk organization notification not found")
    if notification.status in {DingTalkDeliveryStatus.SENT, DingTalkDeliveryStatus.SANDBOXED}:
        return notification
    if (
        notification.status is DingTalkDeliveryStatus.FAILED
        and notification.error_code == _UNKNOWN_OUTCOME
    ):
        # A POST may already have reached DingTalk; never resend automatically.
        return notification
    if active_settings.dingtalk_mode is DingTalkMode.SANDBOX:
        notification.status = DingTalkDeliveryStatus.SANDBOXED
        notification.error_code = None
        session.flush()
        return notification
    if active_settings.dingtalk_public_base_url is None:
        _mark_failed(notification, "PUBLIC_BASE_URL_MISSING")
        session.flush()
        return notification
    if not _current_eligible_recipient(session, notification.recipient_user_id):
        _mark_failed(notification, "RECIPIENT_NOT_AUTHORIZED")
        session.flush()
        return notification

    # This is the only point the encrypted provider identity is materialized.
    recipient = session.get(User, notification.recipient_user_id)
    recipient_user_id = recipient.dingtalk_user_id if recipient is not None else None
    if not recipient_user_id:
        _mark_failed(notification, "MISSING_DINGTALK_USER_ID")
        session.flush()
        return notification
    batch = session.get(DingTalkOrgSyncBatch, notification.batch_id)
    if batch is None:
        _mark_failed(notification, "BATCH_NOT_FOUND")
        session.flush()
        return notification
    title, markdown, action_url = _action_card(batch, active_settings)

    # Persist the unknown-outcome marker before the provider boundary.  A
    # confirmed response replaces it; a timeout remains terminal until a human
    # reconciles the provider state.
    notification.attempt_count += 1
    notification.dispatched_at = datetime.now(UTC)
    _mark_failed(notification, _UNKNOWN_OUTCOME)
    session.commit()

    try:
        result = (client or get_dingtalk_client()).send_action_card(
            recipient_user_id=recipient_user_id,
            title=title,
            markdown=markdown,
            action_url=action_url,
            action_title="查看组织同步",
        )
    except DingTalkSendOutcomeUnknown:
        # Keep the durable marker and the single confirmed provider attempt.
        return notification
    except DingTalkClientError:
        _mark_failed(notification, "PROVIDER_SEND_FAILED")
    else:
        notification.status = DingTalkDeliveryStatus.SENT
        notification.error_code = None
        notification.provider_task_id = result.task_id
    session.commit()
    return notification
