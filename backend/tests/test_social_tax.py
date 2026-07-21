from decimal import Decimal
from typing import cast

import pytest

from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    CumulativeTaxInput,
    DerivedIncomeRule,
    PolicyValidationError,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
    calculate_cumulative_tax,
    calculate_social_insurance,
    validate_derived_income_rules,
    validate_social_insurance_policy,
    validate_tax_policy,
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


@pytest.mark.parametrize(
    ("policy", "social_base", "message"),
    [
        pytest.param(
            SocialInsurancePolicyInput(city="  ", rules=_social_policy().rules),
            Decimal("0"),
            "city is required",
            id="blank-city",
        ),
        pytest.param(
            _social_policy(
                ContributionRule(
                    kind=ContributionKind.PENSION,
                    employee_rate=Decimal("1.01"),
                    employer_rate=Decimal("0"),
                    base_min=Decimal("0"),
                    base_max=None,
                )
            ),
            Decimal("0"),
            "rate from 0 to 1",
            id="employee-rate-above-one",
        ),
        pytest.param(
            _social_policy(
                ContributionRule(
                    kind=ContributionKind.PENSION,
                    employee_rate=Decimal("0"),
                    employer_rate=Decimal("0"),
                    base_min=Decimal("3000"),
                    base_max=Decimal("2999"),
                )
            ),
            Decimal("0"),
            "must not be below base_min",
            id="cap-below-minimum",
        ),
        pytest.param(
            _social_policy(),
            Decimal("-0.01"),
            "social_base must be a finite non-negative amount",
            id="negative-eligible-base",
        ),
    ],
)
def test_social_insurance_rejects_invalid_policy_or_eligible_base(
    policy: SocialInsurancePolicyInput, social_base: Decimal, message: str
) -> None:
    with pytest.raises(PolicyValidationError, match=message):
        calculate_social_insurance(
            policy=policy,
            social_base=social_base,
            housing_base=Decimal("0"),
        )


def test_social_policy_validator_accepts_a_complete_configured_policy() -> None:
    """A policy may be finalized only after every statutory fund is present."""

    validate_social_insurance_policy(_social_policy())


def test_derived_income_rules_require_complete_explicit_classification() -> None:
    rules = (
        DerivedIncomeRule(
            code="OVERTIME",
            taxable=True,
            in_social_base=True,
            in_housing_base=False,
        ),
        DerivedIncomeRule(
            code="HOLIDAY",
            taxable=False,
            in_social_base=False,
            in_housing_base=False,
        ),
    )

    validate_derived_income_rules(rules, require_complete=True)
    with pytest.raises(PolicyValidationError, match="missing: OVERTIME"):
        validate_derived_income_rules((rules[1],), require_complete=True)


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        pytest.param(
            (
                DerivedIncomeRule(
                    code="BONUS",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
            ),
            "unsupported derived income code",
            id="unknown-calculation-line",
        ),
        pytest.param(
            (
                DerivedIncomeRule(
                    code="OVERTIME",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
                DerivedIncomeRule(
                    code="OVERTIME",
                    taxable=False,
                    in_social_base=False,
                    in_housing_base=False,
                ),
            ),
            "duplicate codes",
            id="duplicate-line-treatment",
        ),
        pytest.param(
            (
                DerivedIncomeRule(
                    code="OVERTIME",
                    taxable=False,
                    in_social_base=False,
                    in_housing_base=cast(bool, "yes"),
                ),
            ),
            "flags must be boolean",
            id="nonboolean-policy-flag",
        ),
    ],
)
def test_derived_income_rules_reject_unusable_classifications(
    rules: tuple[DerivedIncomeRule, ...], message: str
) -> None:
    with pytest.raises(PolicyValidationError, match=message):
        validate_derived_income_rules(rules)


@pytest.mark.parametrize(
    ("policy", "message"),
    [
        pytest.param(
            TaxPolicyInput(monthly_basic_deduction=Decimal("5000"), brackets=()),
            "at least one bracket",
            id="no-brackets",
        ),
        pytest.param(
            TaxPolicyInput(
                monthly_basic_deduction=Decimal("5000"),
                brackets=(
                    TaxBracket(
                        upper_bound=None, rate=Decimal("0.03"), quick_deduction=Decimal("0")
                    ),
                    TaxBracket(
                        upper_bound=None,
                        rate=Decimal("0.10"),
                        quick_deduction=Decimal("2520"),
                    ),
                ),
            ),
            "only the final tax bracket",
            id="open-ended-bracket-before-final",
        ),
        pytest.param(
            TaxPolicyInput(
                monthly_basic_deduction=Decimal("5000"),
                brackets=(
                    TaxBracket(
                        upper_bound=Decimal("36000"),
                        rate=Decimal("0.03"),
                        quick_deduction=Decimal("0"),
                    ),
                ),
            ),
            "terminal bracket without an upper bound",
            id="finite-final-bracket",
        ),
    ],
)
def test_tax_policy_validator_rejects_malformed_terminal_ranges(
    policy: TaxPolicyInput, message: str
) -> None:
    with pytest.raises(PolicyValidationError, match=message):
        validate_tax_policy(policy)


def test_cumulative_tax_clamps_a_negative_ytd_tax_base_to_zero() -> None:
    result = calculate_cumulative_tax(
        policy=_tax_policy(),
        input=CumulativeTaxInput(
            employment_months=1,
            ytd_taxable_income_before=Decimal("1000"),
            ytd_employee_contribution_before=Decimal("1000"),
            ytd_special_deduction_before=Decimal("1000"),
            ytd_tax_withheld_before=Decimal("0"),
            current_taxable_income=Decimal("1000"),
            current_employee_contribution=Decimal("0"),
            current_special_deduction=Decimal("0"),
        ),
    )

    assert result.cumulative_taxable_income == Decimal("0.00")
    assert result.cumulative_tax_due == Decimal("0.00")
    assert result.current_withholding == Decimal("0.00")
