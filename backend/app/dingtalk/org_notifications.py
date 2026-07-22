"""Privacy-safe DingTalk summaries for organization-sync previews.

Only local recipient identifiers and stable provider outcomes are persisted.
The encrypted DingTalk user identifier is read at the outbound provider boundary
and is never copied into this notification table, an error, or an audit detail.
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.auth.permissions import Perm
from app.auth.service import load_global_permissions
from app.core.config import DingTalkMode, Settings, get_settings
from app.dingtalk.authorization import lock_review_authorization_tables
from app.dingtalk.client import (
    DingTalkClient,
    DingTalkClientError,
    DingTalkSendOutcomeUnknown,
    get_dingtalk_client,
)
from app.dingtalk.org_sync import take_organization_access_lock
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


def _recipient_authorization_error(session: Session, recipient_user_id: int) -> str | None:
    """Check recipient state without materializing the encrypted provider id."""

    state = session.execute(
        select(User.id, User.status, User.is_deleted, User.login_enabled).where(
            User.id == recipient_user_id
        )
    ).one_or_none()
    if state is None:
        return "RECIPIENT_NOT_AUTHORIZED"
    _id, status, is_deleted, login_enabled = state
    if (
        status != "ACTIVE"
        or is_deleted
        or not login_enabled
        or not _is_eligible_global_recipient(session, recipient_user_id)
    ):
        return "RECIPIENT_NOT_AUTHORIZED"
    identity_exists = session.scalar(
        select(User.id).where(
            User.id == recipient_user_id,
            User.dingtalk_user_id.is_not(None),
        )
    )
    return None if identity_exists is not None else "MISSING_DINGTALK_USER_ID"


def _locked_current_recipient(
    session: Session, *, recipient_user_id: int
) -> tuple[str | None, str | None]:
    """Revalidate and decrypt a provider id only under the post-marker locks."""

    recipient = session.scalars(
        select(User).where(User.id == recipient_user_id).with_for_update()
    ).first()
    if recipient is None:
        return None, "RECIPIENT_NOT_AUTHORIZED"
    if (
        recipient.status != "ACTIVE"
        or recipient.is_deleted
        or not recipient.login_enabled
        or not _is_eligible_global_recipient(session, recipient_user_id)
    ):
        return None, "RECIPIENT_NOT_AUTHORIZED"
    if not recipient.dingtalk_user_id:
        return None, "MISSING_DINGTALK_USER_ID"
    return recipient.dingtalk_user_id, None


def _action_card(batch: DingTalkOrgSyncBatch, settings: Settings) -> tuple[str, str, str]:
    public_base_url = settings.dingtalk_public_base_url
    if public_base_url is None:
        raise ValueError("missing public base URL")
    try:
        parsed_base_url = urlsplit(str(public_base_url))
        valid_base_url = (
            parsed_base_url.scheme == "https"
            and parsed_base_url.hostname is not None
            and (parsed_base_url.port is None or parsed_base_url.port > 0)
            and parsed_base_url.username is None
            and parsed_base_url.password is None
            and not parsed_base_url.query
            and not parsed_base_url.fragment
        )
    except ValueError:
        valid_base_url = False
    if not valid_base_url:
        raise ValueError("invalid public base URL")
    ready_count = batch.ready_region_count + batch.ready_store_count + batch.ready_reviewer_count
    conflict_count = (
        batch.region_conflict_count + batch.store_conflict_count + batch.reviewer_conflict_count
    )
    return (
        "组织同步待确认",
        f"发现 {ready_count} 项待应用变更，{conflict_count} 项冲突。请由集团 HR 进入薪酬平台核对。",
        urlunsplit(
            (
                parsed_base_url.scheme,
                parsed_base_url.netloc,
                f"{parsed_base_url.path.rstrip('/')}/org",
                "",
                "",
            )
        ),
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
    take_organization_access_lock(session)
    lock_review_authorization_tables(session)
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
    recipient_error = _recipient_authorization_error(session, notification.recipient_user_id)
    if recipient_error is not None:
        _mark_failed(notification, recipient_error)
        session.flush()
        return notification
    batch = session.get(DingTalkOrgSyncBatch, notification.batch_id)
    if batch is None:
        _mark_failed(notification, "BATCH_NOT_FOUND")
        session.flush()
        return notification
    try:
        title, markdown, action_url = _action_card(batch, active_settings)
    except ValueError:
        _mark_failed(notification, "PUBLIC_BASE_URL_INVALID")
        session.flush()
        return notification

    # Persist the unknown-outcome marker before the provider boundary.  A
    # confirmed response replaces it; a timeout remains terminal until a human
    # reconciles the provider state.
    notification.attempt_count += 1
    attempt_count = notification.attempt_count
    notification.dispatched_at = datetime.now(UTC)
    _mark_failed(notification, _UNKNOWN_OUTCOME)
    session.commit()

    take_organization_access_lock(session)
    lock_review_authorization_tables(session)
    notification = session.scalars(
        select(DingTalkOrgSyncNotification)
        .where(DingTalkOrgSyncNotification.id == notification_id)
        .with_for_update()
    ).first()
    if notification is None:
        raise DingTalkOrgSyncNotificationError("DingTalk organization notification not found")
    if (
        notification.status is not DingTalkDeliveryStatus.FAILED
        or notification.error_code != _UNKNOWN_OUTCOME
        or notification.attempt_count != attempt_count
        or notification.provider_task_id is not None
    ):
        return notification
    recipient_user_id, recipient_error = _locked_current_recipient(
        session, recipient_user_id=notification.recipient_user_id
    )
    if recipient_error is not None or recipient_user_id is None:
        notification.attempt_count = max(0, attempt_count - 1)
        notification.dispatched_at = None
        _mark_failed(notification, recipient_error or "MISSING_DINGTALK_USER_ID")
        session.commit()
        return notification

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
