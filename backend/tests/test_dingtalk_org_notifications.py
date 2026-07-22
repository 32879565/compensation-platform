from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.auth.permissions import Perm
from app.core.config import DingTalkMode, Settings
from app.core.security import hash_password
from app.dingtalk.client import DingTalkClientError, DingTalkSendOutcomeUnknown, DingTalkSendResult
from app.dingtalk.org_notifications import (
    dispatch_org_sync_notification,
    stage_org_sync_notifications,
)
from app.models.auth import Permission, Role, RolePermission, User, UserRole
from app.models.dingtalk import (
    DingTalkDeliveryStatus,
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncNotification,
)

pytestmark = pytest.mark.usefixtures("pg_engine")


def _settings(*, mode: DingTalkMode = DingTalkMode.SANDBOX) -> Settings:
    data: dict[str, object] = {
        "database_url": "postgresql+psycopg://test:test@localhost/test",
        "secret_key": "test-secret-key-only-for-tests-not-production",
        "encryption_key": "test-encryption-key-only-for-tests-not-production",
        "cookie_secure": False,
        "dingtalk_mode": mode,
    }
    if mode is DingTalkMode.LIVE:
        data.update(
            dingtalk_client_id="test-client-id",
            dingtalk_client_secret=SecretStr("test-client-secret-value"),
            dingtalk_agent_id=123,
            dingtalk_corp_id="ding-test-corp",
            dingtalk_read_sync_enabled=True,
            dingtalk_org_root_mappings="10:GROUP",
            dingtalk_public_base_url="https://pay.example.test",
        )
    return Settings(**data)


def _batch(session) -> DingTalkOrgSyncBatch:
    batch = DingTalkOrgSyncBatch(
        public_id="b" * 32,
        snapshot_hash="a" * 64,
        root_config_hash="c" * 64,
        local_baseline_hash="d" * 64,
        expires_at=datetime.now(UTC) + timedelta(hours=1),
        ready_region_count=1,
        ready_store_count=1,
        ready_reviewer_count=1,
        region_conflict_count=1,
        store_conflict_count=0,
        reviewer_conflict_count=0,
    )
    session.add(batch)
    session.flush()
    return batch


def _user(
    session,
    username: str,
    *,
    dingtalk_user_id: str | None,
    login_enabled: bool = True,
    status: str = "ACTIVE",
    is_deleted: bool = False,
) -> User:
    user = User(
        username=username,
        password_hash=hash_password("StrongPass123!"),
        dingtalk_user_id=dingtalk_user_id,
        login_enabled=login_enabled,
        status=status,
        is_deleted=is_deleted,
    )
    session.add(user)
    session.flush()
    return user


def _grant(session, user: User, *, global_scope: bool, permissions: tuple[str, ...]) -> None:
    seed_rbac(session)
    role = Role(
        code=f"ROLE_{user.id}_{global_scope}",
        name="notification test",
        is_global_scope=global_scope,
    )
    session.add(role)
    session.flush()
    permission_rows = session.scalars(
        select(Permission).where(Permission.code.in_(permissions))
    ).all()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.add_all(
        RolePermission(role_id=role.id, permission_id=permission.id)
        for permission in permission_rows
    )
    session.flush()


def _global_hr(session, username: str, *, dingtalk_user_id: str | None = "ding-recipient") -> User:
    user = _user(session, username, dingtalk_user_id=dingtalk_user_id)
    _grant(
        session,
        user,
        global_scope=True,
        permissions=(Perm.DINGTALK_ORG_SYNC, Perm.NOTIFICATION_MANAGE),
    )
    return user


def test_org_sync_notification_selects_only_global_hr_and_hides_pii(db_session):
    batch = _batch(db_session)
    global_hr = _global_hr(db_session, "global-hr")
    _global_hr(db_session, "missing-ding", dingtalk_user_id=None)
    scoped = _user(db_session, "scoped", dingtalk_user_id="ding-scoped")
    _grant(
        db_session,
        scoped,
        global_scope=False,
        permissions=(Perm.DINGTALK_ORG_SYNC, Perm.NOTIFICATION_MANAGE),
    )
    one_permission = _user(db_session, "one-permission", dingtalk_user_id="ding-one")
    _grant(db_session, one_permission, global_scope=True, permissions=(Perm.DINGTALK_ORG_SYNC,))
    for username, kwargs in (
        ("disabled", {"login_enabled": False}),
        ("inactive", {"status": "DISABLED"}),
        ("deleted", {"is_deleted": True}),
    ):
        ineligible = _user(db_session, username, dingtalk_user_id=f"ding-{username}", **kwargs)
        _grant(
            db_session,
            ineligible,
            global_scope=True,
            permissions=(Perm.DINGTALK_ORG_SYNC, Perm.NOTIFICATION_MANAGE),
        )

    ids = stage_org_sync_notifications(db_session, batch=batch, settings=_settings())
    rows = db_session.scalars(
        select(DingTalkOrgSyncNotification).where(DingTalkOrgSyncNotification.id.in_(ids))
    ).all()

    assert {row.recipient_user_id for row in rows} == {global_hr.id}
    assert all(
        row.idempotency_key == f"org-sync:{batch.public_id}:user:{row.recipient_user_id}"
        for row in rows
    )
    assert all("ding-recipient" not in str(row.__dict__) for row in rows)


def test_staging_is_idempotent_and_sandboxed(db_session):
    batch = _batch(db_session)
    _global_hr(db_session, "global-hr")

    first = stage_org_sync_notifications(db_session, batch=batch, settings=_settings())
    second = stage_org_sync_notifications(db_session, batch=batch, settings=_settings())
    rows = db_session.scalars(select(DingTalkOrgSyncNotification)).all()

    assert first == second
    assert len(rows) == 1
    assert rows[0].status is DingTalkDeliveryStatus.SANDBOXED

    client = _Client(DingTalkSendResult(task_id=9, request_id=None))
    returned = dispatch_org_sync_notification(
        db_session, notification_id=first[0], settings=_settings(), client=client
    )
    assert returned.status is DingTalkDeliveryStatus.SANDBOXED
    assert client.calls == []


class _Client:
    def __init__(self, outcome: object) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, str]] = []

    def send_action_card(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_dispatch_sends_only_counts_and_stable_provider_outcomes(db_session):
    batch = _batch(db_session)
    _global_hr(db_session, "global-hr", dingtalk_user_id="remote-user-id")
    notification_id = stage_org_sync_notifications(
        db_session, batch=batch, settings=_settings(mode=DingTalkMode.LIVE)
    )[0]
    client = _Client(DingTalkSendResult(task_id=41, request_id="provider-request"))

    row = dispatch_org_sync_notification(
        db_session,
        notification_id=notification_id,
        settings=_settings(mode=DingTalkMode.LIVE),
        client=client,
    )

    assert row.status is DingTalkDeliveryStatus.SENT
    assert row.attempt_count == 1
    assert row.provider_task_id == 41
    assert client.calls == [
        {
            "recipient_user_id": "remote-user-id",
            "title": "组织同步待确认",
            "markdown": "发现 3 项待应用变更，1 项冲突。请由集团 HR 进入薪酬平台核对。",
            "action_url": "https://pay.example.test/org",
            "action_title": "查看组织同步",
        }
    ]
    persisted = db_session.get(DingTalkOrgSyncNotification, notification_id)
    assert "remote-user-id" not in str(persisted.__dict__)


@pytest.mark.parametrize(
    ("outcome", "error_code"),
    [
        (DingTalkClientError("provider body must stay private"), "PROVIDER_SEND_FAILED"),
        (
            DingTalkSendOutcomeUnknown("provider body must stay private"),
            "PROVIDER_SEND_OUTCOME_UNKNOWN",
        ),
    ],
)
def test_dispatch_stores_only_stable_provider_errors(db_session, outcome, error_code):
    batch = _batch(db_session)
    _global_hr(db_session, "global-hr")
    settings = _settings(mode=DingTalkMode.LIVE)
    notification_id = stage_org_sync_notifications(db_session, batch=batch, settings=settings)[0]
    client = _Client(outcome)

    row = dispatch_org_sync_notification(
        db_session, notification_id=notification_id, settings=settings, client=client
    )

    assert row.status is DingTalkDeliveryStatus.FAILED
    assert row.error_code == error_code
    assert row.attempt_count == 1
    assert "provider body" not in str(row.__dict__)
    if error_code == "PROVIDER_SEND_OUTCOME_UNKNOWN":
        same = dispatch_org_sync_notification(
            db_session, notification_id=notification_id, settings=settings, client=client
        )
        assert same.attempt_count == 1
        assert len(client.calls) == 1


def test_live_dispatch_without_a_public_url_fails_without_calling_provider(db_session):
    batch = _batch(db_session)
    _global_hr(db_session, "global-hr")
    live_settings = _settings(mode=DingTalkMode.LIVE)
    notification_id = stage_org_sync_notifications(db_session, batch=batch, settings=live_settings)[
        0
    ]
    missing_url_settings = Settings.model_construct(
        dingtalk_mode=DingTalkMode.LIVE,
        dingtalk_public_base_url=None,
    )
    client = _Client(DingTalkSendResult(task_id=1, request_id=None))

    row = dispatch_org_sync_notification(
        db_session,
        notification_id=notification_id,
        settings=missing_url_settings,
        client=client,
    )

    assert row.status is DingTalkDeliveryStatus.FAILED
    assert row.error_code == "PUBLIC_BASE_URL_MISSING"
    assert row.attempt_count == 0
    assert client.calls == []
