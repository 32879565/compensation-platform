import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserOrgScope, UserRole

pytestmark = pytest.mark.usefixtures("pg_engine")


def _make_user(session, username, password, role_codes=(), scope_org_ids=()):
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password(password))
    session.add(user)
    session.flush()
    for code in role_codes:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=user.id, role_id=role.id))
    for oid in scope_org_ids:
        session.add(UserOrgScope(user_id=user.id, org_unit_id=oid))
    session.flush()
    return user


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    from app.db.session import get_session
    from app.main import app

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_login_success_sets_cookie_and_returns_token(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    resp = client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["access_token"]
    assert "employee:read" in body["permissions"]
    assert "comp_refresh" in resp.cookies
    assert resp.headers["cache-control"] == "no-store"


def test_login_wrong_password_401(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    resp = client.post("/api/auth/login", json={"username": "hr", "password": "nope"})
    assert resp.status_code == 401


def test_login_unknown_user_same_401(client, db_session):
    # 未知用户与错误口令返回相同状态与信息（防用户名枚举）
    resp = client.post("/api/auth/login", json={"username": "ghost", "password": "x"})
    assert resp.status_code == 401
    assert resp.json()["detail"] == "用户名或密码错误"


def test_login_lockout_after_max_failures(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "hr", "password": "wrong"})
    # 第 6 次即便口令正确也被限速拦截
    resp = client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    assert resp.status_code == 429
    assert resp.headers["retry-after"].isdigit()
    assert resp.headers["cache-control"] == "no-store"


def test_login_ip_lockout_applies_across_rotating_usernames(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    for number in range(5):
        response = client.post(
            "/api/auth/login", json={"username": f"unknown-{number}", "password": "wrong"}
        )
        assert response.status_code == 401

    blocked = client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    assert blocked.status_code == 429


def test_me_requires_auth(client):
    assert client.get("/api/auth/me").status_code == 401


def test_me_returns_permissions_and_scope(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    login = client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    token = login.json()["access_token"]
    resp = client.get("/api/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["username"] == "hr"
    assert body["unrestricted_scope"] is True  # GROUP_HR 全局范围


def test_me_rejects_garbage_token(client):
    resp = client.get("/api/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
    assert resp.status_code == 401


def test_refresh_happy_path(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    resp = client.post("/api/auth/refresh")
    assert resp.status_code == 200
    assert resp.json()["access_token"]
    assert resp.headers["cache-control"] == "no-store"


def test_refresh_without_cookie_is_a_guest_no_content_response(client):
    response = client.post("/api/auth/refresh")
    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["cache-control"] == "no-store"


def test_logout_revokes_and_refresh_then_fails(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    refresh_token = client.cookies.get("comp_refresh")
    assert refresh_token
    assert client.post("/api/auth/logout").status_code == 204
    # Replay the token that logout revoked.  A cookie that was merely cleared
    # from this test client is correctly treated as a guest (204).
    assert (
        client.post("/api/auth/refresh", cookies={"comp_refresh": refresh_token}).status_code == 401
    )


def test_login_success_writes_audit(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    client.post("/api/auth/login", json={"username": "hr", "password": "StrongPass123!"})
    row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login", AuditLog.result == "SUCCESS")
    ).first()
    assert row is not None
    assert row.actor_username == "hr"


def test_login_lockout_writes_audit_without_password(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "hr", "password": "wrong"})
    row = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "auth.login", AuditLog.result == "LOCKED")
    ).first()
    assert row is not None
    # 审计明细绝不含明文口令
    assert "wrong" not in str(row.detail)


def test_repeated_locked_logins_do_not_append_an_audit_row(client, db_session):
    _make_user(db_session, "hr", "StrongPass123!", ["GROUP_HR"])
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "hr", "password": "wrong"})
    before = db_session.scalars(select(AuditLog).where(AuditLog.action == "auth.login")).all()

    for _ in range(3):
        response = client.post("/api/auth/login", json={"username": "hr", "password": "wrong"})
        assert response.status_code == 429

    after = db_session.scalars(select(AuditLog).where(AuditLog.action == "auth.login")).all()
    assert len(after) == len(before) == 1
