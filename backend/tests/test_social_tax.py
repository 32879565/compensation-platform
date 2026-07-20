from decimal import Decimal

import pytest

from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    CumulativeTaxInput,
    PolicyValidationError,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
    calculate_cumulative_tax,
    calculate_social_insurance,
)


def _social_policy(*overrides: ContributionRule) -> SocialInsurancePolicyInput:
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
    rules.update({rule.kind: rule for rule in overrides})
    return SocialInsurancePolicyInput(city="广州", rules=tuple(rules.values()))


def _tax_policy() -> TaxPolicyInput:
    return TaxPolicyInput(
        monthly_basic_deduction=Decimal("5000"),
        brackets=(
            TaxBracket(
                upper_bound=Decimal("36000"), rate=Decimal("0.03"), quick_deduction=Decimal("0")
            ),
            TaxBracket(
                upper_bound=Decimal("144000"), rate=Decimal("0.10"), quick_deduction=Decimal("2520")
            ),
            TaxBracket(upper_bound=None, rate=Decimal("0.20"), quick_deduction=Decimal("16920")),
        ),
    )


def test_social_insurance_clamps_each_contribution_base_and_keeps_both_shares() -> None:
    policy = _social_policy(
        ContributionRule(
            kind=ContributionKind.PENSION,
            employee_rate=Decimal("0.08"),
            employer_rate=Decimal("0.16"),
            base_min=Decimal("3000"),
            base_max=Decimal("8000"),
        ),
        ContributionRule(
            kind=ContributionKind.MEDICAL,
            employee_rate=Decimal("0.02"),
            employer_rate=Decimal("0.06"),
            base_min=Decimal("3000"),
            base_max=None,
        ),
        ContributionRule(
            kind=ContributionKind.HOUSING,
            employee_rate=Decimal("0.07"),
            employer_rate=Decimal("0.07"),
            base_min=Decimal("4000"),
            base_max=Decimal("10000"),
        ),
    )

    result = calculate_social_insurance(
        policy=policy,
        social_base=Decimal("12000"),
        housing_base=Decimal("2000"),
    )

    by_kind = {line.kind: line for line in result.lines}
    assert by_kind[ContributionKind.PENSION].base == Decimal("8000.00")
    assert by_kind[ContributionKind.PENSION].employee_amount == Decimal("640.00")
    assert by_kind[ContributionKind.PENSION].employer_amount == Decimal("1280.00")
    assert by_kind[ContributionKind.MEDICAL].base == Decimal("12000.00")
    assert by_kind[ContributionKind.HOUSING].base == Decimal("4000.00")
    assert result.employee_total == Decimal("1160.00")
    assert result.employer_total == Decimal("2280.00")


def test_social_insurance_rejects_incomplete_or_duplicate_fund_policies() -> None:
    incomplete = SocialInsurancePolicyInput(
        city="广州",
        rules=(
            ContributionRule(
                kind=ContributionKind.PENSION,
                employee_rate=Decimal("0.08"),
                employer_rate=Decimal("0.16"),
                base_min=Decimal("0"),
                base_max=None,
            ),
        ),
    )
    duplicate = SocialInsurancePolicyInput(
        city="广州",
        rules=(
            ContributionRule(
                kind=ContributionKind.PENSION,
                employee_rate=Decimal("0"),
                employer_rate=Decimal("0"),
                base_min=Decimal("0"),
                base_max=None,
            ),
            ContributionRule(
                kind=ContributionKind.PENSION,
                employee_rate=Decimal("0"),
                employer_rate=Decimal("0"),
                base_min=Decimal("0"),
                base_max=None,
            ),
        ),
    )

    with pytest.raises(PolicyValidationError, match="all contribution kinds"):
        calculate_social_insurance(
            policy=incomplete,
            social_base=Decimal("5000"),
            housing_base=Decimal("5000"),
        )
    with pytest.raises(PolicyValidationError, match="duplicate"):
        calculate_social_insurance(
            policy=duplicate,
            social_base=Decimal("5000"),
            housing_base=Decimal("5000"),
        )


def test_cumulative_tax_withholding_uses_ytd_inputs_and_crosses_a_bracket() -> None:
    result = calculate_cumulative_tax(
        policy=_tax_policy(),
        input=CumulativeTaxInput(
            employment_months=2,
            ytd_taxable_income_before=Decimal("10000"),
            ytd_employee_contribution_before=Decimal("1000"),
            ytd_special_deduction_before=Decimal("1000"),
            ytd_tax_withheld_before=Decimal("90"),
            current_taxable_income=Decimal("50000"),
            current_employee_contribution=Decimal("2000"),
            current_special_deduction=Decimal("0"),
        ),
    )

    assert result.cumulative_taxable_income == Decimal("46000.00")
    assert result.cumulative_tax_due == Decimal("2080.00")
    assert result.current_withholding == Decimal("1990.00")


def test_cumulative_tax_uses_employment_months_not_calendar_month() -> None:
    """A May hire receives one, not five, monthly basic deductions."""

    result = calculate_cumulative_tax(
        policy=_tax_policy(),
        input=CumulativeTaxInput(
            employment_months=1,
            ytd_taxable_income_before=Decimal("0"),
            ytd_employee_contribution_before=Decimal("0"),
            ytd_special_deduction_before=Decimal("0"),
            ytd_tax_withheld_before=Decimal("0"),
            current_taxable_income=Decimal("20000"),
            current_employee_contribution=Decimal("0"),
            current_special_deduction=Decimal("0"),
        ),
    )

    assert result.cumulative_taxable_income == Decimal("15000.00")
    assert result.current_withholding == Decimal("450.00")


def test_cumulative_tax_uses_the_lower_bracket_at_its_exact_boundary() -> None:
    result = calculate_cumulative_tax(
        policy=_tax_policy(),
        input=CumulativeTaxInput(
            employment_months=1,
            ytd_taxable_income_before=Decimal("0"),
            ytd_employee_contribution_before=Decimal("0"),
            ytd_special_deduction_before=Decimal("0"),
            ytd_tax_withheld_before=Decimal("0"),
            current_taxable_income=Decimal("41000"),
            current_employee_contribution=Decimal("0"),
            current_special_deduction=Decimal("0"),
        ),
    )

    assert result.cumulative_taxable_income == Decimal("36000.00")
    assert result.current_withholding == Decimal("1080.00")


def test_cumulative_tax_never_creates_a_negative_monthly_withholding() -> None:
    result = calculate_cumulative_tax(
        policy=_tax_policy(),
        input=CumulativeTaxInput(
            employment_months=1,
            ytd_taxable_income_before=Decimal("0"),
            ytd_employee_contribution_before=Decimal("0"),
            ytd_special_deduction_before=Decimal("0"),
            ytd_tax_withheld_before=Decimal("200"),
            current_taxable_income=Decimal("6000"),
            current_employee_contribution=Decimal("0"),
            current_special_deduction=Decimal("0"),
        ),
    )

    assert result.current_withholding == Decimal("0.00")


def test_cumulative_tax_rejects_invalid_employment_months_or_unsorted_brackets() -> None:
    with pytest.raises(PolicyValidationError, match="employment_months"):
        calculate_cumulative_tax(
            policy=_tax_policy(),
            input=CumulativeTaxInput(
                employment_months=13,
                ytd_taxable_income_before=Decimal("0"),
                ytd_employee_contribution_before=Decimal("0"),
                ytd_special_deduction_before=Decimal("0"),
                ytd_tax_withheld_before=Decimal("0"),
                current_taxable_income=Decimal("0"),
                current_employee_contribution=Decimal("0"),
                current_special_deduction=Decimal("0"),
            ),
        )
    unsorted = TaxPolicyInput(
        monthly_basic_deduction=Decimal("5000"),
        brackets=(
            TaxBracket(
                upper_bound=Decimal("144000"), rate=Decimal("0.10"), quick_deduction=Decimal("2520")
            ),
            TaxBracket(
                upper_bound=Decimal("36000"), rate=Decimal("0.03"), quick_deduction=Decimal("0")
            ),
            TaxBracket(upper_bound=None, rate=Decimal("0.20"), quick_deduction=Decimal("16920")),
        ),
    )
    with pytest.raises(PolicyValidationError, match="strictly increasing"):
        calculate_cumulative_tax(
            policy=unsorted,
            input=CumulativeTaxInput(
                employment_months=1,
                ytd_taxable_income_before=Decimal("0"),
                ytd_employee_contribution_before=Decimal("0"),
                ytd_special_deduction_before=Decimal("0"),
                ytd_tax_withheld_before=Decimal("0"),
                current_taxable_income=Decimal("0"),
                current_employee_contribution=Decimal("0"),
                current_special_deduction=Decimal("0"),
            ),
        )
