from datetime import date

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.auth import Role, User, UserOrgScope, UserRole
from app.models.employee import Employee
from app.models.holiday import HolidayWorkRecord, StatutoryHolidayDate
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def _store(session, code: str) -> OrgUnit:
    group = OrgUnit(code=f"G_{code}", name="集团", type=OrgType.GROUP)
    session.add(group)
    session.flush()
    store = OrgUnit(code=code, name=f"{code}门店", type=OrgType.STORE, parent_id=group.id)
    session.add(store)
    session.flush()
    return store


def _user(session, username: str, role_code: str, scope_ids: tuple[int, ...] = ()) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == role_code)).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    for org_unit_id in scope_ids:
        session.add(UserOrgScope(user_id=user.id, org_unit_id=org_unit_id))
    session.flush()
    return user


def _headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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


def test_lists_employee_holiday_work_for_selected_period(client, db_session):
    store = _store(db_session, "S_GZ")
    employee = Employee(emp_no="E1001", name="陈星", org_unit_id=store.id)
    db_session.add(employee)
    db_session.flush()
    db_session.add_all(
        [
            StatutoryHolidayDate(
                holiday_date=date(2026, 5, 1),
                name="劳动节",
                eligible_employment_types=["FULL_TIME"],
            ),
            HolidayWorkRecord(
                employee_id=employee.id,
                holiday_date=date(2026, 5, 1),
                worked=True,
                reason="门店排班",
                evidence_url="https://evidence.example/shift-1",
            ),
        ]
    )
    _user(db_session, "hr", "GROUP_HR")

    response = client.get(
        f"/api/holiday-calendar/employees/{employee.id}/work",
        params={"period": "2026-05"},
        headers=_headers(client, "hr"),
    )

    assert response.status_code == 200, response.text
    assert response.json() == [
        {
            "employee_id": employee.id,
            "holiday_date": "2026-05-01",
            "worked": True,
            "reason": "门店排班",
            "evidence_url": "https://evidence.example/shift-1",
        }
    ]


def test_holiday_work_list_hides_employee_outside_attendance_scope(client, db_session):
    own_store = _store(db_session, "S_OWN")
    other_store = _store(db_session, "S_OTHER")
    employee = Employee(emp_no="E2001", name="越权员工", org_unit_id=other_store.id)
    db_session.add(employee)
    db_session.flush()
    _user(db_session, "manager", "STORE_MANAGER", (own_store.id,))

    response = client.get(
        f"/api/holiday-calendar/employees/{employee.id}/work",
        params={"period": "2026-05"},
        headers=_headers(client, "manager"),
    )

    assert response.status_code == 404


def test_holiday_work_keeps_period_store_scope_after_employee_transfer(client, db_session):
    original_store = _store(db_session, "S_ORIGINAL")
    new_store = _store(db_session, "S_NEW")
    employee = Employee(emp_no="E3001", name="调店员工", org_unit_id=original_store.id)
    db_session.add_all(
        [
            employee,
            StatutoryHolidayDate(
                holiday_date=date(2026, 5, 1),
                name="劳动节",
                eligible_employment_types=["FULL_TIME"],
            ),
        ]
    )
    db_session.flush()
    _user(db_session, "original_manager", "STORE_MANAGER", (original_store.id,))
    _user(db_session, "new_manager", "STORE_MANAGER", (new_store.id,))

    created = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=_headers(client, "original_manager"),
        json={
            "worked": True,
            "reason": "原门店排班",
            "evidence_url": "https://evidence.example/original-shift",
        },
    )
    assert created.status_code == 200, created.text
    record = db_session.scalars(
        select(HolidayWorkRecord).where(HolidayWorkRecord.employee_id == employee.id)
    ).one()
    assert record.org_unit_id == original_store.id

    employee.org_unit_id = new_store.id
    db_session.commit()

    original_read = client.get(
        f"/api/holiday-calendar/employees/{employee.id}/work",
        params={"period": "2026-05"},
        headers=_headers(client, "original_manager"),
    )
    assert original_read.status_code == 200, original_read.text
    assert len(original_read.json()) == 1

    new_store_read = client.get(
        f"/api/holiday-calendar/employees/{employee.id}/work",
        params={"period": "2026-05"},
        headers=_headers(client, "new_manager"),
    )
    assert new_store_read.status_code == 200
    assert new_store_read.json() == []

    new_store_write = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=_headers(client, "new_manager"),
        json={"worked": False, "reason": "越权覆盖"},
    )
    assert new_store_write.status_code == 404

    original_write = client.put(
        f"/api/holiday-calendar/employees/{employee.id}/work/2026-05-01",
        headers=_headers(client, "original_manager"),
        json={"worked": False, "reason": "原门店复核更正"},
    )
    assert original_write.status_code == 200, original_write.text
