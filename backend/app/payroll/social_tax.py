"""Pure, configurable social-insurance and cumulative-tax calculations.

The module deliberately contains no statutory rates.  Effective policy records
provide every rate, base cap, bracket and deduction; callers persist the exact
policy snapshot alongside a payroll result before relying on these functions.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum

_CENTS = Decimal("0.01")
_ZERO = Decimal("0")


class PolicyValidationError(ValueError):
    """Raised when policy data or a cumulative calculation input is invalid."""


class ContributionKind(StrEnum):
    PENSION = "PENSION"
    MEDICAL = "MEDICAL"
    UNEMPLOYMENT = "UNEMPLOYMENT"
    WORK_INJURY = "WORK_INJURY"
    MATERNITY = "MATERNITY"
    HOUSING = "HOUSING"


@dataclass(frozen=True)
class ContributionRule:
    kind: ContributionKind
    employee_rate: Decimal
    employer_rate: Decimal
    base_min: Decimal
    base_max: Decimal | None


@dataclass(frozen=True)
class SocialInsurancePolicyInput:
    city: str
    rules: tuple[ContributionRule, ...]


@dataclass(frozen=True)
class ContributionLine:
    kind: ContributionKind
    base: Decimal
    employee_amount: Decimal
    employer_amount: Decimal


@dataclass(frozen=True)
class SocialInsuranceResult:
    lines: tuple[ContributionLine, ...]
    employee_total: Decimal
    employer_total: Decimal


@dataclass(frozen=True)
class TaxBracket:
    """One cumulative taxable-income range; ``None`` is the terminal range."""

    upper_bound: Decimal | None
    rate: Decimal
    quick_deduction: Decimal


@dataclass(frozen=True)
class TaxPolicyInput:
    monthly_basic_deduction: Decimal
    brackets: tuple[TaxBracket, ...]


@dataclass(frozen=True)
class CumulativeTaxInput:
    month: int
    ytd_taxable_income_before: Decimal
    ytd_employee_contribution_before: Decimal
    ytd_special_deduction_before: Decimal
    ytd_tax_withheld_before: Decimal
    current_taxable_income: Decimal
    current_employee_contribution: Decimal
    current_special_deduction: Decimal


@dataclass(frozen=True)
class CumulativeTaxResult:
    cumulative_taxable_income: Decimal
    cumulative_tax_due: Decimal
    current_withholding: Decimal


def _q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


def _require_finite_nonnegative(value: Decimal, name: str) -> None:
    if not value.is_finite() or value < _ZERO:
        raise PolicyValidationError(f"{name} must be a finite non-negative amount.")


def _validate_rate(value: Decimal, name: str) -> None:
    if not value.is_finite() or value < _ZERO or value > Decimal("1"):
        raise PolicyValidationError(f"{name} must be a finite rate from 0 to 1.")


def _validated_social_rules(policy: SocialInsurancePolicyInput) -> tuple[ContributionRule, ...]:
    if not policy.city.strip():
        raise PolicyValidationError("social-insurance policy city is required.")
    seen: set[ContributionKind] = set()
    for rule in policy.rules:
        if rule.kind in seen:
            raise PolicyValidationError("social-insurance policy has duplicate contribution kinds.")
        seen.add(rule.kind)
        _validate_rate(rule.employee_rate, f"{rule.kind}.employee_rate")
        _validate_rate(rule.employer_rate, f"{rule.kind}.employer_rate")
        _require_finite_nonnegative(rule.base_min, f"{rule.kind}.base_min")
        if rule.base_max is not None:
            _require_finite_nonnegative(rule.base_max, f"{rule.kind}.base_max")
            if rule.base_max < rule.base_min:
                raise PolicyValidationError(f"{rule.kind}.base_max must not be below base_min.")
    if seen != set(ContributionKind):
        raise PolicyValidationError("social-insurance policy must include all contribution kinds.")
    return tuple(sorted(policy.rules, key=lambda rule: rule.kind.value))


def calculate_social_insurance(
    *,
    policy: SocialInsurancePolicyInput,
    social_base: Decimal,
    housing_base: Decimal,
) -> SocialInsuranceResult:
    """Calculate personal and employer contributions using policy-defined caps.

    A rule's minimum applies even when the caller's eligible payroll base is
    zero.  A policy for an exempt group must explicitly use a zero minimum and
    zero rates; this preserves a fail-closed, auditable configuration surface.
    """

    _require_finite_nonnegative(social_base, "social_base")
    _require_finite_nonnegative(housing_base, "housing_base")
    rules = _validated_social_rules(policy)
    lines: list[ContributionLine] = []
    employee_total = _ZERO
    employer_total = _ZERO
    for rule in rules:
        raw_base = housing_base if rule.kind == ContributionKind.HOUSING else social_base
        contribution_base = max(raw_base, rule.base_min)
        if rule.base_max is not None:
            contribution_base = min(contribution_base, rule.base_max)
        contribution_base = _q(contribution_base)
        employee_amount = _q(contribution_base * rule.employee_rate)
        employer_amount = _q(contribution_base * rule.employer_rate)
        lines.append(
            ContributionLine(
                kind=rule.kind,
                base=contribution_base,
                employee_amount=employee_amount,
                employer_amount=employer_amount,
            )
        )
        employee_total += employee_amount
        employer_total += employer_amount
    return SocialInsuranceResult(
        lines=tuple(lines),
        employee_total=_q(employee_total),
        employer_total=_q(employer_total),
    )


def _validated_tax_brackets(policy: TaxPolicyInput) -> tuple[TaxBracket, ...]:
    _require_finite_nonnegative(policy.monthly_basic_deduction, "monthly_basic_deduction")
    if not policy.brackets:
        raise PolicyValidationError("tax policy requires at least one bracket.")
    finite_upper_bound = _ZERO
    for index, bracket in enumerate(policy.brackets):
        _validate_rate(bracket.rate, f"bracket[{index}].rate")
        _require_finite_nonnegative(bracket.quick_deduction, f"bracket[{index}].quick_deduction")
        if bracket.upper_bound is None:
            if index != len(policy.brackets) - 1:
                raise PolicyValidationError("only the final tax bracket may have no upper bound.")
            continue
        _require_finite_nonnegative(bracket.upper_bound, f"bracket[{index}].upper_bound")
        if bracket.upper_bound <= finite_upper_bound:
            raise PolicyValidationError("tax bracket upper bounds must be strictly increasing.")
        if index == len(policy.brackets) - 1:
            raise PolicyValidationError(
                "tax policy requires a terminal bracket without an upper bound."
            )
        finite_upper_bound = bracket.upper_bound
    if policy.brackets[-1].upper_bound is not None:
        raise PolicyValidationError(
            "tax policy requires a terminal bracket without an upper bound."
        )
    return policy.brackets


def _validated_tax_input(input: CumulativeTaxInput) -> None:
    if (
        isinstance(input.month, bool)
        or not isinstance(input.month, int)
        or not 1 <= input.month <= 12
    ):
        raise PolicyValidationError("month must be an integer from 1 to 12.")
    for name in (
        "ytd_taxable_income_before",
        "ytd_employee_contribution_before",
        "ytd_special_deduction_before",
        "ytd_tax_withheld_before",
        "current_taxable_income",
        "current_employee_contribution",
        "current_special_deduction",
    ):
        _require_finite_nonnegative(getattr(input, name), name)


def _tax_bracket_for(taxable_income: Decimal, brackets: tuple[TaxBracket, ...]) -> TaxBracket:
    for bracket in brackets:
        if bracket.upper_bound is None or taxable_income <= bracket.upper_bound:
            return bracket
    raise PolicyValidationError("tax policy has no terminal bracket.")  # defensive; validated above


def calculate_cumulative_tax(
    *, policy: TaxPolicyInput, input: CumulativeTaxInput
) -> CumulativeTaxResult:
    """Calculate this month's withholding from cumulative, already-locked inputs.

    The result never creates a negative monthly withholding.  Any prior
    over-withholding is retained for annual reconciliation instead of silently
    becoming an unconfigured refund rule.
    """

    brackets = _validated_tax_brackets(policy)
    _validated_tax_input(input)
    cumulative_taxable_income = max(
        _ZERO,
        input.ytd_taxable_income_before
        + input.current_taxable_income
        - input.ytd_employee_contribution_before
        - input.current_employee_contribution
        - input.ytd_special_deduction_before
        - input.current_special_deduction
        - policy.monthly_basic_deduction * input.month,
    )
    cumulative_taxable_income = _q(cumulative_taxable_income)
    bracket = _tax_bracket_for(cumulative_taxable_income, brackets)
    cumulative_tax_due = max(
        _ZERO,
        _q(cumulative_taxable_income * bracket.rate - bracket.quick_deduction),
    )
    current_withholding = max(_ZERO, _q(cumulative_tax_due - input.ytd_tax_withheld_before))
    return CumulativeTaxResult(
        cumulative_taxable_income=cumulative_taxable_income,
        cumulative_tax_due=cumulative_tax_due,
        current_withholding=current_withholding,
    )
