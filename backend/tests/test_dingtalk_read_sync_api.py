from __future__ import annotations

import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from queue import Queue

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.orm import Session

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.dingtalk.client import (
    DingTalkAttendanceResult,
    DingTalkDirectoryUser,
    get_dingtalk_client,
)
from app.dingtalk.org_sync import take_organization_sync_lock
from app.dingtalk.read_sync import (
    LocalEmployeeIdentity,
    blind_index_dingtalk_user_id,
    match_directory_users,
)
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
from app.models.dingtalk import (
    DingTalkAttendanceSnapshot,
    DingTalkAttendanceSync,
    DingTalkAttendanceSyncStatus,
)
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


class _FakeReadClient:
    def __init__(self) -> None:
        self.directory_calls = 0
        self.attendance_calls: list[tuple[tuple[str, ...], datetime, datetime]] = []
        self.users = (
            DingTalkDirectoryUser("provider-job", "远端王芳", "E001", True),
            DingTalkDirectoryUser("provider-name", " 李雷 ", None, True),
            DingTalkDirectoryUser("provider-ambiguous", "张伟", None, True),
            DingTalkDirectoryUser("provider-unmatched", "钉钉新增人员", None, True),
        )

    def list_directory_users(self):
        self.directory_calls += 1
        return self.users

    def list_attendance_results(self, *, user_ids, start, end):
        self.attendance_calls.append((tuple(user_ids), start, end))
        return (
            DingTalkAttendanceResult(
                "provider-job", 1782864000000, "OnDuty", "Normal", "Normal", None, None, 1
            ),
            DingTalkAttendanceResult(
                "provider-job", 1782864000000, "OffDuty", "Late", "Normal", None, None, 2
            ),
            DingTalkAttendanceResult(
                "provider-name",
                1782864000000,
                "OnDuty",
                "NotSigned",
                "Normal",
                None,
                None,
                3,
            ),
        )


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def _override_session():
        yield db_session

    app.dependency_overrides[get_session] = _override_session
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _settings(*, enabled: bool) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        secret_key="test-secret-key-only-for-tests-not-production",
        encryption_key="test-encryption-key-only-for-tests-not-production",
        cookie_secure=False,
        dingtalk_client_id="test-client-id",
        dingtalk_client_secret="test-client-secret-value",
        dingtalk_agent_id=123,
        dingtalk_corp_id="ding-test-corp",
        dingtalk_read_sync_enabled=enabled,
    )


def _user(session, username: str, role_code: str) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == role_code)).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_employees(session) -> list[Employee]:
    group = OrgUnit(code="SYNC-GROUP", name="集团", type=OrgType.GROUP)
    store = OrgUnit(
        code="SYNC-STORE", name="同步门店", type=OrgType.STORE, parent=group, city="广州"
    )
    employees = [
        Employee(emp_no="E001", name="王芳", org_unit_id=0),
        Employee(emp_no="LOCAL-2", name="李雷", org_unit_id=0),
        Employee(emp_no="E003", name="张伟", org_unit_id=0),
        Employee(emp_no="E004", name="张伟", org_unit_id=0),
    ]
    session.add_all([group, store])
    session.flush()
    for employee in employees:
        employee.org_unit_id = store.id
        session.add(employee)
    session.commit()
    return employees


def test_attendance_period_bounds_stop_at_today_and_reject_future_months():
    from app.routers.dingtalk_sync import _period_bounds

    assert _period_bounds("2026-07", today=date(2026, 7, 21)) == (
        datetime(2026, 7, 1),
        datetime(2026, 7, 21, 23, 59, 59),
    )
    assert _period_bounds("2026-06", today=date(2026, 7, 21))[1] == datetime(
        2026, 6, 30, 23, 59, 59
    )
    with pytest.raises(ValueError, match="future"):
        _period_bounds("2026-08", today=date(2026, 7, 21))


def test_read_sync_is_fail_closed_and_never_calls_provider_when_disabled(client, db_session):
    admin = _user(db_session, "sync-disabled", "GROUP_HR")
    fake = _FakeReadClient()
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(enabled=False)
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/employees/preview", headers=_token(client, admin.username)
    )

    assert response.status_code == 409
    assert fake.directory_calls == 0


def test_employee_preview_and_confirmation_bind_only_safe_matches(client, db_session):
    employees = _seed_employees(db_session)
    admin = _user(db_session, "sync-admin", "GROUP_HR")
    fake = _FakeReadClient()
    settings = _settings(enabled=True)
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)

    preview = client.post("/api/dingtalk/sync/employees/preview", headers=headers)

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["total_remote_users"] == 4
    assert body["matched"] == 2
    assert body["job_number_matches"] == 1
    assert body["unique_name_matches"] == 1
    assert body["ambiguous"] == 1
    assert body["unmatched"] == 1
    assert {item["match_method"] for item in body["items"]} == {
        "JOB_NUMBER",
        "UNIQUE_NAME",
    }
    serialized = preview.text
    assert "provider-job" not in serialized
    assert "provider-name" not in serialized
    assert "user_id" not in serialized
    assert all(employee.dingtalk_user_id_hash is None for employee in employees)

    applied = client.post("/api/dingtalk/sync/employees/apply", headers=headers)

    assert applied.status_code == 200, applied.text
    assert applied.json() == {
        "matched": 2,
        "linked": 2,
        "unchanged": 0,
        "ambiguous": 1,
        "unmatched": 1,
    }
    db_session.expire_all()
    linked = list(
        db_session.scalars(
            select(Employee).where(Employee.dingtalk_user_id_hash.is_not(None))
        ).all()
    )
    assert {employee.emp_no for employee in linked} == {"E001", "LOCAL-2"}
    assert all(len(employee.dingtalk_user_id_hash or "") == 64 for employee in linked)

    listed = client.get("/api/employees", headers=headers)
    assert listed.status_code == 200
    listed_by_no = {item["emp_no"]: item for item in listed.json()["items"]}
    assert listed_by_no["E001"]["dingtalk_linked"] is True
    assert listed_by_no["E003"]["dingtalk_linked"] is False
    assert "dingtalk_user_id_hash" not in listed.text

    audit_text = "\n".join(
        str(row.detail) for row in db_session.scalars(select(AuditLog)).all() if row.detail
    )
    assert "provider-job" not in audit_text
    assert "provider-name" not in audit_text
    assert "远端王芳" not in audit_text


def test_employee_apply_reads_provider_before_shared_lock_and_current_rows(
    client, db_session, monkeypatch
):
    _seed_employees(db_session)
    admin = _user(db_session, "sync-ordered-apply", "GROUP_HR")
    events: list[str] = []

    class OrderedClient(_FakeReadClient):
        def list_directory_users(self):
            events.append("provider")
            return super().list_directory_users()

    fake = OrderedClient()
    settings = _settings(enabled=True)
    from app.main import app
    from app.routers import dingtalk_sync

    original_lock = dingtalk_sync.take_organization_sync_lock
    original_active_employees = dingtalk_sync._active_employees

    def ordered_lock(session):
        events.append("lock")
        return original_lock(session)

    def ordered_active_employees(session, *, for_update=False):
        events.append(f"employees:{for_update}")
        return original_active_employees(session, for_update=for_update)

    monkeypatch.setattr(dingtalk_sync, "take_organization_sync_lock", ordered_lock)
    monkeypatch.setattr(dingtalk_sync, "_active_employees", ordered_active_employees)
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/employees/apply",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    assert events == ["provider", "lock", "employees:True"]


def test_directory_apply_reloads_after_waiting_for_concurrent_identity_change(pg_engine):
    from app.routers.dingtalk_sync import _active_employees

    suffix = uuid.uuid4().hex[:12]
    with Session(pg_engine) as setup:
        store = OrgUnit(
            code=f"DIR-CONC-{suffix}",
            name=f"Directory concurrency {suffix}",
            type=OrgType.STORE,
        )
        setup.add(store)
        setup.flush()
        employee = Employee(
            emp_no=f"DIR-{suffix}",
            name="Concurrent employee",
            org_unit_id=store.id,
        )
        setup.add(employee)
        setup.commit()
        employee_id = employee.id
        store_id = store.id

    key = "test-encryption-key-only-for-tests"
    first_hash = blind_index_dingtalk_user_id("provider-first", key=key)
    remote_after_provider_read = (
        DingTalkDirectoryUser(
            "provider-second",
            "Concurrent employee",
            f"DIR-{suffix}",
            True,
        ),
    )
    backend_pid: Queue[int] = Queue()

    def delayed_apply() -> tuple[int, str | None]:
        with Session(pg_engine) as worker:
            pid = worker.scalar(select(func.pg_backend_pid()))
            assert pid is not None
            backend_pid.put(pid)
            take_organization_sync_lock(worker)
            employees = _active_employees(worker, for_update=True)
            result = match_directory_users(
                [
                    LocalEmployeeIdentity(
                        employee_id=row.id,
                        emp_no=row.emp_no,
                        name=row.name,
                        dingtalk_user_id_hash=row.dingtalk_user_id_hash,
                    )
                    for row in employees
                    if row.id == employee_id
                ],
                remote_after_provider_read,
                encryption_key=key,
            )
            for match in result.matches:
                matched_employee = worker.get(Employee, match.employee_id)
                assert matched_employee is not None
                matched_employee.dingtalk_user_id_hash = match.user_id_hash
            worker.commit()
            current_hash = worker.scalar(
                select(Employee.dingtalk_user_id_hash).where(Employee.id == employee_id)
            )
            return len(result.matches), current_hash

    try:
        with Session(pg_engine) as first_writer:
            take_organization_sync_lock(first_writer)
            current = first_writer.scalars(
                select(Employee).where(Employee.id == employee_id).with_for_update()
            ).one()
            current.dingtalk_user_id_hash = first_hash
            first_writer.flush()

            with ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(delayed_apply)
                worker_pid = backend_pid.get(timeout=5)
                waiting = False
                with Session(pg_engine) as observer:
                    for _attempt in range(100):
                        waiting = bool(
                            observer.scalar(
                                text(
                                    "SELECT EXISTS ("
                                    "SELECT 1 FROM pg_stat_activity "
                                    "WHERE pid = :pid AND wait_event = 'advisory'"
                                    ")"
                                ),
                                {"pid": worker_pid},
                            )
                        )
                        if waiting:
                            break
                        time.sleep(0.02)
                assert waiting, "the second session never waited on the organization lock"
                first_writer.commit()
                matched, final_hash = future.result(timeout=5)

        assert matched == 0
        assert final_hash == first_hash
    finally:
        with Session(pg_engine) as cleanup:
            cleanup.execute(delete(Employee).where(Employee.id == employee_id))
            cleanup.execute(delete(OrgUnit).where(OrgUnit.id == store_id))
            cleanup.commit()


def test_attendance_preview_is_aggregate_only_and_does_not_write_payroll_inputs(
    client, db_session, monkeypatch
):
    _seed_employees(db_session)
    admin = _user(db_session, "attendance-sync-admin", "GROUP_HR")
    fake = _FakeReadClient()
    settings = _settings(enabled=True)
    from app.main import app
    from app.models.attendance import AttendanceRecord
    from app.routers import dingtalk_sync

    period_bounds = dingtalk_sync._period_bounds
    monkeypatch.setattr(
        dingtalk_sync,
        "_period_bounds",
        lambda period: period_bounds(period, today=date(2026, 7, 21)),
    )

    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    assert client.post("/api/dingtalk/sync/employees/apply", headers=headers).status_code == 200

    preview = client.post(
        "/api/dingtalk/sync/attendance/preview",
        headers=headers,
        json={"period": "2026-07"},
    )

    assert preview.status_code == 200, preview.text
    body = preview.json()
    assert body["period"] == "2026-07"
    assert body["matched_employees"] == 2
    assert body["employees_with_records"] == 2
    assert body["total_records"] == 3
    rows = {row["emp_no"]: row for row in body["items"]}
    assert rows["E001"]["normal_count"] == 1
    assert rows["E001"]["late_count"] == 1
    assert rows["LOCAL-2"]["not_signed_count"] == 1
    assert "provider-job" not in preview.text
    assert "work_date" not in preview.text
    assert "user_check_time" not in preview.text
    assert list(db_session.scalars(select(AttendanceRecord)).all()) == []
    assert len(fake.attendance_calls) == 1
    user_ids, start, end = fake.attendance_calls[0]
    assert set(user_ids) == {"provider-job", "provider-name"}
    assert start == datetime(2026, 7, 1, 0, 0, 0)
    assert end == datetime(2026, 7, 21, 23, 59, 59)


def test_attendance_snapshot_returns_cached_rows_without_calling_provider(client, db_session):
    employees = _seed_employees(db_session)
    admin = _user(db_session, "attendance-cache-admin", "GROUP_HR")
    refreshed_at = datetime(2026, 7, 21, 12, 30, tzinfo=UTC)
    sync = DingTalkAttendanceSync(
        period="2026-07",
        status=DingTalkAttendanceSyncStatus.COMPLETED,
        matched_employees=2,
        employees_with_records=1,
        total_records=3,
        ambiguous_directory_users=1,
        unmatched_directory_users=1,
        source_start=datetime(2026, 7, 1, tzinfo=UTC),
        source_end=datetime(2026, 7, 31, 23, 59, 59, tzinfo=UTC),
        refreshed_at=refreshed_at,
    )
    db_session.add(sync)
    db_session.flush()
    db_session.add(
        DingTalkAttendanceSnapshot(
            sync_id=sync.id,
            employee_id=employees[0].id,
            period="2026-07",
            record_count=3,
            normal_count=1,
            late_count=1,
            early_count=0,
            absent_count=0,
            not_signed_count=1,
            other_count=0,
            refreshed_at=refreshed_at,
        )
    )
    db_session.commit()

    response = client.get(
        "/api/dingtalk/sync/attendance/snapshot",
        headers=_token(client, admin.username),
        params={"period": "2026-07"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "COMPLETED"
    assert body["matched_employees"] == 2
    assert body["employees_with_records"] == 1
    assert body["total_records"] == 3
    assert body["items"] == [
        {
            "employee_id": employees[0].id,
            "emp_no": "E001",
            "name": "王芳",
            "record_count": 3,
            "normal_count": 1,
            "late_count": 1,
            "early_count": 0,
            "absent_count": 0,
            "not_signed_count": 1,
            "other_count": 0,
        }
    ]
    assert "provider-job" not in response.text


def test_attendance_refresh_is_queued_and_returns_before_provider_work(client, db_session):
    _seed_employees(db_session)
    admin = _user(db_session, "attendance-refresh-admin", "GROUP_HR")
    calls: list[tuple[str, tuple[int, str]]] = []

    def runner(period: str, actor: tuple[int, str]) -> None:
        calls.append((period, actor))

    from app.main import app
    from app.routers.dingtalk_sync import get_attendance_refresh_runner

    app.dependency_overrides[get_settings] = lambda: _settings(enabled=True)
    app.dependency_overrides[get_attendance_refresh_runner] = lambda: runner

    response = client.post(
        "/api/dingtalk/sync/attendance/refresh",
        headers=_token(client, admin.username),
        json={"period": "2026-07"},
    )

    assert response.status_code == 202, response.text
    assert response.json()["status"] == "QUEUED"
    assert calls == [("2026-07", (admin.id, admin.username))]
    state = db_session.scalars(
        select(DingTalkAttendanceSync).where(DingTalkAttendanceSync.period == "2026-07")
    ).one()
    assert state.status == DingTalkAttendanceSyncStatus.QUEUED


def test_attendance_refresh_worker_atomically_replaces_cached_aggregates(db_session, monkeypatch):
    employees = _seed_employees(db_session)
    admin = _user(db_session, "attendance-worker-admin", "GROUP_HR")
    sync = DingTalkAttendanceSync(
        period="2026-07",
        status=DingTalkAttendanceSyncStatus.QUEUED,
        requested_by_user_id=admin.id,
    )
    db_session.add(sync)
    db_session.commit()
    fake = _FakeReadClient()

    from sqlalchemy.orm import sessionmaker

    from app.routers import dingtalk_sync as sync_router

    worker_sessions = sessionmaker(
        bind=db_session.get_bind(),
        future=True,
        join_transaction_mode="create_savepoint",
    )
    monkeypatch.setattr(sync_router, "SessionLocal", worker_sessions)
    monkeypatch.setattr(sync_router, "get_settings", lambda: _settings(enabled=True))
    monkeypatch.setattr(
        sync_router.DingTalkClient,
        "from_settings",
        classmethod(lambda _cls, _settings_value: fake),
    )

    sync_router._run_attendance_refresh("2026-07", (admin.id, admin.username))

    db_session.expire_all()
    refreshed = db_session.scalars(
        select(DingTalkAttendanceSync).where(DingTalkAttendanceSync.period == "2026-07")
    ).one()
    assert refreshed.status == DingTalkAttendanceSyncStatus.COMPLETED
    assert refreshed.matched_employees == 2
    assert refreshed.employees_with_records == 2
    assert refreshed.total_records == 3
    rows = list(
        db_session.scalars(
            select(DingTalkAttendanceSnapshot).where(DingTalkAttendanceSnapshot.period == "2026-07")
        ).all()
    )
    rows_by_employee = {row.employee_id: row for row in rows}
    assert rows_by_employee[employees[0].id].late_count == 1
    assert rows_by_employee[employees[1].id].not_signed_count == 1
    assert len(fake.attendance_calls) == 1


def test_store_manager_cannot_run_global_dingtalk_sync(client, db_session):
    manager = _user(db_session, "store-sync-user", "STORE_MANAGER")
    fake = _FakeReadClient()
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(enabled=True)
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/employees/preview", headers=_token(client, manager.username)
    )

    assert response.status_code == 403
    assert fake.directory_calls == 0
