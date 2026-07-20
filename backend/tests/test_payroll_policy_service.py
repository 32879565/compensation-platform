"""S12 service-layer policy selection and cumulative-input safety tests."""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from types import SimpleNamespace

from app.payroll import service
from app.payroll.engine import TaxYearToDate
from app.payroll.social_tax import ContributionKind


def _social_rules() -> list[dict[str, str | None]]:
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


def _policy(
    policy_id: int,
    effective_from: date,
    *,
    city: str = "广州",
    finalized: bool = True,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=policy_id,
        city=city,
        effective_from=effective_from,
        is_finalized=finalized,
        social_rules=_social_rules(),
        monthly_basic_deduction=Decimal("5000"),
        tax_brackets=[
            {"upper_bound": "36000", "rate": "0.03", "quick_deduction": "0"},
            {"upper_bound": None, "rate": "0.10", "quick_deduction": "2520"},
        ],
        derived_income_rules=[],
    )


def _tax_result(
    batch_id: int,
    version: int,
    *,
    taxable: str,
    contribution: str,
    special: str,
    withheld: str,
    employment_months: int,
) -> SimpleNamespace:
    return SimpleNamespace(
        batch_id=batch_id,
        version=version,
        input_snapshot={
            "tax_withholding": {
                "current_taxable_income": taxable,
                "current_employee_contribution": contribution,
                "current_special_deduction": special,
                "current_tax_withheld": withheld,
                "employment_months_to_date": employment_months,
            }
        },
    )


def test_select_effective_policy_ignores_drafts_and_future_versions() -> None:
    january = _policy(1, date(2026, 1, 1))
    april_draft = _policy(2, date(2026, 4, 1), finalized=False)
    june = _policy(3, date(2026, 6, 1))

    chosen = service._select_effective_policy(
        [january, april_draft, june], city="广州", on_date=date(2026, 5, 1)
    )

    assert chosen is january
    assert (
        service._select_effective_policy(
            [january, april_draft, june], city="深圳", on_date=date(2026, 5, 1)
        )
        is None
    )


def test_locked_tax_ytd_uses_latest_result_per_batch_and_structured_snapshot() -> None:
    corrected_may = _tax_result(
        10,
        2,
        taxable="10000",
        contribution="600",
        special="200",
        withheld="150",
        employment_months=1,
    )
    ytd, errors = service._tax_ytd_from_locked_results(
        [
            _tax_result(
                10,
                1,
                taxable="9000",
                contribution="500",
                special="100",
                withheld="100",
                employment_months=1,
            ),
            corrected_may,
            _tax_result(
                11,
                1,
                taxable="11000",
                contribution="600",
                special="200",
                withheld="160",
                employment_months=2,
            ),
        ]
    )

    assert errors == ()
    assert ytd.taxable_income_before == Decimal("21000")
    assert ytd.employee_contribution_before == Decimal("1200")
    assert ytd.special_deduction_before == Decimal("400")
    assert ytd.tax_withheld_before == Decimal("310")
    assert ytd.employment_months_before == 2


def test_locked_tax_ytd_blocks_unstructured_legacy_history() -> None:
    ytd, errors = service._tax_ytd_from_locked_results(
        [SimpleNamespace(batch_id=10, version=1, input_snapshot={})]
    )

    assert ytd.taxable_income_before == Decimal("0")
    assert errors == ("Locked tax history has no structured withholding snapshot.",)


def test_employment_months_start_with_the_hire_month_not_january() -> None:
    employee = SimpleNamespace(hire_date=date(2026, 5, 20))

    assert service._tax_employment_months_to_date(employee, date(2026, 5, 1)) == 1
    assert service._tax_employment_months_to_date(employee, date(2026, 7, 1)) == 3


def test_tax_history_requires_every_prior_employment_month_or_an_opening_balance() -> None:
    assert service._tax_history_coverage_error(TaxYearToDate(), employment_months=1) is None
    assert (
        service._tax_history_coverage_error(
            TaxYearToDate(employment_months_before=4), employment_months=5
        )
        is None
    )
    assert service._tax_history_coverage_error(
        TaxYearToDate(employment_months_before=1), employment_months=3
    ) == (
        "Locked tax history does not cover every prior employment month; "
        "an audited opening balance or correction is required."
    )


def test_audited_opening_cannot_claim_more_employment_months_than_its_period() -> None:
    employee = SimpleNamespace(hire_date=date(2026, 1, 1))
    opening = SimpleNamespace(
        tax_year=2026,
        through_period="2026-01",
        employment_months_to_date=6,
    )

    assert service._tax_opening_coverage_error(opening, employee) == (
        "Audited tax opening employment-month count does not match its through period."
    )


def test_audited_opening_cannot_predate_the_employee_hire_month() -> None:
    employee = SimpleNamespace(hire_date=date(2026, 8, 1))
    opening = SimpleNamespace(
        tax_year=2026,
        through_period="2026-07",
        employment_months_to_date=0,
    )

    assert service._tax_opening_coverage_error(opening, employee) == (
        "Audited tax opening cannot predate the employee hire month."
    )
