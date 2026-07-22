from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings
from app.core.security import hash_password
from app.dingtalk.client import (
    DingTalkClientError,
    DingTalkDepartment,
    DingTalkOrganizationSnapshot,
)
from app.dingtalk.org_sync_job import _schedule_lock, run_scheduled_org_sync
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import DingTalkOrgSyncBatch, DingTalkOrgSyncNotification
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit


class _Connection:
    def __init__(self, *results: object) -> None:
        self.results = iter(results)
        self.statements: list[str] = []
        self.closed = False
        self.invalidated = False

    def scalar(self, statement: object) -> object:
        self.statements.append(str(statement))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    def invalidate(self) -> None:
        self.invalidated = True

    def close(self) -> None:
        self.closed = True


class _Bind:
    def __init__(self, dialect_name: str, connection: _Connection | None = None) -> None:
        self.dialect = SimpleNamespace(name=dialect_name)
        self.connection = connection
        self.connect_calls = 0

    def connect(self) -> _Connection:
        self.connect_calls += 1
        assert self.connection is not None
        return self.connection


class _LockSession:
    def __init__(self, bind: _Bind) -> None:
        self.bind = bind

    def get_bind(self) -> _Bind:
        return self.bind


def test_sqlite_skips_lock_sql_without_skipping_the_job() -> None:
    bind = _Bind("sqlite")

    with _schedule_lock(_LockSession(bind)) as acquired:  # type: ignore[arg-type]
        assert acquired is True

    assert bind.connect_calls == 0


def test_schedule_lock_is_nonblocking_and_released_on_body_failure() -> None:
    connection = _Connection(True, True)
    session = _LockSession(_Bind("postgresql", connection))

    with pytest.raises(RuntimeError, match="preview failed"):
        with _schedule_lock(session) as acquired:  # type: ignore[arg-type]
            assert acquired is True
            raise RuntimeError("preview failed")

    assert "pg_try_advisory_lock" in connection.statements[0]
    assert "pg_advisory_unlock" in connection.statements[1]
    assert connection.closed is True


def test_schedule_lock_contention_does_not_attempt_unlock() -> None:
    connection = _Connection(False)

    with _schedule_lock(
        _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
    ) as acquired:
        assert acquired is False

    assert len(connection.statements) == 1
    assert connection.closed is True


def test_schedule_lock_discards_connection_when_unlock_fails() -> None:
    connection = _Connection(True, RuntimeError("connection lost"))

    with _schedule_lock(
        _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
    ) as acquired:
        assert acquired is True

    assert connection.invalidated is True
    assert connection.closed is True


@pytest.mark.usefixtures("pg_engine")
def test_postgresql_schedule_lock_contends_then_releases(db_session, pg_engine) -> None:
    with _schedule_lock(db_session) as acquired:
        assert acquired is True
        with Session(pg_engine) as contender:
            with _schedule_lock(contender) as contended:
                assert contended is False

    with _schedule_lock(db_session) as reacquired:
        assert reacquired is True


class _JobSession:
    def __init__(self, batch: object, events: list[str]) -> None:
        self.batch = batch
        self.events = events
        self.bind = _Bind("sqlite")

    def get_bind(self) -> _Bind:
        return self.bind

    def rollback(self) -> None:
        self.events.append("rollback")

    def commit(self) -> None:
        self.events.append("commit")

    def scalars(self, statement: object) -> object:
        del statement
        return SimpleNamespace(one=lambda: self.batch)


def _settings() -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        secret_key="test-secret-key-only-for-tests-not-production",
        encryption_key="test-encryption-key-only-for-tests-not-production",
        cookie_secure=False,
        dingtalk_org_root_mappings="10:GROUP",
    )


def _preview(public_id: str = "b" * 32) -> SimpleNamespace:
    return SimpleNamespace(
        batch_id=public_id,
        ready_regions=1,
        ready_stores=2,
        ready_reviewers=3,
        region_conflicts=4,
        store_conflicts=5,
        reviewer_conflicts=6,
    )


def test_sqlite_job_runs_provider_and_preview_without_postgresql_sql(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.dingtalk.org_sync_job as job

    events: list[str] = []
    session = _JobSession(SimpleNamespace(id=11, public_id="b" * 32), events)

    class Client:
        def list_organization_snapshot(self, *, root_department_ids: tuple[int, ...]):
            events.append("provider")
            assert root_department_ids == (10,)
            return DingTalkOrganizationSnapshot(departments=(), users=())

    def preview(*args: object, **kwargs: object) -> SimpleNamespace:
        events.append("preview")
        return SimpleNamespace(
            batch_id="b" * 32,
            ready_regions=0,
            ready_stores=0,
            ready_reviewers=0,
            region_conflicts=0,
            store_conflicts=0,
            reviewer_conflicts=0,
        )

    monkeypatch.setattr(job, "preview_organization_sync", preview)
    monkeypatch.setattr(job.audit, "record", lambda *args, **kwargs: events.append("audit"))

    assert run_scheduled_org_sync(session, settings=_settings(), client=Client()) == 0  # type: ignore[arg-type]
    assert events == ["rollback", "provider", "preview", "audit", "commit"]
    assert session.bind.connect_calls == 0


def test_job_releases_existing_transaction_and_stages_before_individual_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.dingtalk.org_sync_job as job

    events: list[str] = []
    audit_kwargs: dict[str, object] = {}
    batch = SimpleNamespace(id=11, public_id="b" * 32)
    session = _JobSession(batch, events)

    @contextmanager
    def acquired_lock(_session: object):
        events.append("lock")
        yield True

    class Client:
        def list_organization_snapshot(self, *, root_department_ids: tuple[int, ...]):
            events.append("provider")
            assert root_department_ids == (10,)
            return DingTalkOrganizationSnapshot(departments=(), users=())

    monkeypatch.setattr(job, "_schedule_lock", acquired_lock)
    monkeypatch.setattr(job, "preview_organization_sync", lambda *args, **kwargs: _preview())

    def stage(*args: object, **kwargs: object) -> tuple[int, ...]:
        events.append("stage")
        return (101, 102)

    def dispatch(*args: object, notification_id: int, **kwargs: object) -> None:
        events.append(f"dispatch:{notification_id}")

    monkeypatch.setattr(job, "stage_org_sync_notifications", stage)
    monkeypatch.setattr(job, "dispatch_org_sync_notification", dispatch)

    def record(*args: object, **kwargs: object) -> None:
        events.append("audit")
        audit_kwargs.update(kwargs)

    monkeypatch.setattr(job.audit, "record", record)

    assert run_scheduled_org_sync(session, settings=_settings(), client=Client()) == 0  # type: ignore[arg-type]
    assert events == [
        "rollback",
        "lock",
        "provider",
        "stage",
        "commit",
        "dispatch:101",
        "dispatch:102",
        "audit",
        "commit",
    ]
    assert audit_kwargs["detail"] == {"changes": 6, "conflicts": 15}


def test_job_failure_audit_has_an_exact_safe_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.dingtalk.org_sync_job as job

    events: list[str] = []
    detail: dict[str, object] = {}
    session = _JobSession(SimpleNamespace(), events)

    @contextmanager
    def acquired_lock(_session: object):
        yield True

    class Client:
        def list_organization_snapshot(self, *, root_department_ids: tuple[int, ...]):
            del root_department_ids
            raise DingTalkClientError("provider payload: private-id-9988 Alice")

    def record(*args: object, **kwargs: object) -> None:
        detail.update(kwargs)

    monkeypatch.setattr(job, "_schedule_lock", acquired_lock)
    monkeypatch.setattr(job.audit, "record", record)

    assert run_scheduled_org_sync(session, settings=_settings(), client=Client()) == 1  # type: ignore[arg-type]
    assert detail == {
        "action": "dingtalk.organization.schedule.failed",
        "result": "FAILURE",
        "actor": None,
        "detail": {
            "error_code": "ORG_PROVIDER_UNAVAILABLE",
            "error_type": "DingTalkClientError",
        },
    }
    assert "private-id-9988" not in repr(detail)
    assert events == ["rollback", "rollback", "commit"]


@contextmanager
def _always_acquired(_session: object):
    yield True


@pytest.mark.usefixtures("pg_engine")
def test_scheduled_job_reuses_preview_and_notification_idempotency_keys(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.dingtalk.org_sync_job as job

    seed_rbac(db_session)
    db_session.add(OrgUnit(code="GROUP", name="Scheduled Group", type=OrgType.GROUP))
    user = User(
        username="scheduled-hr",
        password_hash=hash_password("StrongPass123!"),
        dingtalk_user_id="scheduled-provider-user",
    )
    db_session.add(user)
    db_session.flush()
    role = db_session.scalars(select(Role).where(Role.code == "GROUP_HR")).one()
    db_session.add(UserRole(user_id=user.id, role_id=role.id))
    db_session.commit()

    class Client:
        def list_organization_snapshot(self, *, root_department_ids: tuple[int, ...]):
            assert root_department_ids == (10,)
            return DingTalkOrganizationSnapshot(
                departments=(DingTalkDepartment(101, 10, "测试店"),),
                users=(),
            )

    monkeypatch.setattr(job, "_schedule_lock", _always_acquired)

    assert run_scheduled_org_sync(db_session, settings=_settings(), client=Client()) == 0
    batch = db_session.scalars(select(DingTalkOrgSyncBatch)).one()
    assert run_scheduled_org_sync(db_session, settings=_settings(), client=Client()) == 0
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == 1
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncNotification)) == 1
    keys = db_session.scalars(select(DingTalkOrgSyncNotification.idempotency_key)).all()
    assert keys == [f"org-sync:{batch.public_id}:user:{user.id}"]


@pytest.mark.usefixtures("pg_engine")
def test_provider_failure_has_no_formal_writes(db_session, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.dingtalk.org_sync_job as job

    db_session.add(OrgUnit(code="EXISTING", name="Existing Group", type=OrgType.GROUP))
    db_session.commit()
    before = tuple(
        db_session.scalar(select(func.count()).select_from(model))
        for model in (OrgUnit, Employee, User, UserRole, UserReviewScope)
    )

    class Client:
        def list_organization_snapshot(self, *, root_department_ids: tuple[int, ...]):
            del root_department_ids
            raise DingTalkClientError("private provider response")

    monkeypatch.setattr(job, "_schedule_lock", _always_acquired)

    assert run_scheduled_org_sync(db_session, settings=_settings(), client=Client()) == 1
    after = tuple(
        db_session.scalar(select(func.count()).select_from(model))
        for model in (OrgUnit, Employee, User, UserRole, UserReviewScope)
    )
    assert after == before
    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "dingtalk.organization.schedule.failed")
    ).one()
    assert audit_row.detail == {
        "error_code": "ORG_PROVIDER_UNAVAILABLE",
        "error_type": "DingTalkClientError",
    }
