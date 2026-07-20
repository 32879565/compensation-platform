import io

import pytest
from openpyxl import Workbook
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.auth import Role, User, UserOrgScope, UserRole
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def _orgs(session):
    group = OrgUnit(code="G", name="集团", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    gz = OrgUnit(code="R_GZ", name="广州", type=OrgType.REGION, parent_id=group.id)
    sz = OrgUnit(code="R_SZ", name="深圳", type=OrgType.REGION, parent_id=group.id)
    session.add_all([gz, sz])
    session.flush()
    gzs = OrgUnit(code="S_GZ", name="广州店", type=OrgType.STORE, parent_id=gz.id, city="广州")
    szs = OrgUnit(code="S_SZ", name="深圳店", type=OrgType.STORE, parent_id=sz.id, city="深圳")
    session.add_all([gzs, szs])
    session.flush()
    return gz, gzs, szs


def _emp(session, emp_no, org_id):
    e = Employee(emp_no=emp_no, name=emp_no, org_unit_id=org_id)
    session.add(e)
    session.flush()
    return e


def _user(session, username, roles, scope_ids=()):
    seed_rbac(session)
    u = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(u)
    session.flush()
    for code in roles:
        role = session.scalars(select(Role).where(Role.code == code)).one()
        session.add(UserRole(user_id=u.id, role_id=role.id))
    for oid in scope_ids:
        session.add(UserOrgScope(user_id=u.id, org_unit_id=oid))
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


def test_set_and_list_attendance(client, db_session):
    _gz, gzs, _szs = _orgs(db_session)
    emp = _emp(db_session, "E1", gzs.id)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.put(
        f"/api/employees/{emp.id}/attendance/2026-05",
        headers=h,
        json={"expected_days": "22", "actual_days": "21.5", "overtime_hours": "8"},
    )
    assert r.status_code == 200
    assert r.json()["actual_days"] == "21.50"
    lst = client.get("/api/attendance?period=2026-05", headers=h)
    assert lst.status_code == 200 and len(lst.json()) == 1


def test_attendance_upsert_overwrites(client, db_session):
    _gz, gzs, _szs = _orgs(db_session)
    emp = _emp(db_session, "E1", gzs.id)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    client.put(
        f"/api/employees/{emp.id}/attendance/2026-05",
        headers=h,
        json={"expected_days": "22", "actual_days": "20"},
    )
    client.put(
        f"/api/employees/{emp.id}/attendance/2026-05",
        headers=h,
        json={"expected_days": "22", "actual_days": "22"},
    )
    lst = client.get("/api/attendance?period=2026-05", headers=h).json()
    assert len(lst) == 1  # 未新增，覆盖
    assert lst[0]["actual_days"] == "22.00"


def test_store_manager_can_record_own_store(client, db_session):
    _gz, gzs, _szs = _orgs(db_session)
    emp = _emp(db_session, "E1", gzs.id)
    _user(db_session, "mgr", ["STORE_MANAGER"], scope_ids=[gzs.id])
    h = _token(client, "mgr")
    r = client.put(
        f"/api/employees/{emp.id}/attendance/2026-05",
        headers=h,
        json={"expected_days": "22", "actual_days": "22"},
    )
    assert r.status_code == 200


def test_store_manager_cannot_record_other_store(client, db_session):
    _gz, gzs, szs = _orgs(db_session)
    other = _emp(db_session, "SZ1", szs.id)
    _user(db_session, "mgr", ["STORE_MANAGER"], scope_ids=[gzs.id])
    h = _token(client, "mgr")
    r = client.put(
        f"/api/employees/{other.id}/attendance/2026-05",
        headers=h,
        json={"expected_days": "22", "actual_days": "22"},
    )
    assert r.status_code == 404  # 越权：他店员工不可见


def test_attendance_validation_rejects_out_of_range(client, db_session):
    _gz, gzs, _szs = _orgs(db_session)
    emp = _emp(db_session, "E1", gzs.id)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.put(
        f"/api/employees/{emp.id}/attendance/2026-05",
        headers=h,
        json={"expected_days": "22", "actual_days": "40"},  # >31
    )
    assert r.status_code == 422


def test_set_performance(client, db_session):
    _gz, gzs, _szs = _orgs(db_session)
    emp = _emp(db_session, "E1", gzs.id)
    _user(db_session, "hr", ["GROUP_HR"])
    h = _token(client, "hr")
    r = client.put(
        f"/api/employees/{emp.id}/performance/2026-05",
        headers=h,
        json={"coefficient": "1.200", "score": "88"},
    )
    assert r.status_code == 200
    assert r.json()["coefficient"] == "1.200"


def test_attendance_read_requires_permission(client, db_session):
    _orgs(db_session)
    _user(db_session, "emp", ["EMPLOYEE"])
    h = _token(client, "emp")
    assert client.get("/api/attendance?period=2026-05", headers=h).status_code == 403


def test_attendance_excel_import_scoped(client, db_session):
    _gz, gzs, szs = _orgs(db_session)
    e1 = _emp(db_session, "E1", gzs.id)  # noqa: F841 广州店，可见
    e2 = _emp(db_session, "E2", szs.id)  # noqa: F841 深圳店，越权
    _user(db_session, "mgr", ["STORE_MANAGER"], scope_ids=[gzs.id])
    h = _token(client, "mgr")

    wb = Workbook()
    ws = wb.active
    ws.append(["工号", "姓名", "应出勤", "实出勤", "加班"])
    ws.append(["E1", "甲", 22, 21, 5])
    ws.append(["E2", "乙", 22, 22, 0])  # 越权工号
    ws.append(["E9", "丙", 22, 22, 0])  # 不存在
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)

    r = client.post(
        "/api/attendance/import?period=2026-05",
        headers=h,
        files={"file": ("att.xlsx", buf, "application/vnd.ms-excel")},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["matched"] == 1  # 仅广州店 E1
    assert set(body["skipped"]) == {"E2", "E9"}  # 越权 + 不存在都跳过
