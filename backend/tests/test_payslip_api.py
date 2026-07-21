from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
from app.models.employee import Department, Employee
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


def _user(session, username: str, *, employee_id: int | None) -> User:
    seed_rbac(session)
    user = User(
        username=username,
        password_hash=hash_password("StrongPass123!"),
        employee_id=employee_id,
    )
    session.add(user)
    session.flush()
    role = session.scalars(select(Role).where(Role.code == "EMPLOYEE")).one()
    session.add(UserRole(user_id=user.id, role_id=role.id))
    session.flush()
    return user


def _token(client, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"username": username, "password": "StrongPass123!"},
    )
    assert response.status_code == 200
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _employee(session, emp_no: str) -> Employee:
    store = session.scalars(select(OrgUnit).where(OrgUnit.code == "S1")).first()
    if store is None:
        store = OrgUnit(code="S1", name="Store", type=OrgType.STORE, city="Guangzhou")
        session.add(store)
        session.flush()
    employee = Employee(emp_no=emp_no, name=emp_no, org_unit_id=store.id)
    session.add(employee)
    session.flush()
    return employee


def _result(
    session,
    *,
    employee: Employee,
    period: str,
    status: BatchStatus = BatchStatus.LOCKED,
    version: int = 1,
    net: str = "5000",
) -> PayrollResult:
    year, month = (int(part) for part in period.split("-"))
    batch = session.scalars(select(PayrollBatch).where(PayrollBatch.period == period)).first()
    if batch is None:
        batch = PayrollBatch(
            period=period,
            attendance_start=date(year, month, 1),
            attendance_end=date(year, month, 28),
            status=status,
            version=1,
            locked_at=datetime.now(UTC) if status == BatchStatus.LOCKED else None,
        )
        session.add(batch)
        session.flush()
    else:
        assert batch.status == status
    result = PayrollResult(
        batch_id=batch.id,
        employee_id=employee.id,
        batch_version=1,
        version=version,
        org_unit_id=employee.org_unit_id,
        department=Department.OTHER,
        actual_attendance_days=Decimal("22"),
        gross=Decimal(net),
        deposit=Decimal("0"),
        net=Decimal(net),
        carry_forward=Decimal("0"),
        rule_version="v1",
        input_snapshot={},
        lines=[
            {
                "code": "BASE",
                "category": "EARNING",
                "formula": "base salary",
                "amount": net,
            }
        ],
        exceptions=[],
        warnings=[],
        has_error=False,
    )
    session.add(result)
    session.flush()
    return result


def test_employee_can_only_view_own_locked_payslip_and_view_is_audited(client, db_session):
    employee = _employee(db_session, "E1")
    other_employee = _employee(db_session, "E2")
    user = _user(db_session, "employee", employee_id=employee.id)
    _user(db_session, "other-employee", employee_id=other_employee.id)
    result = _result(db_session, employee=employee, period="2026-05", net="5100")
    _result(db_session, employee=other_employee, period="2026-05", net="9999")
    _result(
        db_session,
        employee=employee,
        period="2026-04",
        status=BatchStatus.DRAFT,
        net="4900",
    )
    headers = _token(client, "employee")

    periods = client.get("/api/payslips/me/periods", headers=headers)
    assert periods.status_code == 200
    assert periods.json() and [item["period"] for item in periods.json()] == ["2026-05"]

    response = client.get("/api/payslips/me?period=2026-05", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["net"] == "5100.00"
    assert body["lines"] == [
        {"code": "BASE", "category": "EARNING", "formula": "base salary", "amount": "5100.00"}
    ]
    assert client.get("/api/payslips/me?period=2026-04", headers=headers).status_code == 404

    audit_row = db_session.scalars(
        select(AuditLog)
        .where(AuditLog.action == "payslip.view", AuditLog.target_id == result.id)
        .order_by(AuditLog.id.desc())
    ).first()
    assert audit_row is not None
    assert audit_row.actor_user_id == user.id
    assert audit_row.detail == {
        "period": "2026-05",
        "batch_id": result.batch_id,
        "result_version": 1,
    }


def test_payslip_prefers_latest_result_for_the_locked_batch(client, db_session):
    employee = _employee(db_session, "E1")
    _user(db_session, "employee", employee_id=employee.id)
    first = _result(db_session, employee=employee, period="2026-05", net="5000")
    latest = PayrollResult(
        batch_id=first.batch_id,
        employee_id=employee.id,
        batch_version=1,
        version=2,
        org_unit_id=employee.org_unit_id,
        department=Department.OTHER,
        actual_attendance_days=Decimal("22"),
        gross=Decimal("5200"),
        deposit=Decimal("0"),
        net=Decimal("5200"),
        carry_forward=Decimal("0"),
        rule_version="v1",
        input_snapshot={},
        lines=[{"code": "BASE", "category": "EARNING", "formula": "updated", "amount": "5200"}],
        exceptions=[],
        warnings=[],
        has_error=False,
    )
    db_session.add(latest)
    db_session.flush()

    response = client.get("/api/payslips/me?period=2026-05", headers=_token(client, "employee"))
    assert response.status_code == 200
    assert response.json()["net"] == "5200.00"


def test_unbound_employee_account_cannot_access_payslip(client, db_session):
    _user(db_session, "unbound", employee_id=None)
    headers = _token(client, "unbound")

    assert client.get("/api/payslips/me/periods", headers=headers).status_code == 404
    assert client.get("/api/payslips/me?period=2026-05", headers=headers).status_code == 404
