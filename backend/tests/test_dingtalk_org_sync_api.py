from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from app.auth.bootstrap import seed_rbac
from app.core.config import Settings, get_settings
from app.core.security import hash_password
from app.dingtalk.client import (
    DingTalkDepartment,
    DingTalkOrganizationSnapshot,
    DingTalkOrganizationUser,
    get_dingtalk_client,
)
from app.dingtalk.read_sync import blind_index_dingtalk_user_id
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    DingTalkOrgSyncAction,
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
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
        self.users = users
        self.departments = departments
        self.snapshots = snapshots

    def list_organization_snapshot(self) -> DingTalkOrganizationSnapshot:
        self.calls += 1
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


def _settings(*, root_mappings: str = "") -> Settings:
    return Settings(
        database_url="postgresql+psycopg://test:test@localhost/test",
        secret_key="test-secret-key-only-for-tests-not-production",
        encryption_key="test-encryption-key-only-for-tests-not-production",
        cookie_secure=False,
        dingtalk_client_id="test-client-id",
        dingtalk_client_secret="test-client-secret-value",
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
    assert body["expires_at"] > datetime.now(UTC).isoformat()
    assert {item["department"] for item in body["reviewer_items"]} == {
        "DINING",
        "KITCHEN",
    }
    assert {item["action"] for item in body["reviewer_items"]} == {"ASSIGN"}
    assert body["store_items"][0]["action"] == "LINK"
    assert body["store_items"][0]["match_method"] == "UNIQUE_NAME"
    assert "provider-manager" not in response.text
    assert "provider-kitchen" not in response.text
    assert "remote_user_id" not in response.text

    db_session.expire_all()
    assert db_session.get(OrgUnit, store.id).dingtalk_dept_id is None
    assert db_session.scalars(select(UserReviewScope)).all() == []
    batch = db_session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == body["batch_id"])
    ).one()
    assert batch.root_config_hash == _expected_root_config_hash(())
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
    ("local_name", "stable_department_id", "expected_action"),
    [
        ("天河店", None, "ACTIVATE"),
        ("天河旧店", 101, "UPDATE"),
    ],
)
def test_organization_sync_activates_or_updates_historical_store(
    client,
    db_session,
    local_name,
    stable_department_id,
    expected_action,
):
    store = _seed_store_and_managers(db_session)
    store.name = local_name
    store.status = "HISTORICAL"
    store.dingtalk_dept_id = stable_department_id
    db_session.commit()
    admin = _group_hr(db_session, f"org-sync-{expected_action.lower()}")
    fake = _FakeOrganizationClient()
    from app.main import app

    app.dependency_overrides[get_settings] = _settings
    app.dependency_overrides[get_dingtalk_client] = lambda: fake
    headers = _token(client, admin.username)
    preview_response = client.post("/api/dingtalk/sync/organization/preview", headers=headers)

    assert preview_response.status_code == 200, preview_response.text
    preview = preview_response.json()
    assert preview["store_items"][0]["action"] == expected_action
    staged_store = db_session.scalars(
        select(DingTalkOrgSyncItem).where(DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.STORE)
    ).one()
    expected_persistence = {
        "ACTIVATE": (DingTalkOrgSyncAction.ACTIVATE, ("status",)),
        "UPDATE": (DingTalkOrgSyncAction.UPDATE, ("name",)),
    }
    assert (staged_store.action, tuple(staged_store.change_fields)) == expected_persistence[
        expected_action
    ]
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
    assert {item["action"] for item in conflicting_stores} == {"LINK", "UPDATE"}
    assert {item["proposed_org_unit_id"] for item in conflicting_stores} == {store.id}
    assert {item["status"] for item in conflicting_stores} == {"CONFLICT"}
    assert {item["conflict_code"] for item in conflicting_stores} == {"STORE_TARGET_CONFLICT"}
    assert preview["store_conflicts"] == 2
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
    assert staged_create.change_fields == [
        "code",
        "name",
        "parent_id",
        "type",
        "dingtalk_dept_id",
    ]
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
    coverage = next(
        item for item in preview["store_items"] if item["action"] == "MISSING_IN_DINGTALK"
    )
    assert coverage["remote_department_id"] is None
    assert coverage["proposed_org_unit_id"] == hidden_store.id
    assert coverage["match_method"] == "LOCAL_STORE_NOT_VISIBLE"
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
            ("status",),
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
    assert response.json()["unresolved"] == 1
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
