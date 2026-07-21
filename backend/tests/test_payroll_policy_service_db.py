"""PostgreSQL coverage for loading S12 inputs into individual and batch payrolls."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

import pytest

from app.comp.service import set_component_amount
from app.core.security import hash_password
from app.models.attendance import AttendanceRecord, ExpectedAttendanceRule, PerformanceRecord
from app.models.auth import User
from app.models.comp import ComponentType, SalaryComponentDef
from app.models.employee import Department, Employee
from app.models.holiday import HolidayCalendarPeriod, StatutoryHolidayDate
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_policy import EmployeeTaxDeduction, EmployeeTaxYtdOpening, PayrollPolicy
from app.models.payroll_result import PayrollResult
from app.payroll.service import build_input, build_inputs
from app.payroll.social_tax import ContributionKind

pytestmark = pytest.mark.usefixtures("pg_engine")


def _policy_rules() -> list[dict[str, str | None]]:
    return [
        {
            "kind": kind.value,
            "employee_rate": "0",
            "employer_rate": "0",
            "base_min": "0",
            "base_max": None,
        }
        for kind in ContributionKind
    ]


def _tax_snapshot(
    *,
    taxable: str,
    contribution: str,
    special: str,
    withheld: str,
    employment_months: int,
) -> dict[str, object]:
    return {
        "tax_withholding": {
            "current_taxable_income": taxable,
            "current_employee_contribution": contribution,
            "current_special_deduction": special,
            "current_tax_withheld": withheld,
            "employment_months_to_date": employment_months,
        }
    }


def _locked_result(
    session,
    batch: PayrollBatch,
    employee: Employee,
    *,
    version: int,
    snapshot: dict[str, object],
) -> None:
    session.add(
        PayrollResult(
            batch_id=batch.id,
            employee_id=employee.id,
            batch_version=batch.version,
            version=version,
            org_unit_id=employee.org_unit_id,
            department=employee.department,
            actual_attendance_days=Decimal("22"),
            statutory_holiday_days=Decimal("0"),
            statutory_holiday_worked_days=Decimal("0"),
            gross=Decimal("10000"),
            deposit=Decimal("0"),
            net=Decimal("10000"),
            carry_forward=Decimal("0"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("0"),
            rule_version="v4",
            input_snapshot=snapshot,
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )


def test_tax_opening_orm_metadata_keeps_the_migration_checks() -> None:
    constraint_names = {
        constraint.name
        for constraint in EmployeeTaxYtdOpening.__table__.constraints
        if constraint.name is not None
    }

    assert {
        "ck_tax_opening_year",
        "ck_tax_opening_period",
        "ck_tax_opening_employment_months",
        "ck_tax_opening_taxable_income",
        "ck_tax_opening_employee_contribution",
        "ck_tax_opening_special_deduction",
        "ck_tax_opening_tax_withheld",
        "ck_tax_opening_evidence_ref",
        "ck_tax_opening_finalization",
    }.issubset(constraint_names)


def test_build_input_fail_closes_when_legacy_employee_has_no_hire_date(db_session) -> None:
    store = OrgUnit(code="NO-HIRE", name="Legacy store", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="NO-HIRE-E1",
        name="Legacy employee",
        org_unit_id=store.id,
        department=Department.DINING,
        hire_date=None,
    )
    db_session.add(employee)
    db_session.flush()

    payroll_input, missing = build_input(db_session, employee, "2026-07")

    assert missing == []
    assert payroll_input.holiday_eligible is False
    assert "Employee hire date is required for payroll eligibility." in (
        payroll_input.source_exceptions
    )


def test_build_input_carries_a_deferred_deposit_without_unpaid_wages(db_session) -> None:
    store = OrgUnit(code="DEFERRED-ONLY", name="Deferred store", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="DEFERRED-ONLY-E1",
        name="Deferred employee",
        org_unit_id=store.id,
        department=Department.OTHER,
        hire_date=date(2026, 5, 1),
    )
    prior_batch = PayrollBatch(
        period="2026-05",
        attendance_start=date(2026, 5, 1),
        attendance_end=date(2026, 5, 31),
        status=BatchStatus.LOCKED,
        version=1,
    )
    db_session.add_all([employee, prior_batch])
    db_session.flush()
    db_session.add(
        PayrollResult(
            batch_id=prior_batch.id,
            employee_id=employee.id,
            batch_version=1,
            version=1,
            org_unit_id=store.id,
            department=Department.OTHER,
            actual_attendance_days=Decimal("0"),
            statutory_holiday_days=Decimal("0"),
            statutory_holiday_worked_days=Decimal("0"),
            gross=Decimal("0"),
            deposit=Decimal("0"),
            net=Decimal("0"),
            carry_forward=Decimal("0"),
            deferred_deductions=Decimal("0"),
            deferred_deposit=Decimal("600"),
            rule_version="v4",
            input_snapshot={"is_new_employee": True},
            lines=[],
            exceptions=[],
            warnings=[],
            has_error=False,
        )
    )
    db_session.flush()

    payroll_input, _missing = build_input(db_session, employee, "2026-06")

    assert payroll_input.prior_carry_forward == Decimal("0")
    assert payroll_input.prior_deferred_deductions == Decimal("0")
    assert payroll_input.prior_deferred_deposit == Decimal("600")


def test_mid_month_new_hire_uses_structure_effective_on_the_hire_date(db_session) -> None:
    store = OrgUnit(code="MID-HIRE", name="Mid-month hire store", type=OrgType.STORE)
    employee = Employee(
        emp_no="MID-HIRE-E1",
        name="Mid-month hire",
        org_unit_id=0,
        department=Department.OTHER,
        hire_date=date(2026, 5, 20),
    )
    component = SalaryComponentDef(
        code="MID_HIRE_COMP",
        name="Mid-hire comprehensive salary",
        component_type=ComponentType.COMPREHENSIVE,
    )
    db_session.add_all([store, component])
    db_session.flush()
    employee.org_unit_id = store.id
    db_session.add(employee)
    db_session.flush()
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=component.id,
        amount=Decimal("6000"),
        effective_from=date(2026, 5, 20),
    )

    individual, individual_missing = build_input(db_session, employee, "2026-05")
    bulk, bulk_missing = build_inputs(db_session, [employee], "2026-05")[employee.id]

    assert individual_missing == []
    assert bulk_missing == []
    assert [(item.code, item.amount) for item in individual.structure] == [
        ("MID_HIRE_COMP", Decimal("6000"))
    ]
    assert [(item.code, item.amount) for item in bulk.structure] == [
        ("MID_HIRE_COMP", Decimal("6000"))
    ]


@pytest.mark.parametrize(
    ("probation_end", "leave_date", "expected_error"),
    [
        (
            date(2025, 12, 31),
            None,
            "Employee probation end cannot precede the hire date.",
        ),
        (
            None,
            date(2025, 12, 31),
            "Employee leave date cannot precede the hire date.",
        ),
    ],
)
def test_build_input_fail_closes_when_legacy_employee_lifecycle_dates_are_invalid(
    db_session, probation_end, leave_date, expected_error
) -> None:
    store = OrgUnit(
        code=f"INVALID-DATE-{expected_error[:4]}", name="Legacy store", type=OrgType.STORE
    )
    db_session.add_all(
        [
            store,
            HolidayCalendarPeriod(period="2026-07", is_finalized=True),
            StatutoryHolidayDate(
                holiday_date=date(2026, 7, 1),
                name="Lifecycle regression holiday",
                eligible_employment_types=["FULL_TIME"],
            ),
        ]
    )
    db_session.flush()
    employee = Employee(
        emp_no=f"INVALID-DATE-E{store.id}",
        name="Legacy employee",
        org_unit_id=store.id,
        department=Department.DINING,
        hire_date=date(2026, 1, 1),
        probation_end=probation_end,
        leave_date=leave_date,
    )
    db_session.add(employee)
    db_session.flush()

    individual, _missing = build_input(db_session, employee, "2026-07")
    bulk, _bulk_missing = build_inputs(db_session, [employee], "2026-07")[employee.id]

    assert individual.holiday_eligible is False
    assert bulk.holiday_eligible is False
    assert individual.statutory_holidays == ()
    assert bulk.statutory_holidays == ()
    assert expected_error in individual.source_exceptions
    assert expected_error in bulk.source_exceptions


def test_build_input_classifies_named_special_position_even_for_legacy_flag(db_session) -> None:
    store = OrgUnit(code="SPECIAL", name="Special-role store", type=OrgType.STORE)
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="SPECIAL-E1",
        name="Legacy dishwasher",
        org_unit_id=store.id,
        department=Department.KITCHEN,
        position_title="洗碗岗位",
        is_special_position=False,
        hire_date=date(2026, 1, 1),
    )
    db_session.add(employee)
    db_session.flush()

    individual, _missing = build_input(db_session, employee, "2026-07")
    bulk, _bulk_missing = build_inputs(db_session, [employee], "2026-07")[employee.id]

    assert individual.is_special_position is True
    assert bulk.is_special_position is True


def test_build_inputs_loads_effective_policy_deduction_flags_and_locked_ytd(db_session) -> None:
    store = OrgUnit(code="S12", name="Policy store", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    employee = Employee(
        emp_no="S12-E1",
        name="Policy employee",
        org_unit_id=store.id,
        department=Department.OTHER,
        social_city="广州",
        hire_date=date(2026, 1, 1),
        probation_end=date(2026, 7, 31),
    )
    component = SalaryComponentDef(
        code="S12_COMP",
        name="S12 comprehensive pay",
        component_type=ComponentType.COMPREHENSIVE,
        taxable=True,
        in_social_base=True,
        in_housing_base=True,
    )
    schedule = ExpectedAttendanceRule(
        name="S12 schedule",
        weekly_rest_days=[],
        monthly_expected_days=Decimal("22"),
        effective_from=date(2026, 1, 1),
    )
    db_session.add_all(
        [employee, component, schedule, HolidayCalendarPeriod(period="2026-07", is_finalized=True)]
    )
    db_session.flush()
    opening_auditor = User(
        username="s12-opening-auditor",
        password_hash=hash_password("StrongPass123!"),
    )
    db_session.add(opening_auditor)
    db_session.flush()
    opening = EmployeeTaxYtdOpening(
        employee_id=employee.id,
        tax_year=2026,
        through_period="2026-04",
        employment_months_to_date=4,
        taxable_income=Decimal("40000"),
        employee_contribution=Decimal("2000"),
        special_deduction=Decimal("300"),
        tax_withheld=Decimal("1000"),
        evidence_ref="migration-test-opening",
        is_finalized=True,
        finalized_by=opening_auditor.id,
        finalized_at=datetime.now(UTC),
    )
    set_component_amount(
        db_session,
        employee_id=employee.id,
        component_id=component.id,
        amount=Decimal("10000"),
        effective_from=date(2026, 1, 1),
    )
    db_session.add(
        AttendanceRecord(
            employee_id=employee.id,
            period="2026-07",
            generated_expected_days=Decimal("22"),
            expected_days_rule_id=schedule.id,
            expected_days=Decimal("22"),
            actual_days=Decimal("22"),
        )
    )
    policy = PayrollPolicy(
        city="广州",
        effective_from=date(2026, 1, 1),
        social_rules=_policy_rules(),
        monthly_basic_deduction=Decimal("5000"),
        tax_brackets=[
            {"upper_bound": "36000", "rate": "0.03", "quick_deduction": "0"},
            {"upper_bound": None, "rate": "0.10", "quick_deduction": "2520"},
        ],
        is_finalized=True,
    )
    # A more recent draft and future policy must not affect July's immutable input.
    draft = PayrollPolicy(
        city="广州",
        effective_from=date(2026, 6, 1),
        social_rules=_policy_rules(),
        monthly_basic_deduction=Decimal("9999"),
        tax_brackets=[{"upper_bound": None, "rate": "0", "quick_deduction": "0"}],
        is_finalized=False,
    )
    future = PayrollPolicy(
        city="广州",
        effective_from=date(2026, 8, 1),
        social_rules=_policy_rules(),
        monthly_basic_deduction=Decimal("9999"),
        tax_brackets=[{"upper_bound": None, "rate": "0", "quick_deduction": "0"}],
        is_finalized=True,
    )
    db_session.add_all(
        [
            policy,
            draft,
            future,
            EmployeeTaxDeduction(
                employee_id=employee.id,
                effective_from=date(2026, 5, 1),
                monthly_special_deduction=Decimal("100"),
            ),
            EmployeeTaxDeduction(
                employee_id=employee.id,
                effective_from=date(2026, 6, 1),
                monthly_special_deduction=Decimal("300"),
            ),
            PerformanceRecord(
                employee_id=employee.id,
                period="2026-07",
                coefficient=Decimal("1.200"),
            ),
            opening,
        ]
    )
    may_batch = PayrollBatch(
        period="2026-05",
        attendance_start=date(2026, 5, 1),
        attendance_end=date(2026, 5, 31),
        status=BatchStatus.LOCKED,
        version=1,
    )
    june_batch = PayrollBatch(
        period="2026-06",
        attendance_start=date(2026, 6, 1),
        attendance_end=date(2026, 6, 30),
        status=BatchStatus.LOCKED,
        version=1,
    )
    db_session.add_all([may_batch, june_batch])
    db_session.flush()
    _locked_result(
        db_session,
        may_batch,
        employee,
        version=1,
        snapshot=_tax_snapshot(
            taxable="9000", contribution="400", special="100", withheld="100", employment_months=5
        ),
    )
    _locked_result(
        db_session,
        may_batch,
        employee,
        version=2,
        snapshot=_tax_snapshot(
            taxable="10000", contribution="500", special="200", withheld="150", employment_months=5
        ),
    )
    _locked_result(
        db_session,
        june_batch,
        employee,
        version=1,
        snapshot=_tax_snapshot(
            taxable="11000", contribution="600", special="300", withheld="200", employment_months=6
        ),
    )
    db_session.flush()

    individual, missing = build_input(db_session, employee, "2026-07")
    bulk, bulk_missing = build_inputs(db_session, [employee], "2026-07")[employee.id]

    assert missing == bulk_missing == []
    assert individual.source_exceptions == ()
    assert individual.payroll_policy is not None
    assert individual.payroll_policy.policy_id == policy.id
    assert individual.monthly_special_deduction == Decimal("300")
    assert individual.tax_employment_months == 7
    assert individual.tax_ytd.taxable_income_before == Decimal("61000")
    assert individual.tax_ytd.employee_contribution_before == Decimal("3100")
    assert individual.tax_ytd.special_deduction_before == Decimal("800")
    assert individual.tax_ytd.tax_withheld_before == Decimal("1350")
    assert individual.tax_ytd.employment_months_before == 6
    assert individual.tax_opening is not None
    assert individual.tax_opening.opening_id == opening.id
    assert individual.tax_opening.revision == 1
    assert individual.tax_opening.through_period == "2026-04"
    assert individual.tax_opening.evidence_ref == "migration-test-opening"
    assert individual.performance_coefficient == Decimal("1.200")
    assert individual.probation_end == date(2026, 7, 31)
    assert individual.structure[0].taxable is True
    assert individual.structure[0].in_social_base is True
    assert individual.structure[0].in_housing_base is True
    assert bulk == individual
