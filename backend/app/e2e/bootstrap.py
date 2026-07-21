"""Seed the minimum auditable payroll world for the browser lifecycle E2E.

This module exposes no HTTP endpoint.  The CLI refuses to run unless both the
backend disposable-target marker and the explicit write opt-in are present.
All credentials are read from the environment and are never printed.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.bootstrap import seed_rbac
from app.core.security import hash_password
from app.models.attendance import AttendanceRecord, ExpectedAttendanceRule
from app.models.auth import Role, User, UserOrgScope, UserReviewScope, UserRole
from app.models.comp import ComponentType, EmployeeSalaryStructure, SalaryComponentDef
from app.models.employee import Department, Employee, EmployeeStatus, EmploymentType
from app.models.holiday import HolidayCalendarPeriod
from app.models.org import OrgType, OrgUnit
from app.models.payroll_policy import EmployeeTaxYtdOpening, PayrollPolicy
from app.payroll.social_tax import ContributionKind

E2E_PERIOD = "2026-05"
E2E_EMPLOYEE_NO = "E2E001"
E2E_EMPLOYEE_NAME = "E2E Payroll Employee"
E2E_STORE_CODE = "E2E-STORE"
E2E_STORE_NAME = "E2E Disposable Store"
E2E_CITY = "E2E-City"


def require_disposable_seed_environment(*, marker: str | None, allow_writes: str | None) -> None:
    """Fail closed before any database session is opened."""
    if not marker or not marker.strip():
        raise RuntimeError("COMP_E2E_TARGET_MARKER is required for E2E seed data")
    if allow_writes != "true":
        raise RuntimeError("E2E_ALLOW_WRITES=true is required for E2E seed data")


def _org_unit(
    session: Session,
    *,
    code: str,
    name: str,
    org_type: OrgType,
    parent_id: int | None = None,
    city: str | None = None,
) -> OrgUnit:
    org = session.scalars(select(OrgUnit).where(OrgUnit.code == code)).first()
    if org is None:
        org = OrgUnit(code=code, name=name, type=org_type)
        session.add(org)
    org.name = name
    org.type = org_type
    org.parent_id = parent_id
    org.city = city
    org.status = "ACTIVE"
    org.is_deleted = False
    org.deleted_at = None
    session.flush()
    return org


def _assign_role(session: Session, user: User, role_code: str) -> None:
    role_id = session.scalars(select(Role.id).where(Role.code == role_code)).one()
    exists = session.scalar(
        select(UserRole.id).where(UserRole.user_id == user.id, UserRole.role_id == role_id)
    )
    if exists is None:
        session.add(UserRole(user_id=user.id, role_id=role_id))


def seed_payroll_scenario(
    session: Session,
    *,
    admin_username: str,
    reviewer_username: str,
    reviewer_password: str,
) -> dict[str, int | str]:
    """Idempotently prepare one employee and the prerequisites for a locked payroll."""
    if not admin_username.strip():
        raise ValueError("E2E_USERNAME is required")
    if not reviewer_username.strip():
        raise ValueError("E2E_REVIEWER_USERNAME is required")
    if len(reviewer_password) < 12:
        raise ValueError("E2E_REVIEWER_PASSWORD must contain at least 12 characters")

    seed_rbac(session)
    admin = session.scalars(select(User).where(User.username == admin_username)).first()
    if admin is None or admin.is_deleted or admin.status != "ACTIVE":
        raise ValueError("The active E2E administrator must be bootstrapped before scenario seed")

    group = _org_unit(
        session,
        code="E2E-GROUP",
        name="E2E Disposable Group",
        org_type=OrgType.GROUP,
    )
    region = _org_unit(
        session,
        code="E2E-REGION",
        name="E2E Disposable Region",
        org_type=OrgType.REGION,
        parent_id=group.id,
    )
    store = _org_unit(
        session,
        code=E2E_STORE_CODE,
        name=E2E_STORE_NAME,
        org_type=OrgType.STORE,
        parent_id=region.id,
        city=E2E_CITY,
    )

    employee = session.scalars(select(Employee).where(Employee.emp_no == E2E_EMPLOYEE_NO)).first()
    if employee is None:
        employee = Employee(
            emp_no=E2E_EMPLOYEE_NO,
            name=E2E_EMPLOYEE_NAME,
            org_unit_id=store.id,
        )
        session.add(employee)
    employee.name = E2E_EMPLOYEE_NAME
    employee.org_unit_id = store.id
    employee.employment_type = EmploymentType.FULL_TIME
    employee.department = Department.OTHER
    employee.status = EmployeeStatus.ACTIVE
    employee.hire_date = date(2026, 1, 1)
    employee.leave_date = None
    employee.is_special_position = False
    employee.social_city = E2E_CITY
    employee.id_card = "440100199001010011"
    employee.bank_account = "6222020200000000000"
    employee.is_deleted = False
    employee.deleted_at = None
    session.flush()

    component = session.scalars(
        select(SalaryComponentDef).where(SalaryComponentDef.code == "E2E_COMP")
    ).first()
    if component is None:
        component = SalaryComponentDef(
            code="E2E_COMP",
            name="E2E Comprehensive Salary",
            component_type=ComponentType.COMPREHENSIVE,
        )
        session.add(component)
    component.name = "E2E Comprehensive Salary"
    component.component_type = ComponentType.COMPREHENSIVE
    component.taxable = True
    component.in_social_base = False
    component.in_housing_base = False
    component.allowance_kind = None
    component.is_deleted = False
    component.deleted_at = None
    session.flush()

    structure = session.scalars(
        select(EmployeeSalaryStructure).where(
            EmployeeSalaryStructure.employee_id == employee.id,
            EmployeeSalaryStructure.component_id == component.id,
            EmployeeSalaryStructure.effective_to.is_(None),
        )
    ).first()
    if structure is None:
        structure = EmployeeSalaryStructure(
            employee_id=employee.id,
            component_id=component.id,
            amount=Decimal("5000.00"),
            effective_from=date(2026, 1, 1),
        )
        session.add(structure)
    else:
        structure.amount = Decimal("5000.00")
        structure.effective_from = date(2026, 1, 1)

    schedule = session.scalars(
        select(ExpectedAttendanceRule).where(
            ExpectedAttendanceRule.name == "E2E fixed monthly schedule",
            ExpectedAttendanceRule.org_unit_id == store.id,
        )
    ).first()
    if schedule is None:
        schedule = ExpectedAttendanceRule(
            name="E2E fixed monthly schedule",
            org_unit_id=store.id,
            effective_from=date(2026, 1, 1),
        )
        session.add(schedule)
    schedule.employment_type = EmploymentType.FULL_TIME
    schedule.department = Department.OTHER
    schedule.weekly_rest_days = []
    schedule.monthly_expected_days = Decimal("22.00")
    schedule.effective_to = None
    schedule.priority = 100
    schedule.is_active = True
    session.flush()

    attendance = session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee.id,
            AttendanceRecord.period == E2E_PERIOD,
        )
    ).first()
    if attendance is None:
        attendance = AttendanceRecord(
            employee_id=employee.id,
            period=E2E_PERIOD,
            expected_days=Decimal("22.00"),
            actual_days=Decimal("22.00"),
        )
        session.add(attendance)
    attendance.generated_expected_days = Decimal("22.00")
    attendance.expected_days_rule_id = schedule.id
    attendance.expected_days = Decimal("22.00")
    attendance.expected_days_adjust_reason = None
    attendance.actual_days = Decimal("22.00")
    attendance.worked_hours = Decimal("0.00")
    attendance.rest_days = Decimal("0.00")
    attendance.overtime_hours = Decimal("0.00")
    attendance.holiday_worked_days = Decimal("0.00")
    attendance.leave_days = Decimal("0.00")

    calendar = session.scalars(
        select(HolidayCalendarPeriod).where(HolidayCalendarPeriod.period == E2E_PERIOD)
    ).first()
    if calendar is None:
        calendar = HolidayCalendarPeriod(period=E2E_PERIOD)
        session.add(calendar)
    calendar.is_finalized = True
    calendar.finalized_by = admin.id
    calendar.finalized_at = datetime.now(UTC)

    policy = session.scalars(
        select(PayrollPolicy).where(
            PayrollPolicy.city == E2E_CITY,
            PayrollPolicy.effective_from == date(2026, 1, 1),
        )
    ).first()
    if policy is None:
        policy = PayrollPolicy(
            city=E2E_CITY,
            effective_from=date(2026, 1, 1),
            social_rules=[],
            monthly_basic_deduction=Decimal("5000.00"),
            tax_brackets=[],
        )
        session.add(policy)
    policy.social_rules = [
        {
            "kind": kind.value,
            "employee_rate": "0",
            "employer_rate": "0",
            "base_min": "0",
            "base_max": None,
        }
        for kind in ContributionKind
    ]
    policy.monthly_basic_deduction = Decimal("5000.00")
    policy.tax_brackets = [{"upper_bound": None, "rate": "0", "quick_deduction": "0"}]
    policy.derived_income_rules = [
        {
            "code": code,
            "taxable": False,
            "in_social_base": False,
            "in_housing_base": False,
        }
        for code in ("OVERTIME", "HOLIDAY")
    ]
    policy.is_finalized = True
    policy.finalized_by = admin.id
    policy.finalized_at = datetime.now(UTC)

    opening = session.scalars(
        select(EmployeeTaxYtdOpening).where(
            EmployeeTaxYtdOpening.employee_id == employee.id,
            EmployeeTaxYtdOpening.tax_year == 2026,
            EmployeeTaxYtdOpening.superseded_at.is_(None),
        )
    ).first()
    if opening is None:
        opening = EmployeeTaxYtdOpening(
            employee_id=employee.id,
            tax_year=2026,
            revision=1,
            through_period="2026-04",
            employment_months_to_date=4,
            taxable_income=Decimal("0.00"),
            employee_contribution=Decimal("0.00"),
            special_deduction=Decimal("0.00"),
            tax_withheld=Decimal("0.00"),
            evidence_ref="Disposable browser E2E opening",
        )
        session.add(opening)
    opening.through_period = "2026-04"
    opening.employment_months_to_date = 4
    opening.taxable_income = Decimal("0.00")
    opening.employee_contribution = Decimal("0.00")
    opening.special_deduction = Decimal("0.00")
    opening.tax_withheld = Decimal("0.00")
    opening.evidence_ref = "Disposable browser E2E opening"
    opening.is_finalized = True
    opening.finalized_by = admin.id
    opening.finalized_at = datetime.now(UTC)

    reviewer = session.scalars(select(User).where(User.username == reviewer_username)).first()
    if reviewer is None:
        reviewer = User(username=reviewer_username, password_hash=hash_password(reviewer_password))
        session.add(reviewer)
    else:
        reviewer.password_hash = hash_password(reviewer_password)
    reviewer.employee_id = employee.id
    reviewer.status = "ACTIVE"
    reviewer.is_deleted = False
    reviewer.deleted_at = None
    session.flush()
    _assign_role(session, reviewer, "STORE_MANAGER")
    _assign_role(session, reviewer, "EMPLOYEE")

    org_scope = session.scalar(
        select(UserOrgScope.id).where(
            UserOrgScope.user_id == reviewer.id,
            UserOrgScope.org_unit_id == store.id,
        )
    )
    if org_scope is None:
        session.add(UserOrgScope(user_id=reviewer.id, org_unit_id=store.id))
    review_scope = session.scalar(
        select(UserReviewScope.id).where(
            UserReviewScope.user_id == reviewer.id,
            UserReviewScope.org_unit_id == store.id,
            UserReviewScope.department == Department.OTHER,
        )
    )
    if review_scope is None:
        session.add(
            UserReviewScope(
                user_id=reviewer.id,
                org_unit_id=store.id,
                department=Department.OTHER,
            )
        )

    session.flush()
    return {
        "period": E2E_PERIOD,
        "employee_id": employee.id,
        "store_id": store.id,
        "reviewer_user_id": reviewer.id,
    }


def main() -> None:  # pragma: no cover - exercised by the disposable Docker E2E
    import os
    import sys

    from app.core.config import get_settings
    from app.db.session import SessionLocal

    try:
        require_disposable_seed_environment(
            marker=get_settings().e2e_target_marker,
            allow_writes=os.environ.get("E2E_ALLOW_WRITES"),
        )
        admin_username = os.environ.get("E2E_USERNAME", "")
        reviewer_username = os.environ.get("E2E_REVIEWER_USERNAME", "")
        reviewer_password = os.environ.get("E2E_REVIEWER_PASSWORD", "")
        with SessionLocal() as session:
            result = seed_payroll_scenario(
                session,
                admin_username=admin_username,
                reviewer_username=reviewer_username,
                reviewer_password=reviewer_password,
            )
            session.commit()
    except (RuntimeError, ValueError) as exc:
        print(f"E2E payroll seed refused: {exc}", file=sys.stderr)
        raise SystemExit(1) from None

    print(
        "Seeded disposable payroll browser scenario "
        f"for period {result['period']} and employee ID {result['employee_id']}."
    )


if __name__ == "__main__":  # pragma: no cover
    main()
