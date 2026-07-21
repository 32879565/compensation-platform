from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.auth.bootstrap import seed_rbac
from app.comp.service import set_component_amount
from app.core.security import hash_password
from app.models.attendance import AttendanceRecord, ExpectedAttendanceRule
from app.models.audit import AuditLog
from app.models.auth import Role, User, UserRole
from app.models.comp import ComponentType, SalaryComponentDef
from app.models.employee import Employee
from app.models.holiday import HolidayCalendarPeriod
from app.models.org import OrgType, OrgUnit
from app.models.payroll_policy import EmployeeTaxYtdOpening, PayrollPolicy
from app.payroll.social_tax import ContributionKind

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

    from app.db.session import get_session
    from app.main import app

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
    emp = Employee(
        emp_no="E1",
        name="张三",
        org_unit_id=store.id,
        social_city="广州",
        hire_date=date(2026, 1, 1),
    )
    session.add(emp)
    session.flush()
    opening_auditor = User(
        username=f"opening-auditor-{emp.id}",
        password_hash=hash_password("StrongPass123!"),
    )
    session.add(opening_auditor)
    session.flush()
    comp = SalaryComponentDef(
        code="COMP", name="综合薪资", component_type=ComponentType.COMPREHENSIVE
    )
    session.add(comp)
    session.flush()
    set_component_amount(
        session,
        employee_id=emp.id,
        component_id=comp.id,
        amount=Decimal("5000"),
        effective_from=date(2026, 1, 1),
    )
    schedule_rule = ExpectedAttendanceRule(
        name="Preview schedule",
        weekly_rest_days=[],
        monthly_expected_days=Decimal("22"),
        effective_from=date(2026, 1, 1),
    )
    session.add_all([schedule_rule, HolidayCalendarPeriod(period="2026-05", is_finalized=True)])
    session.flush()
    if with_attendance:
        session.add(
            AttendanceRecord(
                employee_id=emp.id,
                period="2026-05",
                generated_expected_days=Decimal("22"),
                expected_days_rule_id=schedule_rule.id,
                expected_days=Decimal("22"),
                actual_days=Decimal("22"),
            )
        )
        session.flush()
    session.add_all(
        [
            PayrollPolicy(
                city="广州",
                effective_from=date(2026, 1, 1),
                social_rules=[
                    {
                        "kind": kind.value,
                        "employee_rate": "0",
                        "employer_rate": "0",
                        "base_min": "0",
                        "base_max": None,
                    }
                    for kind in ContributionKind
                ],
                monthly_basic_deduction=Decimal("5000"),
                tax_brackets=[{"upper_bound": None, "rate": "0", "quick_deduction": "0"}],
                is_finalized=True,
            ),
            EmployeeTaxYtdOpening(
                employee_id=emp.id,
                tax_year=2026,
                through_period="2026-04",
                employment_months_to_date=4,
                taxable_income=Decimal("0"),
                employee_contribution=Decimal("0"),
                special_deduction=Decimal("0"),
                tax_withheld=Decimal("0"),
                evidence_ref="test tax opening",
                is_finalized=True,
                finalized_by=opening_auditor.id,
                finalized_at=datetime.now(UTC),
            ),
        ]
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
    assert body["gross"] == "5000.00"  # 综合5000/应出勤22×实际22（OTHER 部门=应出勤−休息）
    assert body["rule_version"] == "v4"
    assert any(li["code"] == "ATTEND_WAGE" for li in body["lines"])
    audit_entry = db_session.scalars(
        select(AuditLog).where(AuditLog.action == "payroll.preview.view")
    ).one()
    assert audit_entry.target_id == emp.id
    assert audit_entry.detail == {"period": "2026-05", "has_error": False}


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
