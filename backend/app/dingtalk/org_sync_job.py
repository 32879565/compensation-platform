"""One-shot scheduled DingTalk organization preview.

The schedule lock is held on a dedicated PostgreSQL connection for the whole
provider-read, preview, notification, and audit cycle.  The scheduled path only
creates or reuses a preview; formal organization data is never applied here.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.core.config import Settings, get_settings
from app.db.session import SessionLocal
from app.dingtalk.client import DingTalkClient, DingTalkClientError, get_dingtalk_client
from app.dingtalk.org_notifications import (
    dispatch_org_sync_notification,
    stage_org_sync_notifications,
)
from app.dingtalk.org_sync import DingTalkOrganizationSyncError, preview_organization_sync
from app.models.dingtalk import DingTalkOrgSyncBatch, DingTalkOrgSyncTrigger

_SCHEDULE_LOCK = "compensation-platform:dingtalk-org-sync-schedule:v1"


def _dedicated_connection(session: Session) -> Connection:
    bind = session.get_bind()
    engine = bind.engine if isinstance(bind, Connection) else bind
    return cast(Engine, engine).connect()


@contextmanager
def _schedule_lock(session: Session) -> Iterator[bool]:
    """Hold the non-blocking schedule lock without dirtying a pooled connection."""

    if session.get_bind().dialect.name != "postgresql":
        # PostgreSQL functions must never be compiled or executed on SQLite and
        # test/development dialects continue the job without a PostgreSQL lock.
        yield True
        return

    connection = _dedicated_connection(session)
    acquired = False
    pending_error: BaseException | None = None
    safe_to_close = True

    def invalidate_connection() -> BaseException | None:
        nonlocal safe_to_close

        # Until invalidation succeeds, closing could return an unknown-lock
        # connection to the pool. Prefer leaking that handle to doing so.
        safe_to_close = False
        try:
            connection.invalidate()
        except BaseException as exc:
            return exc
        safe_to_close = True
        return None

    try:
        try:
            acquired = bool(
                connection.scalar(select(func.pg_try_advisory_lock(func.hashtext(_SCHEDULE_LOCK))))
            )
        except BaseException as exc:
            # The server may have acquired the session lock before the client
            # lost the outcome. This connection can never safely re-enter the pool.
            pending_error = exc
            invalidate_connection()
            raise

        try:
            yield acquired
        except BaseException as exc:
            pending_error = exc
            raise
        finally:
            if acquired:
                try:
                    released = connection.scalar(
                        select(func.pg_advisory_unlock(func.hashtext(_SCHEDULE_LOCK)))
                    )
                except BaseException as exc:
                    invalidate_connection()
                    if pending_error is None:
                        pending_error = exc
                        raise
                    # Preserve an exception or cancellation already raised by
                    # the protected job body; cleanup must not replace it.
                else:
                    if not released:
                        invalidation_error = invalidate_connection()
                        if invalidation_error is not None and pending_error is None:
                            pending_error = invalidation_error
                            raise invalidation_error
    finally:
        if safe_to_close:
            try:
                connection.close()
            except BaseException:
                if pending_error is None:
                    raise


def _failure_code(error: DingTalkClientError | DingTalkOrganizationSyncError) -> str:
    if isinstance(error, DingTalkOrganizationSyncError):
        return error.code
    return "ORG_PROVIDER_UNAVAILABLE"


def run_scheduled_org_sync(
    session: Session,
    *,
    settings: Settings,
    client: DingTalkClient,
    now: datetime | None = None,
) -> int:
    """Run one scheduled preview, returning a process exit status."""

    # Session construction and harmless reads may have autobegun a transaction.
    # Release it before lock acquisition and before the provider boundary.
    session.rollback()

    with _schedule_lock(session) as acquired:
        if not acquired:
            return 0
        try:
            snapshot = client.list_organization_snapshot(
                root_department_ids=tuple(
                    remote_id for remote_id, _ in settings.dingtalk_org_root_mapping_pairs
                )
            )
            preview = preview_organization_sync(
                session,
                snapshot,
                encryption_key=settings.encryption_key,
                actor=None,
                root_mappings=settings.dingtalk_org_root_mapping_pairs,
                trigger=DingTalkOrgSyncTrigger.SCHEDULED,
                now=now,
                dining_manager_titles=settings.dingtalk_dining_manager_title_set,
                kitchen_manager_titles=settings.dingtalk_kitchen_manager_title_set,
            )
            batch = session.scalars(
                select(DingTalkOrgSyncBatch).where(
                    DingTalkOrgSyncBatch.public_id == preview.batch_id
                )
            ).one()
            change_count = preview.ready_regions + preview.ready_stores + preview.ready_reviewers
            conflict_count = (
                preview.region_conflicts + preview.store_conflicts + preview.reviewer_conflicts
            )
            if change_count or conflict_count:
                notification_ids = stage_org_sync_notifications(
                    session, batch=batch, settings=settings
                )
                # Make every staged idempotency key durable before any provider
                # call. Dispatch owns its own per-recipient transaction boundary.
                session.commit()
                for notification_id in notification_ids:
                    dispatch_org_sync_notification(
                        session,
                        notification_id=notification_id,
                        settings=settings,
                        client=client,
                    )
                    # Dispatch has several safe early-return branches that may
                    # only flush. Persist every recipient independently before
                    # starting the next provider boundary.
                    session.commit()
            audit.record(
                session,
                action="dingtalk.organization.schedule.succeeded",
                actor=None,
                target_type="dingtalk_org_sync_batch",
                target_id=batch.id,
                detail={"changes": change_count, "conflicts": conflict_count},
            )
            session.commit()
            return 0
        except (DingTalkClientError, DingTalkOrganizationSyncError) as exc:
            session.rollback()
            audit.record(
                session,
                action="dingtalk.organization.schedule.failed",
                result="FAILURE",
                actor=None,
                detail={
                    "error_code": _failure_code(exc),
                    "error_type": type(exc).__name__,
                },
            )
            session.commit()
            return 1


def main() -> int:
    settings = get_settings()
    with SessionLocal() as session:
        return run_scheduled_org_sync(
            session,
            settings=settings,
            client=get_dingtalk_client(),
        )


if __name__ == "__main__":
    raise SystemExit(main())
