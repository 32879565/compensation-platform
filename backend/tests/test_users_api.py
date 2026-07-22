from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.dingtalk.read_sync import blind_index_dingtalk_user_id
from app.models.audit import AuditLog
from app.models.auth import (
    Permission,
    Role,
    RolePermission,
    User,
    UserOrgScope,
    UserReviewScope,
    UserRole,
)
from app.models.dingtalk import (
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncBatchStatus,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
)
from app.models.employee import Department, Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _user(session, username: str, roles: list[str]) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    for role_code in roles:
        role = session.scalars(select(Role).where(Role.code == role_code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def test_user_manager_can_list_roles_and_explicit_review_scopes(client, db_session):
    store = OrgUnit(code="USER_STORE", name="User store", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    _user(db_session, "admin", ["SUPER_ADMIN"])
    manager = _user(db_session, "manager", ["STORE_MANAGER"])
    db_session.add(
        UserReviewScope(
            user_id=manager.id,
            org_unit_id=store.id,
            department=Department.DINING,
        )
    )
    db_session.commit()

    response = client.get("/api/users", headers=_token(client, "admin"))
    assert response.status_code == 200, response.text
    by_username = {item["username"]: item for item in response.json()}
    assert by_username["manager"]["roles"] == ["STORE_MANAGER"]
    assert by_username["manager"]["login_enabled"] is True
    assert by_username["manager"]["review_scopes"] == [
        {"org_unit_id": store.id, "department": "DINING"}
    ]
    assert "password_hash" not in by_username["manager"]


def test_user_list_requires_user_management_permission(client, db_session):
    _user(db_session, "ordinary", ["EMPLOYEE"])

    assert client.get("/api/users", headers=_token(client, "ordinary")).status_code == 403


def test_store_scoped_user_manager_is_rejected(client, db_session):
    seed_rbac(db_session)
    store = OrgUnit(code="SCOPED_USER_STORE", name="Scoped user store", type=OrgType.STORE)
    db_session.add(store)
    role = Role(code="SCOPED_USER_MANAGER", name="Scoped user manager", is_global_scope=False)
    db_session.add(role)
    db_session.flush()
    permission_id = db_session.scalars(
        select(Permission.id).where(Permission.code == "user:manage")
    ).one()
    db_session.add(RolePermission(role_id=role.id, permission_id=permission_id))
    manager = _user(db_session, "scoped-admin", ["AUDITOR", "SCOPED_USER_MANAGER"])
    db_session.add(UserOrgScope(user_id=manager.id, org_unit_id=store.id))
    db_session.commit()

    assert client.get("/api/users", headers=_token(client, manager.username)).status_code == 403


def test_dingtalk_recipient_is_encrypted_audited_and_never_read_back(client, db_session):
    admin = _user(db_session, "ding-admin", ["SUPER_ADMIN"])
    manager = _user(db_session, "ding-manager", ["STORE_MANAGER"])
    db_session.commit()
    headers = _token(client, admin.username)

    configured = client.put(
        f"/api/users/{manager.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": "provider-manager-id"},
    )
    assert configured.status_code == 200, configured.text
    assert configured.json() == {"configured": True}

    listed = client.get("/api/users", headers=headers)
    row = next(item for item in listed.json() if item["id"] == manager.id)
    assert row["dingtalk_recipient_configured"] is True
    assert "dingtalk_user_id" not in row
    stored = db_session.scalar(
        text("SELECT dingtalk_user_id FROM app_user WHERE id = :user_id"),
        {"user_id": manager.id},
    )
    assert stored != "provider-manager-id"
    db_session.refresh(manager)
    assert manager.dingtalk_user_id_hash is not None
    assert len(manager.dingtalk_user_id_hash) == 64
    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "user.dingtalk_recipient.replace")
    ).one()
    assert audit_row.detail == {
        "before_configured": False,
        "after_configured": True,
        "invalidated_sync_proof_count": 0,
    }

    cleared = client.put(
        f"/api/users/{manager.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": None},
    )
    assert cleared.json() == {"configured": False}
    db_session.refresh(manager)
    assert manager.dingtalk_user_id_hash is None


def test_dingtalk_recipient_cannot_be_bound_to_two_users(client, db_session):
    admin = _user(db_session, "ding-unique-admin", ["SUPER_ADMIN"])
    first = _user(db_session, "ding-unique-first", ["STORE_MANAGER"])
    second = _user(db_session, "ding-unique-second", ["STORE_MANAGER"])
    db_session.commit()
    headers = _token(client, admin.username)
    assert (
        client.put(
            f"/api/users/{first.id}/dingtalk-recipient",
            headers=headers,
            json={"dingtalk_user_id": "provider-unique-id"},
        ).status_code
        == 200
    )

    duplicate = client.put(
        f"/api/users/{second.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": "provider-unique-id"},
    )

    assert duplicate.status_code == 409
    db_session.refresh(second)
    assert second.dingtalk_user_id is None
    assert second.dingtalk_user_id_hash is None


def test_unlinked_account_cannot_claim_an_employee_dingtalk_identity(client, db_session):
    admin = _user(db_session, "ding-cross-table-admin", ["SUPER_ADMIN"])
    store = OrgUnit(code="DING-CROSS-TABLE", name="Cross-table store", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="DING-CROSS-001",
        name="Identity owner",
        org_unit_id=store.id,
        department=Department.DINING,
        dingtalk_user_id_hash=blind_index_dingtalk_user_id(
            "provider-cross-table-owner",
            key="test-encryption-key-only-for-tests",
        ),
    )
    unlinked = _user(db_session, "ding-cross-table-unlinked", ["STORE_MANAGER"])
    db_session.add(employee)
    db_session.commit()

    response = client.put(
        f"/api/users/{unlinked.id}/dingtalk-recipient",
        headers=_token(client, admin.username),
        json={"dingtalk_user_id": "provider-cross-table-owner"},
    )

    assert response.status_code == 409
    db_session.refresh(unlinked)
    assert unlinked.dingtalk_user_id is None
    assert unlinked.dingtalk_user_id_hash is None


def test_second_account_cannot_overwrite_employee_dingtalk_identity(client, db_session):
    admin = _user(db_session, "ding-employee-owner-admin", ["SUPER_ADMIN"])
    store = OrgUnit(
        code="DING-EMPLOYEE-OWNER-STORE",
        name="员工身份门店",
        type=OrgType.STORE,
    )
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="DING-EMPLOYEE-OWNER-001",
        name="多账号员工",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    db_session.add(employee)
    db_session.flush()
    first = _user(db_session, "ding-employee-owner-first", ["STORE_MANAGER"])
    second = _user(db_session, "ding-employee-owner-second", ["STORE_MANAGER"])
    first.employee_id = employee.id
    second.employee_id = employee.id
    db_session.commit()
    headers = _token(client, admin.username)

    configured = client.put(
        f"/api/users/{first.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": "provider-employee-owner-first"},
    )
    assert configured.status_code == 200, configured.text
    db_session.refresh(first)
    db_session.refresh(employee)
    original_hash = first.dingtalk_user_id_hash
    assert original_hash is not None
    assert employee.dingtalk_user_id_hash == original_hash

    rejected = client.put(
        f"/api/users/{second.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": "provider-employee-owner-second"},
    )

    assert rejected.status_code == 409
    db_session.refresh(second)
    db_session.refresh(employee)
    assert second.dingtalk_user_id is None
    assert second.dingtalk_user_id_hash is None
    assert employee.dingtalk_user_id_hash == original_hash


def test_scoped_reviewer_identity_must_move_through_org_sync(client, db_session):
    admin = _user(db_session, "ding-scoped-admin", ["SUPER_ADMIN"])
    store = OrgUnit(code="DING-SCOPED-STORE", name="身份门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="DING-SCOPED-001",
        name="负责人",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    db_session.add(employee)
    db_session.flush()
    manager = _user(db_session, "ding-scoped-manager", ["STORE_MANAGER"])
    manager.employee_id = employee.id
    db_session.commit()
    headers = _token(client, admin.username)
    configured = client.put(
        f"/api/users/{manager.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": "provider-scoped-id"},
    )
    assert configured.status_code == 200, configured.text
    db_session.refresh(employee)
    db_session.refresh(manager)
    assert employee.dingtalk_user_id_hash == manager.dingtalk_user_id_hash
    db_session.add(
        UserReviewScope(
            user_id=manager.id,
            org_unit_id=store.id,
            department=Department.DINING,
        )
    )
    db_session.commit()

    rejected = client.put(
        f"/api/users/{manager.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": "provider-replacement-id"},
    )

    assert rejected.status_code == 409
    db_session.refresh(manager)
    assert manager.dingtalk_user_id == "provider-scoped-id"


def test_user_manager_can_make_reviewer_dingtalk_only(client, db_session):
    admin = _user(db_session, "login-control-admin", ["SUPER_ADMIN"])
    manager = _user(db_session, "login-control-manager", ["STORE_MANAGER"])
    db_session.commit()
    manager_login = client.post(
        "/api/auth/login",
        json={"username": manager.username, "password": "StrongPass123!"},
    )
    assert manager_login.status_code == 200
    manager_headers = {"Authorization": f"Bearer {manager_login.json()['access_token']}"}
    manager_refresh = client.cookies.get("comp_refresh")
    assert manager_refresh
    headers = _token(client, admin.username)

    response = client.put(
        f"/api/users/{manager.id}/login-enabled",
        headers=headers,
        json={"login_enabled": False},
    )
    assert response.status_code == 200, response.text
    assert response.json() == {"login_enabled": False}
    assert client.get("/api/auth/me", headers=manager_headers).status_code == 401
    assert (
        client.post(
            "/api/auth/refresh",
            cookies={"comp_refresh": manager_refresh},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/api/auth/login",
            json={"username": manager.username, "password": "StrongPass123!"},
        ).status_code
        == 401
    )

    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "user.login_enabled.replace")
    ).one()
    assert audit_row.detail == {"before": True, "after": False}

    own_disable = client.put(
        f"/api/users/{admin.id}/login-enabled",
        headers=headers,
        json={"login_enabled": False},
    )
    assert own_disable.status_code == 409


def test_review_scope_reassignment_conflict_returns_409_and_preserves_owner(client, db_session):
    admin = _user(db_session, "scope-admin", ["SUPER_ADMIN"])
    original = _user(db_session, "scope-original", ["STORE_MANAGER"])
    replacement = _user(db_session, "scope-replacement", ["STORE_MANAGER"])
    store = OrgUnit(code="SCOPE-STORE", name="复核门店", type=OrgType.STORE)
    db_session.add(store)
    db_session.commit()
    headers = _token(client, admin.username)
    scope_body = {"scopes": [{"org_unit_id": store.id, "department": Department.DINING.value}]}
    first = client.put(
        f"/api/users/{original.id}/review-scopes",
        headers=headers,
        json=scope_body,
    )
    assert first.status_code == 200, first.text

    conflict = client.put(
        f"/api/users/{replacement.id}/review-scopes",
        headers=headers,
        json=scope_body,
    )

    assert conflict.status_code == 409
    owner_ids = db_session.scalars(
        select(UserReviewScope.user_id).where(
            UserReviewScope.org_unit_id == store.id,
            UserReviewScope.department == Department.DINING,
        )
    ).all()
    assert owner_ids == [original.id]


def test_manual_scope_remove_and_readd_cannot_restore_applied_sync_proof(client, db_session):
    admin = _user(db_session, "scope-proof-admin", ["SUPER_ADMIN"])
    manager = _user(db_session, "scope-proof-manager", ["STORE_MANAGER"])
    store = OrgUnit(code="SCOPE-PROOF-STORE", name="Proof store", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="SCOPE-PROOF-001",
        name="Proof manager",
        org_unit_id=store.id,
        department=Department.DINING,
    )
    db_session.add(employee)
    db_session.flush()
    manager.employee_id = employee.id
    db_session.add(
        UserReviewScope(
            user_id=manager.id,
            org_unit_id=store.id,
            department=Department.DINING,
        )
    )
    sync_batch = DingTalkOrgSyncBatch(
        status=DingTalkOrgSyncBatchStatus.APPLIED,
        snapshot_hash="a" * 64,
        expires_at=datetime.now(UTC) + timedelta(minutes=15),
        requested_by_user_id=admin.id,
        applied_by_user_id=admin.id,
        applied_at=datetime.now(UTC),
    )
    db_session.add(sync_batch)
    db_session.flush()
    reviewer_item = DingTalkOrgSyncItem(
        batch_id=sync_batch.id,
        row_key="REVIEWER:SCOPE-PROOF:DINING",
        kind=DingTalkOrgSyncItemKind.REVIEWER,
        status=DingTalkOrgSyncItemStatus.APPLIED,
        remote_department_id=700,
        remote_department_name=store.name,
        remote_department_path=f"Group / {store.name}",
        proposed_org_unit_id=store.id,
        proposed_employee_id=employee.id,
        department=Department.DINING,
        match_method="ASSIGN|STABLE_ID",
        applied_identity_proof="d" * 64,
        baseline_fingerprint="b" * 64,
    )
    db_session.add(reviewer_item)
    db_session.commit()
    headers = _token(client, admin.username)

    removed = client.put(
        f"/api/users/{manager.id}/review-scopes",
        headers=headers,
        json={"scopes": []},
    )
    assert removed.status_code == 200, removed.text
    db_session.refresh(reviewer_item)
    assert reviewer_item.applied_identity_proof is None

    restored = client.put(
        f"/api/users/{manager.id}/review-scopes",
        headers=headers,
        json={
            "scopes": [
                {"org_unit_id": store.id, "department": Department.DINING.value}
            ]
        },
    )
    assert restored.status_code == 200, restored.text
    db_session.refresh(reviewer_item)
    assert reviewer_item.applied_identity_proof is None
