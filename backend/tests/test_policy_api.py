from datetime import date

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
from app.models.payroll_batch import BatchStatus, PayrollBatch

pytestmark = pytest.mark.usefixtures("pg_engine")


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


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "StrongPass123!"}
    )
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _body(*, city: str = "广州", effective_from: str = "2026-01-01") -> dict:
    return {
        "city": city,
        "effective_from": effective_from,
        "social_rules": [
            {
                "kind": "PENSION",
                "employee_rate": "0.08",
                "employer_rate": "0.16",
                "base_min": "3000",
                "base_max": "8000",
            },
            {
                "kind": "MEDICAL",
                "employee_rate": "0.02",
                "employer_rate": "0.06",
                "base_min": "3000",
                "base_max": "12000",
            },
            {
                "kind": "UNEMPLOYMENT",
                "employee_rate": "0.005",
                "employer_rate": "0.005",
                "base_min": "3000",
                "base_max": "12000",
            },
            {
                "kind": "WORK_INJURY",
                "employee_rate": "0",
                "employer_rate": "0.004",
                "base_min": "3000",
                "base_max": "12000",
            },
            {
                "kind": "MATERNITY",
                "employee_rate": "0",
                "employer_rate": "0.008",
                "base_min": "3000",
                "base_max": "12000",
            },
            {
                "kind": "HOUSING",
                "employee_rate": "0.07",
                "employer_rate": "0.07",
                "base_min": "4000",
                "base_max": "10000",
            },
        ],
        "monthly_basic_deduction": "5000",
        "tax_brackets": [
            {"upper_bound": "36000", "rate": "0.03", "quick_deduction": "0"},
            {"upper_bound": "144000", "rate": "0.1", "quick_deduction": "2520"},
            {"upper_bound": None, "rate": "0.2", "quick_deduction": "16920"},
        ],
        "derived_income_rules": [
            {
                "code": "OVERTIME",
                "taxable": True,
                "in_social_base": True,
                "in_housing_base": False,
            },
            {
                "code": "HOLIDAY",
                "taxable": True,
                "in_social_base": False,
                "in_housing_base": False,
            },
        ],
    }


def test_group_hr_can_create_finalize_and_resolve_an_effective_payroll_policy(client, db_session):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    created = client.post("/api/payroll-policies", headers=headers, json=_body())

    assert created.status_code == 201, created.text
    policy_id = created.json()["id"]
    assert created.json()["is_finalized"] is False
    assert created.json()["derived_income_rules"] == _body()["derived_income_rules"]
    finalized = client.post(f"/api/payroll-policies/{policy_id}/finalize", headers=headers)
    assert finalized.status_code == 200, finalized.text
    assert finalized.json()["is_finalized"] is True

    resolved = client.get(
        "/api/payroll-policies/active",
        headers=headers,
        params={"city": "广州", "on_date": "2026-05-01"},
    )
    assert resolved.status_code == 200
    assert resolved.json()["id"] == policy_id
    assert resolved.json()["effective_from"] == "2026-01-01"
    actions = {row.action for row in db_session.scalars(select(AuditLog)).all()}
    assert {"payroll_policy.create", "payroll_policy.finalize"}.issubset(actions)


def test_finalized_policy_is_immutable_and_a_newer_effective_policy_wins(client, db_session):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    first = client.post("/api/payroll-policies", headers=headers, json=_body()).json()
    assert (
        client.post(f"/api/payroll-policies/{first['id']}/finalize", headers=headers).status_code
        == 200
    )

    changed_draft = client.post(
        "/api/payroll-policies",
        headers=headers,
        json=_body(effective_from="2026-07-01"),
    )
    assert changed_draft.status_code == 201
    second = changed_draft.json()
    assert (
        client.post(f"/api/payroll-policies/{second['id']}/finalize", headers=headers).status_code
        == 200
    )

    assert (
        client.patch(
            f"/api/payroll-policies/{first['id']}",
            headers=headers,
            json={"monthly_basic_deduction": "6000"},
        ).status_code
        == 409
    )
    resolved = client.get(
        "/api/payroll-policies/active",
        headers=headers,
        params={"city": "广州", "on_date": date(2026, 7, 1).isoformat()},
    )
    assert resolved.status_code == 200
    assert resolved.json()["id"] == second["id"]


def test_reopened_current_batch_allows_an_explicit_successor_policy(client, db_session):
    _user(db_session, "hr", ["GROUP_HR"])
    headers = _token(client, "hr")
    original = client.post("/api/payroll-policies", headers=headers, json=_body()).json()
    assert client.post(f"/api/payroll-policies/{original['id']}/finalize", headers=headers).status_code == 200
    db_session.add(
        PayrollBatch(
            period="2026-05",
            attendance_start=date(2026, 5, 1),
            attendance_end=date(2026, 5, 31),
            status=BatchStatus.DRAFT,
            version=2,
        )
    )
    db_session.flush()

    successor = client.post(
        "/api/payroll-policies",
        headers=headers,
        json=_body(effective_from="2026-05-01"),
    )
    assert successor.status_code == 201, successor.text

    finalized = client.post(
        f"/api/payroll-policies/{successor.json()['id']}/finalize", headers=headers
    )

    assert finalized.status_code == 200, finalized.text


def test_policy_api_requires_policy_permissions_and_rejects_duplicate_city_effective_date(
    client, db_session
):
    _user(db_session, "employee", ["EMPLOYEE"])
    _user(db_session, "hr", ["GROUP_HR"])
    employee_headers = _token(client, "employee")
    hr_headers = _token(client, "hr")

    assert client.get("/api/payroll-policies", headers=employee_headers).status_code == 403
    assert (
        client.post("/api/payroll-policies", headers=employee_headers, json=_body()).status_code
        == 403
    )
    assert client.post("/api/payroll-policies", headers=hr_headers, json=_body()).status_code == 201
    assert client.post("/api/payroll-policies", headers=hr_headers, json=_body()).status_code == 409
