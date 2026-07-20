import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.auth import Role, User, UserRole
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def _user(session, username, roles):
    seed_rbac(session)
    u = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(u)
    session.flush()
    for code in roles:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=u.id, role_id=role.id))
    session.flush()
    return u


def _employee(session):
    store = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, city="广州")
    session.add(store)
    session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id)
    session.add(emp)
    session.flush()
    return emp


@pytest.fixture
def client(db_session):
    from fastapi.testclient import TestClient

    import app.auth.router as router_mod
    from app.db.session import get_session
    from app.main import app

    router_mod._throttle._failures.clear()

    def _override():
        yield db_session

    app.dependency_overrides[get_session] = _override
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def _token(client, username):
    r = client.post("/api/auth/login", json={"username": username, "password": "StrongPass123!"})
    return {"Authorization": f"Bearer {r.json()['access_token']}"}


def test_component_crud_and_permission(client, db_session):
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.post(
        "/api/salary-components",
        headers=h,
        json={"code": "BASE", "name": "基本薪资", "component_type": "BASE", "in_social_base": True},
    )
    assert r.status_code == 201
    assert r.json()["in_social_base"] is True
    assert len(client.get("/api/salary-components", headers=h).json()) == 1
    # 重复编码 409
    assert (
        client.post(
            "/api/salary-components",
            headers=h,
            json={"code": "BASE", "name": "x", "component_type": "BASE"},
        ).status_code
        == 409
    )


def test_component_requires_permission(client, db_session):
    _user(db_session, "emp", ["EMPLOYEE"])
    h = _token(client, "emp")
    assert client.get("/api/salary-components", headers=h).status_code == 403


def test_set_and_get_structure_with_compa(client, db_session):
    emp = _employee(db_session)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    comp = client.post(
        "/api/salary-components",
        headers=h,
        json={"code": "BASE", "name": "基本", "component_type": "BASE"},
    ).json()
    r = client.put(
        f"/api/employees/{emp.id}/structure/{comp['id']}",
        headers=h,
        json={"amount": "5000", "effective_from": "2026-01-01"},
    )
    assert r.status_code == 200
    got = client.get(f"/api/employees/{emp.id}/structure?on_date=2026-06-01", headers=h)
    assert got.status_code == 200
    body = got.json()
    assert len(body["items"]) == 1
    assert body["compa"]["total"] == "5000.00"
    assert body["compa"]["band_status"] == "NO_BAND"  # 员工无职级带宽
