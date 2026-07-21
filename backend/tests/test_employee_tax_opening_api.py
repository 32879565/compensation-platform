"""Audited employee tax-opening management API coverage."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.auth import Role, User, UserRole
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import PayrollResult

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
    openings = client.get(f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers)
    assert openings.status_code == 200
    assert [item["revision"] for item in openings.json()] == [2, 1]
    assert openings.json()[0]["superseded_at"] is None
    assert openings.json()[1]["superseded_at"] is not None


def test_all_reopened_affected_batches_allow_a_tax_opening_correction(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    headers = _headers(client, "hr")
    db_session.add_all(
        [
            PayrollBatch(
                period="2026-05",
                attendance_start=date(2026, 5, 1),
                attendance_end=date(2026, 5, 31),
                status=BatchStatus.DRAFT,
                version=2,
            ),
            PayrollBatch(
                period="2026-06",
                attendance_start=date(2026, 6, 1),
                attendance_end=date(2026, 6, 30),
                status=BatchStatus.DRAFT,
                version=2,
            ),
        ]
    )
    db_session.flush()

    created = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )
    assert created.status_code == 201, created.text

    finalized = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{created.json()['id']}/finalize",
        headers=headers,
    )

    assert finalized.status_code == 200, finalized.text


def test_pending_downstream_batch_blocks_a_tax_opening_correction(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    headers = _headers(client, "hr")
    db_session.add_all(
        [
            PayrollBatch(
                period="2026-05",
                attendance_start=date(2026, 5, 1),
                attendance_end=date(2026, 5, 31),
                status=BatchStatus.DRAFT,
                version=2,
            ),
            PayrollBatch(
                period="2026-06",
                attendance_start=date(2026, 6, 1),
                attendance_end=date(2026, 6, 30),
                status=BatchStatus.PENDING_HR,
                version=1,
            ),
        ]
    )
    db_session.flush()

    created = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )
    assert created.status_code == 201, created.text

    finalized = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{created.json()['id']}/finalize",
        headers=headers,
    )

    assert finalized.status_code == 409


def test_tax_opening_correction_ignores_history_outside_its_tax_year(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    employee.hire_date = date(2025, 1, 1)
    headers = _headers(client, "hr")
    previous_year = PayrollBatch(
        period="2025-12",
        attendance_start=date(2025, 12, 1),
        attendance_end=date(2025, 12, 31),
        status=BatchStatus.LOCKED,
        version=1,
    )
    next_year = PayrollBatch(
        period="2027-01",
        attendance_start=date(2027, 1, 1),
        attendance_end=date(2027, 1, 31),
        status=BatchStatus.PENDING_HR,
        version=1,
    )
    db_session.add_all([previous_year, next_year])
    db_session.flush()
    db_session.add(
        PayrollResult(
            batch_id=previous_year.id,
            employee_id=employee.id,
            batch_version=1,
            version=1,
            org_unit_id=employee.org_unit_id,
            department=employee.department,
            actual_attendance_days=Decimal("20"),
            statutory_holiday_days=Decimal("0"),
            statutory_holiday_worked_days=Decimal("0"),
            gross=Decimal("1000"),
            deposit=Decimal("0"),
            net=Decimal("1000"),
            carry_forward=Decimal("0"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("0"),
            rule_version="v4",
            input_snapshot={},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    db_session.flush()
    created = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )
    assert created.status_code == 201, created.text

    finalized = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{created.json()['id']}/finalize",
        headers=headers,
    )

    assert finalized.status_code == 200, finalized.text


def test_finalization_revalidates_a_draft_after_hire_date_changes(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    headers = _headers(client, "hr")
    created = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )
    assert created.status_code == 201, created.text
    employee.hire_date = date(2026, 3, 1)
    db_session.flush()

    finalized = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{created.json()['id']}/finalize",
        headers=headers,
    )

    assert finalized.status_code == 422


def test_successor_draft_cannot_change_its_predecessor_tax_year(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    headers = _headers(client, "hr")
    original = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )
    assert original.status_code == 201, original.text
    assert (
        client.post(
            f"/api/employees/{employee.id}/tax-ytd-openings/{original.json()['id']}/finalize",
            headers=headers,
        ).status_code
        == 200
    )
    successor = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings/{original.json()['id']}/supersede",
        headers=headers,
        json=_body(taxable_income="41000", evidence_ref="migration://corrected-ytd"),
    )
    assert successor.status_code == 201, successor.text
    changed_year = _body()
    changed_year["tax_year"] = 2027
    changed_year["through_period"] = "2027-04"

    patched = client.patch(
        f"/api/employees/{employee.id}/tax-ytd-openings/{successor.json()['id']}",
        headers=headers,
        json=changed_year,
    )

    assert patched.status_code == 422


def test_initial_draft_tax_year_collision_returns_conflict(client, db_session):
    _user(db_session, "hr")
    employee = _employee(db_session)
    headers = _headers(client, "hr")
    first = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=_body()
    )
    assert first.status_code == 201, first.text
    next_year = _body()
    next_year["tax_year"] = 2027
    next_year["through_period"] = "2027-04"
    second = client.post(
        f"/api/employees/{employee.id}/tax-ytd-openings", headers=headers, json=next_year
    )
    assert second.status_code == 201, second.text

    conflict = client.patch(
        f"/api/employees/{employee.id}/tax-ytd-openings/{second.json()['id']}",
        headers=headers,
        json=_body(),
    )

    assert conflict.status_code == 409
