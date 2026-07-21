from __future__ import annotations

import pytest
from sqlalchemy import select, text

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
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
from app.models.employee import Department
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
    audit_row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "user.dingtalk_recipient.replace")
    ).one()
    assert audit_row.detail == {
        "before_configured": False,
        "after_configured": True,
    }

    cleared = client.put(
        f"/api/users/{manager.id}/dingtalk-recipient",
        headers=headers,
        json={"dingtalk_user_id": None},
    )
    assert cleared.json() == {"configured": False}
