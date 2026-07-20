from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.comp.service import set_component_amount
from app.core.security import hash_password
from app.models.attendance import AttendanceRecord
from app.models.auth import Role, User, UserRole
from app.models.comp import ComponentType, SalaryComponentDef
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


def _setup_employee(session, with_attendance=True):
    store = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, city="广州")
    session.add(store)
    session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id)
    session.add(emp)
    session.flush()
    base = SalaryComponentDef(code="BASE", name="基本", component_type=ComponentType.BASE)
    session.add(base)
    session.flush()
    set_component_amount(
        session,
        employee_id=emp.id,
        component_id=base.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 1, 1),
    )
    if with_attendance:
        session.add(
            AttendanceRecord(
                employee_id=emp.id,
                period="2026-05",
                expected_days=Decimal("22"),
                actual_days=Decimal("22"),
            )
        )
        session.flush()
    return emp


def test_payroll_preview_full(client, db_session):
    emp = _setup_employee(db_session)
    _user(db_session, "fin", ["FINANCE"])
    h = _token(client, "fin")
    r = client.get(f"/api/employees/{emp.id}/payroll-preview?period=2026-05", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["has_error"] is False
    assert body["gross"] == "5000.00"
    assert body["rule_version"] == "v1"
    assert any(li["code"] == "BASE" for li in body["lines"])


def test_payroll_preview_missing_attendance_error(client, db_session):
    emp = _setup_employee(db_session, with_attendance=False)
    _user(db_session, "fin", ["FINANCE"])
    h = _token(client, "fin")
    r = client.get(f"/api/employees/{emp.id}/payroll-preview?period=2026-05", headers=h)
    assert r.status_code == 200
    body = r.json()
    assert body["has_error"] is True
    assert any("考勤" in e for e in body["exceptions"])


def test_payroll_preview_requires_permission(client, db_session):
    emp = _setup_employee(db_session)
    _user(db_session, "emp", ["EMPLOYEE"])
    h = _token(client, "emp")
    r = client.get(f"/api/employees/{emp.id}/payroll-preview?period=2026-05", headers=h)
    assert r.status_code == 403
