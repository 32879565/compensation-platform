from __future__ import annotations

import gc
from contextlib import contextmanager
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, event, func, select, text
from sqlalchemy.orm import Session
from sqlalchemy.pool import QueuePool

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings
from app.core.security import hash_password
from app.dingtalk.client import (
    DingTalkClientError,
    DingTalkDepartment,
    DingTalkOrganizationSnapshot,
)
from app.dingtalk.org_sync_job import _SCHEDULE_LOCK, _schedule_lock, run_scheduled_org_sync
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import DingTalkOrgSyncBatch, DingTalkOrgSyncNotification
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit


class _Connection:
    def __init__(
        self,
        *results: object,
        detach_error: BaseException | None = None,
        invalidate_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self.results = iter(results)
        self.detach_error = detach_error
        self.invalidate_error = invalidate_error
        self.close_error = close_error
        self.statements: list[str] = []
        self.operations: list[str] = []
        self.closed = False
        self.detached = False
        self.invalidated = False
        self.close_calls = 0
        self.detach_calls = 0
        self.invalidate_calls = 0

    def detach(self) -> None:
        self.operations.append("detach")
        self.detach_calls += 1
        if self.detach_error is not None:
            raise self.detach_error
        self.detached = True

    def scalar(self, statement: object) -> object:
        self.operations.append("scalar")
        self.statements.append(str(statement))
        result = next(self.results)
        if isinstance(result, BaseException):
            raise result
        return result

    def invalidate(self) -> None:
        self.operations.append("invalidate")
        self.invalidate_calls += 1
        if self.invalidate_error is not None:
            raise self.invalidate_error
        self.invalidated = True

    def close(self) -> None:
        self.operations.append("close")
        self.close_calls += 1
        if self.close_error is not None:
            raise self.close_error
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
    assert connection.operations[:2] == ["detach", "scalar"]
    assert connection.detached is True
    assert connection.closed is True


def test_schedule_lock_contention_does_not_attempt_unlock() -> None:
    connection = _Connection(False)

    with _schedule_lock(
        _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
    ) as acquired:
        assert acquired is False

    assert connection.operations == ["detach", "scalar", "close"]
    assert connection.detached is True
    assert len(connection.statements) == 1
    assert connection.closed is True


def test_schedule_lock_discards_connection_when_unlock_fails() -> None:
    connection = _Connection(True, RuntimeError("connection lost"))

    with pytest.raises(RuntimeError, match="connection lost"):
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ) as acquired:
            assert acquired is True

    assert connection.invalidated is True
    assert connection.closed is True


def test_schedule_lock_invalidates_when_unlock_reports_not_released() -> None:
    connection = _Connection(True, False)

    with _schedule_lock(
        _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
    ) as acquired:
        assert acquired is True

    assert connection.invalidate_calls == 1
    assert connection.invalidated is True
    assert connection.closed is True


@pytest.mark.parametrize(
    "acquire_error",
    [RuntimeError("acquire failed"), KeyboardInterrupt()],
    ids=["exception", "base-exception"],
)
def test_schedule_lock_invalidates_and_reraises_acquire_error(
    acquire_error: BaseException,
) -> None:
    connection = _Connection(acquire_error)

    with pytest.raises(type(acquire_error)) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ):
            pytest.fail("the lock body must not run")

    assert raised.value is acquire_error
    assert connection.operations[:2] == ["detach", "scalar"]
    assert connection.detached is True
    assert connection.invalidated is True
    assert connection.closed is True


def test_schedule_lock_invalidates_and_reraises_unlock_interrupt() -> None:
    unlock_error = KeyboardInterrupt()
    connection = _Connection(True, unlock_error)

    with pytest.raises(KeyboardInterrupt) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ) as acquired:
            assert acquired is True

    assert raised.value is unlock_error
    assert connection.invalidated is True
    assert connection.closed is True


def test_schedule_lock_preserves_body_interrupt_when_unlock_also_fails() -> None:
    body_error = KeyboardInterrupt()
    connection = _Connection(True, SystemExit("unlock interrupted"))

    with pytest.raises(KeyboardInterrupt) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ) as acquired:
            assert acquired is True
            raise body_error

    assert raised.value is body_error
    assert connection.invalidated is True
    assert connection.closed is True


def test_schedule_lock_closes_detached_connection_when_acquire_invalidation_fails() -> None:
    acquire_error = KeyboardInterrupt()
    connection = _Connection(
        acquire_error,
        invalidate_error=SystemExit("invalidation interrupted"),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ):
            pytest.fail("the lock body must not run")

    assert raised.value is acquire_error
    assert connection.detached is True
    assert connection.invalidate_calls == 1
    assert connection.close_calls == 1


def test_schedule_lock_closes_detached_connection_when_unlock_invalidation_fails() -> None:
    body_error = SystemExit("body cancelled")
    connection = _Connection(
        True,
        KeyboardInterrupt(),
        invalidate_error=RuntimeError("invalidation failed"),
    )

    with pytest.raises(SystemExit) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ) as acquired:
            assert acquired is True
            raise body_error

    assert raised.value is body_error
    assert connection.detached is True
    assert connection.invalidate_calls == 1
    assert connection.close_calls == 1


def test_schedule_lock_preserves_acquire_error_when_close_also_fails() -> None:
    acquire_error = RuntimeError("acquire outcome unknown")
    connection = _Connection(
        acquire_error,
        close_error=KeyboardInterrupt(),
    )

    with pytest.raises(RuntimeError) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ):
            pytest.fail("the lock body must not run")

    assert raised.value is acquire_error
    assert connection.invalidated is True
    assert connection.close_calls == 1


def test_schedule_lock_preserves_unlock_error_when_close_also_fails() -> None:
    unlock_error = SystemExit("unlock outcome unknown")
    connection = _Connection(
        True,
        unlock_error,
        close_error=KeyboardInterrupt(),
    )

    with pytest.raises(SystemExit) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ) as acquired:
            assert acquired is True

    assert raised.value is unlock_error
    assert connection.invalidated is True
    assert connection.close_calls == 1


def test_schedule_lock_preserves_body_error_when_safe_close_fails() -> None:
    body_error = KeyboardInterrupt()
    connection = _Connection(
        True,
        True,
        close_error=SystemExit("close interrupted"),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ) as acquired:
            assert acquired is True
            raise body_error

    assert raised.value is body_error
    assert connection.invalidated is False
    assert connection.close_calls == 1


def test_schedule_lock_detach_failure_closes_before_any_lock_sql() -> None:
    detach_error = KeyboardInterrupt()
    connection = _Connection(
        detach_error=detach_error,
        close_error=SystemExit("close interrupted"),
    )

    with pytest.raises(KeyboardInterrupt) as raised:
        with _schedule_lock(
            _LockSession(_Bind("postgresql", connection))  # type: ignore[arg-type]
        ):
            pytest.fail("the lock body must not run")

    assert raised.value is detach_error
    assert connection.detach_calls == 1
    assert connection.statements == []
    assert connection.invalidate_calls == 0
    assert connection.close_calls == 1


@pytest.mark.usefixtures("pg_engine")
def test_postgresql_schedule_lock_contends_then_releases(db_session, pg_engine) -> None:
    with _schedule_lock(db_session) as acquired:
        assert acquired is True
        with Session(pg_engine) as contender:
            with _schedule_lock(contender) as contended:
                assert contended is False

    with _schedule_lock(db_session) as reacquired:
        assert reacquired is True


@pytest.mark.usefixtures("pg_engine")
def test_detached_postgresql_lock_never_checks_in_after_cleanup_failures(
    pg_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    import app.dingtalk.org_sync_job as job

    isolated_engine = create_engine(
        pg_engine.url,
        poolclass=QueuePool,
        pool_size=1,
        max_overflow=0,
        pool_timeout=2,
        future=True,
    )
    checked_in_connections: list[object] = []

    @event.listens_for(isolated_engine.pool, "checkin")
    def record_checkin(dbapi_connection: object, _connection_record: object) -> None:
        checked_in_connections.append(dbapi_connection)

    real_connection = isolated_engine.connect()
    locked_driver_connection = real_connection.connection.driver_connection

    class CleanupFailureConnection:
        def __init__(self, connection) -> None:
            self.connection = connection
            self.locked_backend_pid: int | None = None

        def detach(self) -> None:
            self.connection.detach()

        def scalar(self, statement: object) -> object:
            if "pg_advisory_unlock" in str(statement):
                raise RuntimeError("unlock outcome unknown")
            result = self.connection.scalar(statement)
            self.locked_backend_pid = self.connection.scalar(text("SELECT pg_backend_pid()"))
            return result

        def invalidate(self) -> None:
            raise KeyboardInterrupt()

        def close(self) -> None:
            self.connection.close()

    faulty_connection = CleanupFailureConnection(real_connection)
    holder = [faulty_connection]
    monkeypatch.setattr(job, "_dedicated_connection", lambda _session: holder.pop())

    try:
        with Session(isolated_engine) as session:
            with pytest.raises(RuntimeError, match="unlock outcome unknown"):
                with _schedule_lock(session) as acquired:
                    assert acquired is True

        locked_backend_pid = faulty_connection.locked_backend_pid
        assert locked_backend_pid is not None
        holder.clear()
        faulty_connection = None  # type: ignore[assignment]
        real_connection = None  # type: ignore[assignment]
        gc.collect()

        dirty_connection_checked_in = any(
            candidate is locked_driver_connection for candidate in checked_in_connections
        )
        with isolated_engine.connect() as probe:
            probe_backend_pid = probe.scalar(text("SELECT pg_backend_pid()"))
            inherited_lock = bool(
                probe.scalar(select(func.pg_advisory_unlock(func.hashtext(_SCHEDULE_LOCK))))
            )
            assert probe.scalar(select(func.pg_try_advisory_lock(func.hashtext(_SCHEDULE_LOCK))))
            assert probe.scalar(select(func.pg_advisory_unlock(func.hashtext(_SCHEDULE_LOCK))))
    finally:
        isolated_engine.dispose()

    assert dirty_connection_checked_in is False
    assert probe_backend_pid != locked_backend_pid
    assert inherited_lock is False


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
        "commit",
        "dispatch:102",
        "commit",
        "audit",
        "commit",
    ]
    assert audit_kwargs["detail"] == {"changes": 6, "conflicts": 15}


def test_each_recipient_is_committed_before_a_later_dispatch_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import app.dingtalk.org_sync_job as job

    class DurableJobSession(_JobSession):
        def __init__(self) -> None:
            super().__init__(SimpleNamespace(id=11, public_id="b" * 32), [])
            self.pending: list[int] = []
            self.durable: list[int] = []

        def rollback(self) -> None:
            super().rollback()
            self.pending.clear()

        def commit(self) -> None:
            super().commit()
            self.durable.extend(self.pending)
            self.pending.clear()

    session = DurableJobSession()

    @contextmanager
    def acquired_lock(_session: object):
        yield True

    class Client:
        def list_organization_snapshot(self, *, root_department_ids: tuple[int, ...]):
            del root_department_ids
            return DingTalkOrganizationSnapshot(departments=(), users=())

    def dispatch(*args: object, notification_id: int, **kwargs: object) -> None:
        if notification_id == 102:
            raise job.DingTalkOrganizationSyncError("ORG_DISPATCH_FAILED", "safe")
        session.pending.append(notification_id)

    monkeypatch.setattr(job, "_schedule_lock", acquired_lock)
    monkeypatch.setattr(job, "preview_organization_sync", lambda *args, **kwargs: _preview())
    monkeypatch.setattr(job, "stage_org_sync_notifications", lambda *args, **kwargs: (101, 102))
    monkeypatch.setattr(job, "dispatch_org_sync_notification", dispatch)
    monkeypatch.setattr(job.audit, "record", lambda *args, **kwargs: None)

    assert run_scheduled_org_sync(session, settings=_settings(), client=Client()) == 1  # type: ignore[arg-type]
    assert session.durable == [101]


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
