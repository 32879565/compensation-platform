from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

import app.dingtalk.org_sync as org_sync
from app.auth.bootstrap import seed_rbac
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.dingtalk.client import (
    DingTalkDepartment,
    DingTalkOrganizationSnapshot,
    DingTalkOrganizationUser,
    get_dingtalk_client,
)
from app.dingtalk.org_sync import preview_organization_sync
from app.dingtalk.read_sync import blind_index_dingtalk_user_id
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    DingTalkOrgSyncAction,
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
    DingTalkOrgSyncTrigger,
)
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


class _FakeOrganizationClient:
    def __init__(
        self,
        users: tuple[DingTalkOrganizationUser, ...] | None = None,
        *,
        departments: tuple[DingTalkDepartment, ...] | None = None,
        snapshots: tuple[DingTalkOrganizationSnapshot, ...] | None = None,
    ) -> None:
        self.calls = 0
        self.root_department_id_calls: list[tuple[int, ...] | None] = []
        self.users = users
        self.departments = departments
        self.snapshots = snapshots

    def list_organization_snapshot(
        self,
        *,
        root_department_ids: tuple[int, ...] | None = None,
    ) -> DingTalkOrganizationSnapshot:
        self.calls += 1
        self.root_department_id_calls.append(root_department_ids)
        if self.snapshots is not None:
            return self.snapshots[min(self.calls - 1, len(self.snapshots) - 1)]
        return DingTalkOrganizationSnapshot(
            departments=(
                self.departments
                if self.departments is not None
                else (
                    DingTalkDepartment(10, 1, "潮发运营中心"),
                    DingTalkDepartment(101, 10, "天河店"),
                )
            ),
            users=(
                self.users
                if self.users is not None
                else (
                    DingTalkOrganizationUser(
                        "provider-manager",
                        "店长甲",
                        "M001",
                        "店长",
                        True,
                        (101,),
                    ),
                    DingTalkOrganizationUser(
                        "provider-kitchen",
                        "厨管乙",
                        "M002",
                        "厨房经理",
                        True,
                        (101,),
                    ),
                )
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


def _settings(*, root_mappings: str = "10:DIRECT-GROUP") -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        secret_key="test-secret-key-only-for-tests-not-production",
        encryption_key="test-encryption-key-only-for-tests-not-production",
        cookie_secure=False,
        dingtalk_client_id="test-client-id",
        dingtalk_client_secret=SecretStr("test-client-secret-value"),
        dingtalk_agent_id=123,
        dingtalk_corp_id="ding-test-corp",
        dingtalk_read_sync_enabled=True,
        dingtalk_org_root_mappings=root_mappings,
    )


def _expected_root_config_hash(root_mappings: tuple[tuple[int, str], ...]) -> str:
    payload = json.dumps(sorted(root_mappings), separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


def _group_hr(session, username: str) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == "GROUP_HR")).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user


def _store_manager(session, username: str) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.commit()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _seed_store_and_managers(session) -> OrgUnit:
    group = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    store = OrgUnit(
        code="DIRECT-STORE",
        name="天河店",
        type=OrgType.STORE,
        parent=group,
        city="广州",
    )
    session.add_all([group, store])
    session.flush()
    session.add_all(
        [
            Employee(
                emp_no="M001",
                name="店长甲",
                org_unit_id=store.id,
                department=Department.DINING,
                position_title="店长",
            ),
            Employee(
                emp_no="M002",
                name="厨管乙",
                org_unit_id=store.id,
                department=Department.KITCHEN,
                position_title="厨房经理",
            ),
        ]
    )
    session.commit()
    return store


def test_organization_preview_stages_safe_store_and_reviewer_changes_without_applying(
    client, db_session
):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-preview")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["remote_stores"] == 1
    assert body["local_stores"] == 1
    assert body["ready_stores"] == 1
    assert body["store_conflicts"] == 0
    assert body["ready_reviewers"] == 2
    assert body["reviewer_conflicts"] == 0
    assert body["trigger"] == "MANUAL"
    assert body["created_at"] == body["last_checked_at"]
    assert body["expires_at"] > datetime.now(UTC).isoformat()
    assert {item["department"] for item in body["reviewer_items"]} == {
        "DINING",
        "KITCHEN",
    }
    assert {item["action"] for item in body["reviewer_items"]} == {"ASSIGN"}
    assert body["store_items"][0]["action"] == "LINK"
    assert body["store_items"][0]["match_method"] == "EXACT_RELATIVE_PATH"
    assert "provider-manager" not in response.text
    assert "provider-kitchen" not in response.text
    assert "remote_user_id" not in response.text

    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    assert db_session.scalars(select(UserReviewScope)).all() == []
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == body["batch_id"])
    ).one()
    assert batch.root_config_hash == _expected_root_config_hash(((10, "DIRECT-GROUP"),))
    staged_items = db_session.scalars(select(DingTalkOrgSyncItem)).all()
    assert len(staged_items) == 3
    assert {
        (item.kind.value, item.action, tuple(item.change_fields), item.proposed_org_type)
        for item in staged_items
    } == {
        (
            DingTalkOrgSyncItemKind.STORE.value,
            DingTalkOrgSyncAction.LINK,
            ("dingtalk_dept_id",),
            OrgType.STORE,
        ),
        (
            DingTalkOrgSyncItemKind.REVIEWER.value,
            DingTalkOrgSyncAction.ASSIGN_SCOPE,
            ("reviewer_scope",),
            None,
        ),
    }
    staged_values = "|".join(
        str(getattr(item, column.name))
        for item in staged_items
        for column in DingTalkOrgSyncItem.__table__.columns
    )
    assert "provider-manager" not in staged_values
    assert "provider-kitchen" not in staged_values
    assert "店长甲" not in staged_values
    assert "厨管乙" not in staged_values
    assert fake.calls == 1


def test_organization_preview_persists_the_configured_root_mapping_hash(client, db_session):
    _seed_store_and_managers(db_session)
    db_session.add(OrgUnit(code="REGION-B", name="区域乙", type=OrgType.REGION))
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-root-fingerprint")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(
        root_mappings="20:REGION-B,10:DIRECT-GROUP"
    )
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(
            DingTalkOrgSyncBatch.public_id == response.json()["batch_id"]
        )
    ).one()
    assert batch.root_config_hash == _expected_root_config_hash(
        ((20, "REGION-B"), (10, "DIRECT-GROUP"))
    )


def test_organization_preview_and_confirmation_read_only_configured_roots(client, db_session):
    _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-scoped-provider-roots")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(root_mappings="10:DIRECT-GROUP")
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)

    preview_response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text

    apply_response = client.post(
        f"/api/dingtalk/sync/organization/{preview_response.json()['batch_id']}/apply",
        headers=headers,
    )

    assert apply_response.status_code == 200, apply_response.text
    assert fake.root_department_id_calls == [(10,), (10,)]
    assert all(1 not in root_ids for root_ids in fake.root_department_id_calls if root_ids)


def test_organization_apply_missing_batch_does_not_read_provider(client, db_session):
    admin = _group_hr(db_session, "org-sync-missing-apply")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        f"/api/dingtalk/sync/organization/{'0' * 32}/apply",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 404
    assert fake.calls == 0


def test_organization_sync_is_hr_only_and_rejects_before_provider_read(client, db_session):
    manager = _store_manager(db_session, "org-sync-denied")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, manager.username),
    )

    assert response.status_code == 403
    assert fake.calls == 0
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == 0


def test_organization_apply_rejects_changed_preview_without_partial_writes(client, db_session):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-stale")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=headers,
    )
    batch_id = preview_response.json()["batch_id"]
    employee = db_session.scalars(select(Employee).where(Employee.emp_no == "M001")).one()
    employee.name = "店长甲（已调整）"
    db_session.commit()

    response = client.post(
        f"/api/dingtalk/sync/organization/{batch_id}/apply",
        headers=headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    assert db_session.scalars(select(UserReviewScope)).all() == []
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == batch_id)
    ).one()
    assert batch.status.value == "STALE"
    assert all(
        item.remote_user_id_hash is None
        for item in db_session.scalars(
            select(DingTalkOrgSyncItem).where(DingTalkOrgSyncItem.batch_id == batch.id)
        ).all()
    )


def test_organization_apply_clears_departed_manager_scope_fail_closed(client, db_session):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-movement")
    employees = db_session.scalars(select(Employee).order_by(Employee.emp_no)).all()
    manager_role = db_session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    managers: list[User] = []
    for index, employee in enumerate(employees, start=1):
        provider_id = f"departed-provider-{index}"
        provider_hash = blind_index_dingtalk_user_id(
            provider_id,
            key=_settings().encryption_key,
        )
        employee.dingtalk_user_id_hash = provider_hash
        manager = User(
            username=f"departed-manager-{index}",
            password_hash=hash_password("StrongPass123!"),
            employee_id=employee.id,
            dingtalk_user_id=provider_id,
            dingtalk_user_id_hash=provider_hash,
            login_enabled=False,
        )
        db_session.add(manager)
        db_session.flush()
        managers.append(manager)
        db_session.add_all(
            [
                UserRole(user_id=manager.id, role_id=manager_role.id),
                UserReviewScope(
                    user_id=manager.id,
                    org_unit_id=store.id,
                    department=employee.department,
                ),
            ]
        )
    db_session.commit()
    fake = _FakeOrganizationClient(users=())
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["ready_reviewers"] == 2
    assert preview["reviewer_conflicts"] == 0
    assert {(item["action"], item["match_method"]) for item in preview["reviewer_items"]} == {
        ("REMOVE", "REMOVE_MISSING_MANAGER")
    }
    assert {item["current_reviewer_name"] for item in preview["reviewer_items"]} == {
        manager.username for manager in managers
    }
    staged_reviewers = db_session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.REVIEWER
        )
    ).all()
    assert {
        (item.action, tuple(item.change_fields), item.proposed_org_type)
        for item in staged_reviewers
    } == {(DingTalkOrgSyncAction.REMOVE_SCOPE, ("reviewer_scope",), None)}

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["unresolved"] == 0
    assert (
        db_session.scalars(
            select(UserReviewScope).where(UserReviewScope.org_unit_id == store.id)
        ).all()
        == []
    )
    assert all(db_session.get(User, manager.id) is not None for manager in managers)


def test_organization_apply_rejects_expired_preview(client, db_session):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-expired")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=headers,
    )
    batch_id = preview_response.json()["batch_id"]
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == batch_id)
    ).one()
    batch.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    db_session.commit()

    response = client.post(
        f"/api/dingtalk/sync/organization/{batch_id}/apply",
        headers=headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    assert db_session.get(DingTalkOrgSyncBatch, batch.id).status.value == "STALE"


def test_organization_apply_links_store_and_replaces_reviewer_scopes_idempotently(
    client, db_session
):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-apply")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=headers,
    )
    assert preview_response.status_code == 200, preview_response.text
    batch_id = preview_response.json()["batch_id"]

    response = client.post(
        f"/api/dingtalk/sync/organization/{batch_id}/apply",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json() == {
        "applied_stores": 1,
        "applied_reviewers": 2,
        "unresolved": 0,
        "already_applied": False,
    }
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id == 101
    employees = db_session.scalars(
        select(Employee).where(Employee.emp_no.in_(("M001", "M002"))).order_by(Employee.emp_no)
    ).all()
    accounts = db_session.scalars(
        select(User).where(User.employee_id.in_([employee.id for employee in employees]))
    ).all()
    assert len(accounts) == 2
    assert all(account.status == "ACTIVE" for account in accounts)
    assert all(account.login_enabled is False for account in accounts)
    assert {account.dingtalk_user_id for account in accounts} == {
        "provider-manager",
        "provider-kitchen",
    }
    assert all(account.dingtalk_user_id_hash for account in accounts)
    manager_role = db_session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    assert (
        db_session.scalar(
            select(func.count())
            .select_from(UserRole)
            .where(
                UserRole.user_id.in_([account.id for account in accounts]),
                UserRole.role_id == manager_role.id,
            )
        )
        == 2
    )
    assert {
        (scope.user_id, scope.department)
        for scope in db_session.scalars(select(UserReviewScope)).all()
    } == {
        (account.id, employee.department)
        for account in accounts
        for employee in employees
        if account.employee_id == employee.id
    }
    items = db_session.scalars(select(DingTalkOrgSyncItem)).all()
    assert all(item.remote_user_id_hash is None for item in items)
    assert all(
        (item.applied_identity_proof is not None) == (item.kind.value == "REVIEWER")
        for item in items
    )
    assert all(item.status == DingTalkOrgSyncItemStatus.APPLIED for item in items)
    audit_entry = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "dingtalk.organization.apply")
    ).one()
    assert "provider-manager" not in str(audit_entry.detail)
    assert "provider-kitchen" not in str(audit_entry.detail)
    assert "店长甲" not in str(audit_entry.detail)
    assert "厨管乙" not in str(audit_entry.detail)
    assert audit_entry.detail is not None
    assert audit_entry.detail["store_changes"][0]["before"] == {
        "org_unit_id": store.id,
        "parent_org_unit_id": store.parent_id,
        "status": "ACTIVE",
    }
    assert {
        tuple(change["before_user_ids"]) for change in audit_entry.detail["reviewer_changes"]
    } == {()}
    assert all(
        len(change["after_user_ids"]) == 1 for change in audit_entry.detail["reviewer_changes"]
    )

    user_count = db_session.scalar(select(func.count()).select_from(User))
    scope_count = db_session.scalar(select(func.count()).select_from(UserReviewScope))
    repeated = client.post(
        f"/api/dingtalk/sync/organization/{batch_id}/apply",
        headers=headers,
    )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json() == {
        "applied_stores": 1,
        "applied_reviewers": 2,
        "unresolved": 0,
        "already_applied": True,
    }
    assert db_session.scalar(select(func.count()).select_from(User)) == user_count
    assert db_session.scalar(select(func.count()).select_from(UserReviewScope)) == scope_count
    assert fake.calls == 2


@pytest.mark.parametrize(
    ("local_name", "stable_department_id", "expected_change_fields"),
    [
        ("天河店", None, ()),
        ("天河旧店", 101, ("name",)),
    ],
)
def test_organization_sync_activates_or_updates_historical_store(
    client,
    db_session,
    local_name,
    stable_department_id,
    expected_change_fields,
):
    store = _seed_store_and_managers(db_session)
    store.name = local_name
    store.status = "HISTORICAL"
    store.dingtalk_dept_id = stable_department_id
    db_session.commit()
    admin = _group_hr(db_session, f"org-sync-activate-{len(expected_change_fields)}")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["store_items"][0]["action"] == "ACTIVATE"
    staged_store = db_session.scalars(
        select(DingTalkOrgSyncItem).where(DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.STORE)
    ).one()
    assert (staged_store.action, tuple(staged_store.change_fields)) == (
        DingTalkOrgSyncAction.ACTIVATE,
        expected_change_fields,
    )
    assert staged_store.proposed_org_type == OrgType.STORE

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    db_session.expire_all()
    updated = db_session.get(OrgUnit, store.id)
    assert updated.status == "ACTIVE"
    assert updated.name == "天河店"
    assert updated.dingtalk_dept_id == 101


def test_organization_sync_conflicts_when_two_departments_target_same_local_store(
    client, db_session
):
    store = _seed_store_and_managers(db_session)
    store.dingtalk_dept_id = 101
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-duplicate-store-target")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(101, 10, "天河新店"),
            DingTalkDepartment(102, 10, "天河店"),
        )
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)

    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    conflicting_stores = [
        item for item in preview["store_items"] if item["remote_department_id"] in {101, 102}
    ]
    assert len(conflicting_stores) == 2
    assert {item["action"] for item in conflicting_stores} == {"CREATE", "UPDATE"}
    assert {item["proposed_org_unit_id"] for item in conflicting_stores} == {store.id, None}
    stores_by_remote_id = {item["remote_department_id"]: item for item in conflicting_stores}
    assert stores_by_remote_id[101]["status"] == "READY"
    assert stores_by_remote_id[101]["conflict_code"] is None
    assert stores_by_remote_id[102]["status"] == "CONFLICT"
    assert stores_by_remote_id[102]["conflict_code"] == "ORG_PATH_AMBIGUOUS"
    assert preview["store_conflicts"] == 1
    assert preview["reviewer_conflicts"] > 0

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    unchanged = db_session.get(OrgUnit, store.id)
    assert unchanged.name == "天河店"
    assert unchanged.dingtalk_dept_id == 101
    assert db_session.scalars(select(UserReviewScope)).all() == []


def test_organization_apply_rejects_same_name_store_created_after_create_preview(
    client, db_session
):
    existing_store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-concurrent-same-name")
    fake = _FakeOrganizationClient(
        users=(
            DingTalkOrganizationUser("provider-manager", "店长甲", "M001", "店长", True, (102,)),
            DingTalkOrganizationUser(
                "provider-kitchen", "厨管乙", "M002", "厨房经理", True, (102,)
            ),
        ),
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(102, 10, "珠江新城店"),
        ),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    create_item = next(
        item for item in preview["store_items"] if item["remote_department_id"] == 102
    )
    assert create_item["action"] == "CREATE"
    assert create_item["status"] == "READY"
    assert preview["reviewer_conflicts"] == 0

    concurrent_store = OrgUnit(
        code="MANUAL-ZHUJIANG",
        name="珠江新城店",
        type=OrgType.STORE,
        parent_id=existing_store.parent_id,
        city="广州",
    )
    db_session.add(concurrent_store)
    db_session.commit()

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    same_name_stores = db_session.scalars(
        select(OrgUnit).where(
            OrgUnit.type == OrgType.STORE,
            OrgUnit.name == "珠江新城店",
            OrgUnit.is_deleted.is_(False),
        )
    ).all()
    assert [store.id for store in same_name_stores] == [concurrent_store.id]
    assert (
        db_session.scalars(
            select(OrgUnit).where(
                (OrgUnit.code == "DINGTALK-102") | (OrgUnit.dingtalk_dept_id == 102)
            )
        ).all()
        == []
    )
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == preview["batch_id"])
    ).one()
    assert batch.status.value == "STALE"


def test_organization_sync_creates_remote_only_store_and_assigns_both_reviewers(client, db_session):
    existing_store = _seed_store_and_managers(db_session)
    group_id = existing_store.parent_id
    db_session.add_all(
        [
            Employee(
                emp_no="M003",
                name="新店店长",
                org_unit_id=existing_store.id,
                department=Department.DINING,
                position_title="店长",
            ),
            Employee(
                emp_no="M004",
                name="新店厨管",
                org_unit_id=existing_store.id,
                department=Department.KITCHEN,
                position_title="厨房经理",
            ),
        ]
    )
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-create-store")
    users = (
        DingTalkOrganizationUser("provider-manager", "店长甲", "M001", "店长", True, (101,)),
        DingTalkOrganizationUser("provider-kitchen", "厨管乙", "M002", "厨房经理", True, (101,)),
        DingTalkOrganizationUser("provider-new-manager", "新店店长", "M003", "店长", True, (102,)),
        DingTalkOrganizationUser(
            "provider-new-kitchen", "新店厨管", "M004", "厨房经理", True, (102,)
        ),
    )
    fake = _FakeOrganizationClient(
        users=users,
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(101, 10, "天河店"),
            DingTalkDepartment(102, 10, "珠江新城店"),
        ),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    create_item = next(item for item in preview["store_items"] if item["action"] == "CREATE")
    assert create_item["remote_department_name"] == "珠江新城店"
    assert create_item["proposed_parent_org_unit_id"] == group_id
    assert preview["reviewer_conflicts"] == 0
    staged_create = db_session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.STORE,
            DingTalkOrgSyncItem.remote_department_id == 102,
        )
    ).one()
    assert staged_create.action == DingTalkOrgSyncAction.CREATE
    assert staged_create.change_fields == []
    assert staged_create.proposed_org_type == OrgType.STORE

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    stores = db_session.scalars(
        select(OrgUnit).where(OrgUnit.type == OrgType.STORE).order_by(OrgUnit.id)
    ).all()
    assert len(stores) == 2
    created = next(store for store in stores if store.dingtalk_dept_id == 102)
    assert created.code == "DINGTALK-102"
    assert created.name == "珠江新城店"
    assert created.parent_id == group_id
    assert created.status == "ACTIVE"
    assert created.city is None
    assert {
        scope.department
        for scope in db_session.scalars(
            select(UserReviewScope).where(UserReviewScope.org_unit_id == created.id)
        ).all()
    } == {Department.DINING, Department.KITCHEN}
    created_items = db_session.scalars(
        select(DingTalkOrgSyncItem).where(DingTalkOrgSyncItem.remote_department_id == 102)
    ).all()
    assert len(created_items) == 3
    assert all(item.status == DingTalkOrgSyncItemStatus.APPLIED for item in created_items)
    assert all(item.proposed_org_unit_id == created.id for item in created_items)


def test_organization_sync_surfaces_local_only_store_and_clears_both_scopes(client, db_session):
    visible_store = _seed_store_and_managers(db_session)
    hidden_store = OrgUnit(
        code="HIDDEN-STORE",
        name="越秀店",
        type=OrgType.STORE,
        parent_id=visible_store.parent_id,
        city="广州",
        status="ACTIVE",
    )
    db_session.add(hidden_store)
    db_session.flush()
    seed_rbac(db_session)
    manager_role = db_session.scalars(select(Role).where(Role.code == "STORE_MANAGER")).one()
    hidden_reviewers: list[User] = []
    for department in (Department.DINING, Department.KITCHEN):
        reviewer = User(
            username=f"hidden-{department.value.lower()}",
            password_hash=hash_password("StrongPass123!"),
            login_enabled=False,
        )
        db_session.add(reviewer)
        db_session.flush()
        hidden_reviewers.append(reviewer)
        db_session.add_all(
            [
                UserRole(user_id=reviewer.id, role_id=manager_role.id),
                UserReviewScope(
                    user_id=reviewer.id,
                    org_unit_id=hidden_store.id,
                    department=department,
                ),
            ]
        )
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-local-coverage")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    coverage = next(item for item in preview["store_items"] if item["action"] == "DEACTIVATE")
    assert coverage["remote_department_id"] is None
    assert coverage["proposed_org_unit_id"] == hidden_store.id
    assert coverage["match_method"] == "MISSING_IN_DINGTALK"
    clears = [
        item
        for item in preview["reviewer_items"]
        if item["match_method"] == "CLEAR_UNCOVERED_STORE"
    ]
    assert len(clears) == 2
    assert {item["action"] for item in clears} == {"REMOVE"}
    assert preview["reviewer_conflicts"] == 0
    staged_hidden_items = db_session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.proposed_org_unit_id == hidden_store.id
        )
    ).all()
    assert {
        (item.kind, item.action, tuple(item.change_fields), item.proposed_org_type)
        for item in staged_hidden_items
    } == {
        (
            DingTalkOrgSyncItemKind.STORE,
            DingTalkOrgSyncAction.DEACTIVATE,
            (),
            OrgType.STORE,
        ),
        (
            DingTalkOrgSyncItemKind.REVIEWER,
            DingTalkOrgSyncAction.REMOVE_SCOPE,
            ("reviewer_scope",),
            None,
        ),
    }

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["unresolved"] == 0
    db_session.expire_all()
    assert db_session.get(OrgUnit, hidden_store.id).status == "HISTORICAL"
    assert (
        db_session.scalars(
            select(UserReviewScope).where(UserReviewScope.org_unit_id == hidden_store.id)
        ).all()
        == []
    )


def test_organization_sync_rejects_weak_name_only_manager_match(client, db_session):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-weak-name")
    fake = _FakeOrganizationClient(
        users=(
            DingTalkOrganizationUser("weak-provider-manager", "店长甲", None, "店长", True, (101,)),
            DingTalkOrganizationUser(
                "provider-kitchen", "厨管乙", "M002", "厨房经理", True, (101,)
            ),
        )
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    weak = next(item for item in preview["reviewer_items"] if item["department"] == "DINING")
    assert weak["action"] == "CONFLICT"
    assert weak["match_method"] == "UNIQUE_NAME"
    assert weak["conflict_code"] == "WEAK_NAME_MATCH"
    staged_weak = db_session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.REVIEWER,
            DingTalkOrgSyncItem.department == Department.DINING,
        )
    ).one()
    assert staged_weak.action == DingTalkOrgSyncAction.NO_CHANGE
    assert staged_weak.change_fields == []
    assert staged_weak.proposed_org_type is None

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    assert db_session.scalars(select(UserReviewScope)).all() == []


def test_organization_sync_conflicts_same_manager_covering_multiple_stores(client, db_session):
    first_store = _seed_store_and_managers(db_session)
    second_store = OrgUnit(
        code="SECOND-STORE",
        name="越秀店",
        type=OrgType.STORE,
        parent_id=first_store.parent_id,
        city="广州",
    )
    db_session.add(second_store)
    db_session.flush()
    db_session.add(
        Employee(
            emp_no="M003",
            name="越秀厨管",
            org_unit_id=second_store.id,
            department=Department.KITCHEN,
            position_title="厨房经理",
        )
    )
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-multi-store-manager")
    fake = _FakeOrganizationClient(
        users=(
            DingTalkOrganizationUser(
                "provider-manager", "店长甲", "M001", "店长", True, (101, 102)
            ),
            DingTalkOrganizationUser(
                "provider-kitchen", "厨管乙", "M002", "厨房经理", True, (101,)
            ),
            DingTalkOrganizationUser(
                "provider-kitchen-two", "越秀厨管", "M003", "厨房经理", True, (102,)
            ),
        ),
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(101, 10, "天河店"),
            DingTalkDepartment(102, 10, "越秀店"),
        ),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    duplicated = [
        item
        for item in preview["reviewer_items"]
        if item["conflict_code"] == "MANAGER_ASSIGNED_MULTIPLE_STORES"
    ]
    assert len(duplicated) == 2
    assert {item["action"] for item in duplicated} == {"CONFLICT"}
    staged_duplicates = db_session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.conflict_code == "MANAGER_ASSIGNED_MULTIPLE_STORES"
        )
    ).all()
    assert len(staged_duplicates) == 2
    assert all(item.action == DingTalkOrgSyncAction.NO_CHANGE for item in staged_duplicates)
    assert all(item.change_fields == [] for item in staged_duplicates)
    assert all(item.proposed_org_type is None for item in staged_duplicates)

    response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, first_store.id).dingtalk_dept_id is None
    assert db_session.get(OrgUnit, second_store.id).dingtalk_dept_id is None


def test_organization_apply_marks_batch_stale_when_fresh_dingtalk_snapshot_changes(
    client, db_session
):
    store = _seed_store_and_managers(db_session)
    admin = _group_hr(db_session, "org-sync-provider-stale")
    original = _FakeOrganizationClient().list_organization_snapshot()
    changed = DingTalkOrganizationSnapshot(
        departments=original.departments,
        users=(
            *original.users[:-1],
            DingTalkOrganizationUser(
                "provider-kitchen",
                "厨管乙",
                "M002",
                "厨房经理（已调动）",
                True,
                (101,),
            ),
        ),
    )
    fake = _FakeOrganizationClient(snapshots=(original, changed))
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)
    batch_id = preview_response.json()["batch_id"]

    response = client.post(f"/api/dingtalk/sync/organization/{batch_id}/apply", headers=headers)

    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == batch_id)
    ).one()
    assert batch.status.value == "STALE"
    assert all(
        item.remote_user_id_hash is None
        for item in db_session.scalars(
            select(DingTalkOrgSyncItem).where(DingTalkOrgSyncItem.batch_id == batch.id)
        ).all()
    )


def test_organization_preview_stages_region_create_and_authority_deactivate(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    old_region = OrgUnit(code="OLD-REGION", name="旧区", type=OrgType.REGION, parent=anchor)
    old_store = OrgUnit(code="OLD-STORE", name="旧店", type=OrgType.STORE, parent=old_region)
    outside_anchor = OrgUnit(code="OUTSIDE-GROUP", name="外部集团", type=OrgType.GROUP)
    outside_store = OrgUnit(
        code="OUTSIDE-STORE", name="外部门店", type=OrgType.STORE, parent=outside_anchor
    )
    db_session.add_all([anchor, old_region, old_store, outside_anchor, outside_store])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-region-create")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(110, 10, "广州一区"),
            DingTalkDepartment(111, 110, "珠江新城店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["remote_regions"] == 1
    assert body["local_regions"] == 1
    assert body["ready_regions"] == 2
    assert body["region_conflicts"] == 0
    assert body["warnings"] == 0
    created_region = next(item for item in body["region_items"] if item["action"] == "CREATE")
    assert created_region == {
        "id": created_region["id"],
        "kind": "REGION",
        "action": "CREATE",
        "change_fields": [],
        "remote_department_id": 110,
        "remote_department_name": "广州一区",
        "remote_department_path": "广州一区",
        "match_method": "NO_LOCAL_PATH_MATCH",
        "proposed_org_unit_id": None,
        "proposed_org_unit_name": "广州一区",
        "proposed_parent_org_unit_id": anchor.id,
        "proposed_parent_org_unit_name": anchor.name,
        "status": "READY",
        "conflict_code": None,
    }
    assert (
        next(item for item in body["store_items"] if item["proposed_org_unit_id"] == old_store.id)[
            "action"
        ]
        == "DEACTIVATE"
    )
    assert body["store_conflicts"] == 0
    assert all(item["proposed_org_unit_id"] != outside_store.id for item in body["store_items"])


def test_organization_preview_matches_same_name_stores_by_full_relative_path(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    east = OrgUnit(code="EAST", name="东区", type=OrgType.REGION, parent=anchor)
    west = OrgUnit(code="WEST", name="西区", type=OrgType.REGION, parent=anchor)
    east_store = OrgUnit(code="EAST-CENTRAL", name="中心店", type=OrgType.STORE, parent=east)
    west_store = OrgUnit(code="WEST-CENTRAL", name="中心店", type=OrgType.STORE, parent=west)
    db_session.add_all([anchor, east, west, east_store, west_store])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-full-path")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(110, 10, "东区"),
            DingTalkDepartment(120, 10, "西区"),
            DingTalkDepartment(210, 110, "中心店"),
            DingTalkDepartment(220, 120, "中心店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    store_items = {item["remote_department_id"]: item for item in response.json()["store_items"]}
    assert store_items[210]["proposed_org_unit_id"] == east_store.id
    assert store_items[220]["proposed_org_unit_id"] == west_store.id
    assert {store_items[210]["match_method"], store_items[220]["match_method"]} == {
        "EXACT_RELATIVE_PATH"
    }


def test_organization_preview_conflicts_on_ambiguous_local_relative_path(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    region = OrgUnit(code="SOUTH", name="南区", type=OrgType.REGION, parent=anchor)
    first = OrgUnit(code="SOUTH-FIRST", name="中心店", type=OrgType.STORE, parent=region)
    second = OrgUnit(code="SOUTH-SECOND", name="中心店", type=OrgType.STORE, parent=region)
    db_session.add_all([anchor, region, first, second])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-path-ambiguous")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(110, 10, "南区"),
            DingTalkDepartment(210, 110, "中心店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    remote_store = next(
        item for item in response.json()["store_items"] if item["remote_department_id"] == 210
    )
    assert remote_store["status"] == "CONFLICT"
    assert remote_store["conflict_code"] == "ORG_PATH_AMBIGUOUS"
    assert response.json()["store_conflicts"] > 0


def test_organization_preview_update_lists_exact_name_and_parent_fields(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    east = OrgUnit(code="MOVE-EAST", name="东区", type=OrgType.REGION, parent=anchor)
    west = OrgUnit(code="MOVE-WEST", name="西区", type=OrgType.REGION, parent=anchor)
    store = OrgUnit(
        code="MOVING-STORE",
        name="旧门店",
        type=OrgType.STORE,
        parent=east,
        dingtalk_dept_id=210,
    )
    db_session.add_all([anchor, east, west, store])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-update-fields")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(110, 10, "东区"),
            DingTalkDepartment(120, 10, "西区"),
            DingTalkDepartment(210, 120, "新门店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    item = next(
        item for item in response.json()["store_items"] if item["remote_department_id"] == 210
    )
    assert item["action"] == "UPDATE"
    assert item["change_fields"] == ["name", "parent_id"]
    assert item["proposed_parent_org_unit_id"] == west.id
    staged = db_session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.remote_department_id == 210,
            DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.STORE,
        )
    ).one()
    assert staged.action == DingTalkOrgSyncAction.UPDATE
    assert staged.match_method == "STABLE_DEPARTMENT_ID"
    assert "|" not in staged.match_method


@pytest.mark.parametrize(
    "anchor",
    [
        None,
        OrgUnit(code="DIRECT-GROUP", name="已停用", type=OrgType.REGION, status="HISTORICAL"),
        OrgUnit(code="DIRECT-GROUP", name="门店锚点", type=OrgType.STORE),
    ],
    ids=["missing", "inactive", "store"],
)
def test_organization_preview_fails_closed_for_invalid_configured_anchor(
    client, db_session, anchor
):
    if anchor is not None:
        db_session.add(anchor)
        db_session.commit()
    admin = _group_hr(db_session, f"org-sync-invalid-anchor-{anchor.type if anchor else 'missing'}")
    fake = _FakeOrganizationClient(users=())
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == 0


def test_scheduled_organization_preview_reuses_only_complete_unchanged_baseline(db_session):
    _seed_store_and_managers(db_session)
    snapshot = _FakeOrganizationClient().list_organization_snapshot(root_department_ids=(10,))
    first_checked_at = datetime(2026, 7, 22, 9, 0, tzinfo=UTC)

    first = preview_organization_sync(
        db_session,
        snapshot,
        encryption_key=_settings().encryption_key,
        actor=None,
        root_mappings=((10, "DIRECT-GROUP"),),
        trigger=DingTalkOrgSyncTrigger.SCHEDULED,
        now=first_checked_at,
    )
    first_batch_count = db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch))
    first_item_count = db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncItem))

    reused = preview_organization_sync(
        db_session,
        snapshot,
        encryption_key=_settings().encryption_key,
        actor=None,
        root_mappings=((10, "DIRECT-GROUP"),),
        trigger=DingTalkOrgSyncTrigger.SCHEDULED,
        now=first_checked_at + timedelta(minutes=1),
    )

    assert reused.batch_id == first.batch_id
    assert reused.created_at == first.created_at
    assert reused.last_checked_at == first_checked_at + timedelta(minutes=1)
    assert (
        db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch))
        == first_batch_count
    )
    assert (
        db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncItem)) == first_item_count
    )

    expired_replacement = preview_organization_sync(
        db_session,
        snapshot,
        encryption_key=_settings().encryption_key,
        actor=None,
        root_mappings=((10, "DIRECT-GROUP"),),
        trigger=DingTalkOrgSyncTrigger.SCHEDULED,
        now=first_checked_at + timedelta(minutes=16),
    )
    assert expired_replacement.batch_id != first.batch_id

    store = db_session.scalars(select(OrgUnit).where(OrgUnit.code == "DIRECT-STORE")).one()
    store.name = "本地并发改名店"
    db_session.commit()
    changed = preview_organization_sync(
        db_session,
        snapshot,
        encryption_key=_settings().encryption_key,
        actor=None,
        root_mappings=((10, "DIRECT-GROUP"),),
        trigger=DingTalkOrgSyncTrigger.SCHEDULED,
        now=first_checked_at + timedelta(minutes=17),
    )
    assert changed.batch_id != first.batch_id


def test_manual_organization_preview_stales_only_the_same_root_hash(db_session):
    _seed_store_and_managers(db_session)
    other_anchor = OrgUnit(code="OTHER-GROUP", name="其它集团", type=OrgType.GROUP)
    db_session.add(other_anchor)
    db_session.commit()
    actor = _group_hr(db_session, "org-sync-manual-scope")
    first_snapshot = _FakeOrganizationClient().list_organization_snapshot(root_department_ids=(10,))
    other_snapshot = DingTalkOrganizationSnapshot(
        departments=(
            DingTalkDepartment(20, 1, "其它运营中心"),
            DingTalkDepartment(201, 20, "其它门店"),
        ),
        users=(),
    )

    first = preview_organization_sync(
        db_session,
        first_snapshot,
        encryption_key=_settings().encryption_key,
        actor=(actor.id, actor.username),
        root_mappings=((10, "DIRECT-GROUP"),),
    )
    other = preview_organization_sync(
        db_session,
        other_snapshot,
        encryption_key=_settings().encryption_key,
        actor=(actor.id, actor.username),
        root_mappings=((20, "OTHER-GROUP"),),
    )
    db_session.expire_all()
    first_batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == first.batch_id)
    ).one()
    assert first_batch.status.value == "PREVIEWED"

    replacement = preview_organization_sync(
        db_session,
        first_snapshot,
        encryption_key=_settings().encryption_key,
        actor=(actor.id, actor.username),
        root_mappings=((10, "DIRECT-GROUP"),),
    )
    db_session.expire_all()
    first_batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == first.batch_id)
    ).one()
    other_batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == other.batch_id)
    ).one()
    assert replacement.batch_id != first.batch_id
    assert first_batch.status.value == "STALE"
    assert other_batch.status.value == "PREVIEWED"


def test_bound_local_path_cannot_be_rebound_when_remote_department_id_changes(client, db_session):
    store = _seed_store_and_managers(db_session)
    store.dingtalk_dept_id = 999
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-no-rebind")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(10, 1, "潮发运营中心"),
            DingTalkDepartment(101, 10, "天河店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)

    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    item = next(
        row for row in preview_response.json()["store_items"] if row["remote_department_id"] == 101
    )
    assert item["status"] == "CONFLICT"
    assert item["conflict_code"] == "ORG_PATH_AMBIGUOUS"
    assert item["proposed_org_unit_id"] is None

    apply_response = client.post(
        f"/api/dingtalk/sync/organization/{preview_response.json()['batch_id']}/apply",
        headers=headers,
    )
    assert apply_response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id == 999


def test_shared_anchor_supports_two_roots_without_cross_path_matches(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    east = OrgUnit(code="SHARED-EAST", name="东店", type=OrgType.STORE, parent=anchor)
    west = OrgUnit(code="SHARED-WEST", name="西店", type=OrgType.STORE, parent=anchor)
    db_session.add_all([anchor, east, west])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-shared-anchor")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(101, 10, "东店"),
            DingTalkDepartment(201, 20, "西店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(
        root_mappings="20:DIRECT-GROUP,10:DIRECT-GROUP"
    )
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    stores = {row["remote_department_id"]: row for row in response.json()["store_items"]}
    assert stores[101]["proposed_org_unit_id"] == east.id
    assert stores[201]["proposed_org_unit_id"] == west.id
    assert {stores[101]["status"], stores[201]["status"]} == {"READY"}


def test_organization_apply_creates_same_name_store_under_distinct_root_path(client, db_session):
    east_anchor = OrgUnit(code="EAST-GROUP", name="East Group", type=OrgType.GROUP)
    west_anchor = OrgUnit(code="WEST-GROUP", name="West Group", type=OrgType.GROUP)
    existing_store = OrgUnit(
        code="EAST-SHARED",
        name="Shared店",
        type=OrgType.STORE,
        parent=east_anchor,
    )
    db_session.add_all([east_anchor, west_anchor, existing_store])
    db_session.flush()
    db_session.add_all(
        [
            Employee(
                emp_no="E101",
                name="East dining manager",
                org_unit_id=existing_store.id,
                department=Department.DINING,
                position_title="店长",
            ),
            Employee(
                emp_no="E102",
                name="East kitchen manager",
                org_unit_id=existing_store.id,
                department=Department.KITCHEN,
                position_title="厨房经理",
            ),
            Employee(
                emp_no="W201",
                name="West dining manager",
                org_unit_id=existing_store.id,
                department=Department.DINING,
                position_title="店长",
            ),
            Employee(
                emp_no="W202",
                name="West kitchen manager",
                org_unit_id=existing_store.id,
                department=Department.KITCHEN,
                position_title="厨房经理",
            ),
        ]
    )
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-create-same-name-different-root")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(101, 10, "Shared店"),
            DingTalkDepartment(201, 20, "Shared店"),
        ),
        users=(
            DingTalkOrganizationUser(
                "east-dining", "East dining manager", "E101", "店长", True, (101,)
            ),
            DingTalkOrganizationUser(
                "east-kitchen", "East kitchen manager", "E102", "厨房经理", True, (101,)
            ),
            DingTalkOrganizationUser(
                "west-dining", "West dining manager", "W201", "店长", True, (201,)
            ),
            DingTalkOrganizationUser(
                "west-kitchen", "West kitchen manager", "W202", "厨房经理", True, (201,)
            ),
        ),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(
        root_mappings="10:EAST-GROUP,20:WEST-GROUP"
    )
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)

    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["region_items"] == []
    stores_by_remote_id = {item["remote_department_id"]: item for item in preview["store_items"]}
    assert stores_by_remote_id[101]["action"] == "LINK"
    assert stores_by_remote_id[101]["proposed_org_unit_id"] == existing_store.id
    assert stores_by_remote_id[201]["action"] == "CREATE"
    assert stores_by_remote_id[201]["status"] == "READY"
    assert stores_by_remote_id[201]["proposed_org_unit_id"] is None
    assert stores_by_remote_id[201]["proposed_parent_org_unit_id"] == west_anchor.id
    assert preview["reviewer_conflicts"] == 0

    apply_response = client.post(
        f"/api/dingtalk/sync/organization/{preview['batch_id']}/apply",
        headers=headers,
    )

    assert apply_response.status_code == 200, apply_response.text
    assert apply_response.json() == {
        "applied_stores": 2,
        "applied_reviewers": 4,
        "unresolved": 0,
        "already_applied": False,
    }
    db_session.expire_all()
    unchanged_existing = db_session.get(OrgUnit, existing_store.id)
    assert unchanged_existing is not None
    assert (
        unchanged_existing.code,
        unchanged_existing.name,
        unchanged_existing.parent_id,
        unchanged_existing.status,
        unchanged_existing.dingtalk_dept_id,
    ) == ("EAST-SHARED", "Shared店", east_anchor.id, "ACTIVE", 101)
    created_store = db_session.scalars(select(OrgUnit).where(OrgUnit.dingtalk_dept_id == 201)).one()
    assert (
        created_store.code,
        created_store.name,
        created_store.parent_id,
        created_store.status,
    ) == ("DINGTALK-201", "Shared店", west_anchor.id, "ACTIVE")


def test_shared_anchor_conflicts_when_two_roots_target_the_same_local_path(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    store = OrgUnit(code="SHARED-STORE", name="同名店", type=OrgType.STORE, parent=anchor)
    db_session.add_all([anchor, store])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-shared-anchor-conflict")
    fake = _FakeOrganizationClient(
        departments=(
            DingTalkDepartment(101, 10, "同名店"),
            DingTalkDepartment(201, 20, "同名店"),
        ),
        users=(),
    )
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(
        root_mappings="10:DIRECT-GROUP,20:DIRECT-GROUP"
    )
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    stores = [
        row for row in response.json()["store_items"] if row["remote_department_id"] is not None
    ]
    assert len(stores) == 2
    assert {row["status"] for row in stores} == {"CONFLICT"}
    assert {row["conflict_code"] for row in stores} == {"ORG_PATH_AMBIGUOUS"}


def test_distinct_nested_anchors_fail_closed(client, db_session):
    group = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    nested = OrgUnit(code="NESTED-ANCHOR", name="嵌套区域", type=OrgType.REGION, parent=group)
    db_session.add_all([group, nested])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-overlapping-anchors")
    fake = _FakeOrganizationClient(departments=(), users=())
    from app.main import app

    app.dependency_overrides[get_settings] = lambda: _settings(
        root_mappings="10:DIRECT-GROUP,20:NESTED-ANCHOR"
    )
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == 0


def test_normalized_equivalent_name_does_not_stage_false_update(client, db_session):
    anchor = OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP)
    store = OrgUnit(code="NORMALIZED-STORE", name="ＡＢＣ　店", type=OrgType.STORE, parent=anchor)
    db_session.add_all([anchor, store])
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-normalized-name")
    fake = _FakeOrganizationClient(departments=(DingTalkDepartment(101, 10, "abc 店"),), users=())
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 200, response.text
    item = next(row for row in response.json()["store_items"] if row["remote_department_id"])
    assert item["action"] == "LINK"
    assert item["change_fields"] == ["dingtalk_dept_id"]


def test_remote_relative_path_over_storage_limit_fails_closed(client, db_session):
    db_session.add(OrgUnit(code="DIRECT-GROUP", name="集团", type=OrgType.GROUP))
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-long-path")
    departments: list[DingTalkDepartment] = []
    parent_id = 10
    for index in range(1, 25):
        department_id = 100 + index
        departments.append(
            DingTalkDepartment(department_id, parent_id, f"区域{index}-" + "长" * 48)
        )
        parent_id = department_id
    departments.append(DingTalkDepartment(999, parent_id, "终点店"))
    fake = _FakeOrganizationClient(departments=tuple(departments), users=())
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake

    response = client.post(
        "/api/dingtalk/sync/organization/preview",
        headers=_token(client, admin.username),
    )

    assert response.status_code == 409
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == 0


def test_scheduled_cache_hit_skips_full_planners_and_does_not_insert(db_session, monkeypatch):
    _seed_store_and_managers(db_session)
    snapshot = _FakeOrganizationClient().list_organization_snapshot(root_department_ids=(10,))
    checked_at = datetime(2026, 7, 22, 10, 0, tzinfo=UTC)
    first = preview_organization_sync(
        db_session,
        snapshot,
        encryption_key=_settings().encryption_key,
        actor=None,
        root_mappings=((10, "DIRECT-GROUP"),),
        trigger=DingTalkOrgSyncTrigger.SCHEDULED,
        now=checked_at,
    )
    batch_count = db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch))
    item_count = db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncItem))

    def fail_planner(*_args, **_kwargs):
        raise AssertionError("scheduled cache hit entered the full planner")

    baseline_calls = 0
    complete_local_baseline = org_sync._complete_local_baseline

    def count_complete_local_baseline(*args, **kwargs):
        nonlocal baseline_calls
        baseline_calls += 1
        return complete_local_baseline(*args, **kwargs)

    monkeypatch.setattr(org_sync, "_plan_organization_nodes", fail_planner)
    monkeypatch.setattr(org_sync, "_plan_organization_reviewers", fail_planner)
    monkeypatch.setattr(org_sync, "_complete_local_baseline", count_complete_local_baseline)

    reused = preview_organization_sync(
        db_session,
        snapshot,
        encryption_key=_settings().encryption_key,
        actor=None,
        root_mappings=((10, "DIRECT-GROUP"),),
        trigger=DingTalkOrgSyncTrigger.SCHEDULED,
        now=checked_at + timedelta(minutes=1),
    )

    assert reused.batch_id == first.batch_id
    assert reused.reviewer_items == first.reviewer_items
    assert baseline_calls == 2
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncBatch)) == batch_count
    assert db_session.scalar(select(func.count()).select_from(DingTalkOrgSyncItem)) == item_count


def test_reviewer_planner_scans_users_and_store_rows_once(db_session, monkeypatch):
    class CountingTuple(tuple):
        iterations = 0

        def __iter__(self):
            self.iterations += 1
            return super().__iter__()

    class CountingStoreRows(dict):
        reads = 0

        def __getitem__(self, key):
            self.reads += 1
            return super().__getitem__(key)

    class CountingOrganizationUser:
        def __init__(self, user_id: str, name: str, department_ids: tuple[int, ...]) -> None:
            self.user_id = user_id
            self.name = name
            self.job_number = None
            self.title = None
            self.active = True
            self._department_ids = department_ids
            self.department_id_reads = 0

        @property
        def department_ids(self) -> tuple[int, ...]:
            self.department_id_reads += 1
            return self._department_ids

    anchor = OrgUnit(code="DIRECT-GROUP", name="Group", type=OrgType.GROUP)
    stores = [
        OrgUnit(
            code=f"PERF-STORE-{index}",
            name=f"Store {index}",
            type=OrgType.STORE,
            parent=anchor,
        )
        for index in range(20)
    ]
    db_session.add_all([anchor, *stores])
    db_session.commit()
    users = tuple(
        CountingOrganizationUser(
            f"provider-{index}",
            f"Employee {index}",
            (1000 + (index % len(stores)),),
        )
        for index in range(500)
    )
    snapshot = DingTalkOrganizationSnapshot(
        departments=tuple(
            DingTalkDepartment(1000 + index, 10, store.name) for index, store in enumerate(stores)
        ),
        users=cast(tuple[DingTalkOrganizationUser, ...], users),
    )
    state = org_sync._load_local_state(db_session)
    org_units_by_id = {organization.id: organization for organization in state.org_units}
    roots = org_sync._resolve_root_mappings(state, ((10, "DIRECT-GROUP"),))
    authority = org_sync._build_authority_index(state, roots, org_units_by_id)
    node_plan = org_sync._plan_organization_nodes(
        state,
        snapshot,
        authority=authority,
        org_units_by_id=org_units_by_id,
    )
    counted_rows = CountingTuple(node_plan.store_rows)
    counted_store_map = CountingStoreRows(node_plan.store_row_by_remote_id)
    counted_plan = org_sync._NodePlan(
        classified=node_plan.classified,
        rows=node_plan.rows,
        region_rows=node_plan.region_rows,
        store_rows=counted_rows,
        store_row_by_remote_id=counted_store_map,
        store_matches=node_plan.store_matches,
        local_authority_nodes=node_plan.local_authority_nodes,
        local_only_stores=node_plan.local_only_stores,
    )
    directory_match_calls = 0
    match_directory_users = org_sync.match_directory_users

    def count_directory_matches(*args, **kwargs):
        nonlocal directory_match_calls
        directory_match_calls += 1
        return match_directory_users(*args, **kwargs)

    monkeypatch.setattr(org_sync, "match_directory_users", count_directory_matches)
    org_sync._plan_organization_reviewers(
        state,
        snapshot,
        encryption_key=_settings().encryption_key,
        node_plan=counted_plan,
        dining_manager_titles=frozenset(),
        kitchen_manager_titles=frozenset(),
    )

    assert directory_match_calls == 1
    assert sum(user.department_id_reads for user in users) == len(users)
    assert counted_rows.iterations == 1
    assert counted_store_map.reads == len(stores)


def test_apply_stales_preview_when_current_root_mapping_changes(client, db_session):
    store = _seed_store_and_managers(db_session)
    other_anchor = OrgUnit(code="OTHER-ANCHOR", name="其它集团", type=OrgType.GROUP)
    db_session.add(other_anchor)
    db_session.commit()
    admin = _group_hr(db_session, "org-sync-apply-root-change")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)
    batch_id = preview_response.json()["batch_id"]
    app.dependency_overrides[get_settings] = lambda: _settings(root_mappings="10:OTHER-ANCHOR")

    apply_response = client.post(
        f"/api/dingtalk/sync/organization/{batch_id}/apply", headers=headers
    )

    assert apply_response.status_code == 409
    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == batch_id)
    ).one()
    assert batch.status.value == "STALE"
