"""Audited employee tax-opening management API coverage."""

from datetime import date

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.auth import Role, User, UserRole
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


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
    with TestClient(app) as test_client:
        yield test_client
    app.dependency_overrides.clear()


def _user(session, username: str) -> User:
    seed_rbac(session)
    user = User(username=username, password_hash=hash_password("StrongPass123!"))
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == "GROUP_HR")).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _headers(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _employee(session) -> Employee:
    store = OrgUnit(code="TAX", name="Tax store", type=OrgType.STORE, city="Guangzhou")
    session.add(store)
    session.flush()
    employee = Employee(
        emp_no="TAX-E1",
        name="Tax employee",
        org_unit_id=store.id,
        social_city="Guangzhou",
        hire_date=date(2026, 1, 1),
    )
    session.add(employee)
    session.flush()
    return employee


def _body(*, taxable_income: str = "40000", evidence_ref: str = "migration://signed-ytd") -> dict:
    return {
        "tax_year": 2026,
        "through_period": "2026-04",
        "employment_months_to_date": 4,
        "taxable_income": taxable_income,
        "employee_contribution": "2000",
        "special_deduction": "300",
        "tax_withheld": "1000",
        "evidence_ref": evidence_ref,
    }


def test_hr_can_create_finalize_and_supersede_an_audited_tax_opening(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    headers = _headers(client, "hr")

    created = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )

    assert created.status_code == 201, created.text
    assert created.json()["is_finalized"] is False
    opening_id = created.json()["id"]
    finalized = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{opening_id}/finalize",
        headers=headers,
    )
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["is_finalized"] is True
    assert finalized.json()["revision"] == 1
    assert finalized.json()["finalized_by"] is not None

    replacement = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{opening_id}/supersede",
        headers=headers,
        json=_body(taxable_income="41000", evidence_ref="migration://corrected-ytd"),
    )
    assert replacement.status_code == 201, replacement.text
    assert replacement.json()["revision"] == 2
    replacement_id = replacement.json()["id"]
    assert replacement.json()["supersedes_id"] == opening_id

    replacement_finalized = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{replacement_id}/finalize",
        headers=headers,
    )
    assert replacement_finalized.status_code == 200, replacement_finalized.text
    openings = client.get(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers
    )
    assert openings.status_code == 200
    assert [item["revision"] for item in openings.json()] == [2, 1]
    assert openings.json()[0]["superseded_at"] is None
    assert openings.json()[1]["superseded_at"] is not None
