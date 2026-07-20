"""S12 integration coverage: policy input, engine output, and snapshot safety."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.comp import ComponentType
from app.models.employee import Department, EmploymentType
from app.payroll import batch_service
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    RuleConfig,
    StructureComponent,
    TaxYearToDate,
    compute,
)
from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    PayrollPolicyContext,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
)


def _policy() -> PayrollPolicyContext:
    rules = {
        kind: ContributionRule(
            kind=kind,
            employee_rate=Decimal("0"),
            employer_rate=Decimal("0"),
            base_min=Decimal("0"),
            base_max=None,
        )
        for kind in ContributionKind
    }
    rules[ContributionKind.PENSION] = ContributionRule(
        kind=ContributionKind.PENSION,
        employee_rate=Decimal("0.10"),
        employer_rate=Decimal("0.20"),
        base_min=Decimal("0"),
        base_max=Decimal("10000"),
    )
    rules[ContributionKind.WORK_INJURY] = ContributionRule(
        kind=ContributionKind.WORK_INJURY,
        employee_rate=Decimal("0"),
        employer_rate=Decimal("0.01"),
        base_min=Decimal("0"),
        base_max=None,
    )
    rules[ContributionKind.HOUSING] = ContributionRule(
        kind=ContributionKind.HOUSING,
        employee_rate=Decimal("0.05"),
        employer_rate=Decimal("0.05"),
        base_min=Decimal("0"),
        base_max=None,
    )
    return PayrollPolicyContext(
        policy_id=42,
        city="广州",
        effective_from=date(2026, 1, 1),
        social_policy=SocialInsurancePolicyInput(city="广州", rules=tuple(rules.values())),
        tax_policy=TaxPolicyInput(
            monthly_basic_deduction=Decimal("0"),
            brackets=(
                TaxBracket(
                    upper_bound=Decimal("36000"),
                    rate=Decimal("0.10"),
                    quick_deduction=Decimal("0"),
                ),
                TaxBracket(
                    upper_bound=None,
                    rate=Decimal("0.20"),
                    quick_deduction=Decimal("3600"),
                ),
            ),
        ),
    )


def _input(**overrides: object) -> EmployeeInput:
    values: dict[str, object] = {
        "employee_id": 1,
        "period": "2026-02",
        "days_in_month": Decimal("28"),
        "employment_type": EmploymentType.FULL_TIME,
        "department": Department.OTHER,
        "is_special_position": False,
        "structure": [
            StructureComponent(
                code="COMP",
                component_type=ComponentType.COMPREHENSIVE,
                amount=Decimal("10000"),
                taxable=True,
                in_social_base=True,
                in_housing_base=True,
            )
        ],
        "attendance": Attendance(expected_days=Decimal("20"), actual_days=Decimal("20")),
        "payroll_policy": _policy(),
        "monthly_special_deduction": Decimal("500"),
        "tax_ytd": TaxYearToDate(
            taxable_income_before=Decimal("10000"),
            employee_contribution_before=Decimal("1000"),
            special_deduction_before=Decimal("0"),
            tax_withheld_before=Decimal("900"),
        ),
    }
    values.update(overrides)
    return EmployeeInput(**values)


def _line(result, code: str):
    return next(item for item in result.lines if item.code == code)


def test_engine_applies_policy_bases_personal_and_employer_contributions_and_cumulative_tax() -> None:
    result = compute(_input())

    assert result.rule_version == "v3"
    assert _line(result, "SOCIAL_PENSION_EMPLOYEE").amount == Decimal("-1000.00")
    assert _line(result, "HOUSING_FUND_EMPLOYEE").amount == Decimal("-500.00")
    assert _line(result, "SOCIAL_PENSION_EMPLOYER").amount == Decimal("2000.00")
    assert _line(result, "IIT_WITHHOLDING").amount == Decimal("-800.00")
    assert result.tax_state is not None
    assert result.tax_state.current_taxable_income == Decimal("10000.00")
    assert result.tax_state.current_employee_contribution == Decimal("1500.00")
    assert result.tax_state.current_special_deduction == Decimal("500.00")
    assert result.tax_state.current_tax_withheld == Decimal("800.00")
    assert result.net == Decimal("7700.00")


def test_policy_enabled_payroll_blocks_a_deposit_shortfall_instead_of_deferring_tax_or_social() -> None:
    result = compute(_input(is_new_employee=True), RuleConfig(deposit_amount=Decimal("10000")))

    assert result.has_error
    assert any("defer" in message.lower() for message in result.exceptions)


def test_policy_and_tax_context_round_trip_through_the_immutable_input_snapshot() -> None:
    original = _input()

    snapshot = batch_service._input_snapshot(original, [])
    restored, missing = batch_service._input_from_snapshot(
        SimpleNamespace(rule_version="v3", input_snapshot=snapshot), {}
    )

    assert missing == []
    assert restored.payroll_policy == original.payroll_policy
    assert restored.monthly_special_deduction == Decimal("500")
    assert restored.tax_ytd == original.tax_ytd
    assert restored.structure[0].taxable is True
    assert restored.structure[0].in_social_base is True
    assert restored.structure[0].in_housing_base is True


def test_v2_snapshot_without_policy_context_remains_recomputable() -> None:
    legacy = _input(payroll_policy=None, monthly_special_deduction=Decimal("0"))
    snapshot = batch_service._input_snapshot(legacy, [])
    snapshot.pop("payroll_tax", None)

    restored, _missing = batch_service._input_from_snapshot(
        SimpleNamespace(rule_version="v2", input_snapshot=snapshot), {}
    )

    assert restored.payroll_policy is None
    assert compute(restored, RuleConfig(version="v2")).rule_version == "v2"
