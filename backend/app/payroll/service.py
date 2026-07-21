"""把 DB 中的薪资结构/考勤/绩效装配成引擎 v2 输入并预览核算（不落库；落库在 S13c）。"""

from __future__ import annotations

import calendar
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.comp.service import current_structure
from app.models.attendance import AttendanceRecord, PerformanceRecord
from app.models.comp import EmployeeSalaryStructure, SalaryComponentDef
from app.models.employee import Employee, EmploymentType, requires_approved_attendance_days
from app.models.holiday import HolidayCalendarPeriod, HolidayWorkRecord, StatutoryHolidayDate
from app.models.payroll_adjustment import MonthlyPayrollAdjustment, PayrollAdjustmentType
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_policy import EmployeeTaxDeduction, EmployeeTaxYtdOpening, PayrollPolicy
from app.models.payroll_result import PayrollResult as PersistedPayrollResult
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    PayrollResult,
    RuleConfig,
    StatutoryHoliday,
    StructureComponent,
    TaxOpeningProvenance,
    TaxYearToDate,
    compute,
)
from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    DerivedIncomeRule,
    PayrollPolicyContext,
    PolicyValidationError,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
    validate_derived_income_rules,
    validate_social_insurance_policy,
    validate_tax_policy,
)

_EMPTY_CARRY_INPUT = (Decimal("0"), Decimal("0"), Decimal("0"))


@dataclass(frozen=True)
class _MonthlyAdjustmentInputs:
    prev_makeup: Decimal = Decimal("0")
    prev_deduct: Decimal = Decimal("0")
    prev_makeup_taxable: bool | None = None
    prev_makeup_in_social_base: bool | None = None
    prev_makeup_in_housing_base: bool | None = None
    prev_deduct_taxable: bool | None = None
    prev_deduct_in_social_base: bool | None = None
    prev_deduct_in_housing_base: bool | None = None


_EMPTY_MONTHLY_ADJUSTMENT_INPUT = _MonthlyAdjustmentInputs()


def _monthly_adjustment_inputs(
    session: Session,
    employee_ids: set[int],
    period: str,
) -> dict[int, _MonthlyAdjustmentInputs]:
    """Return auditable prior-period makeup/deduction totals by employee."""

    if not employee_ids:
        return {}
    inputs: dict[int, _MonthlyAdjustmentInputs] = {}
    rows = session.execute(
        select(
            MonthlyPayrollAdjustment.employee_id,
            MonthlyPayrollAdjustment.adjustment_type,
            MonthlyPayrollAdjustment.amount,
            MonthlyPayrollAdjustment.taxable,
            MonthlyPayrollAdjustment.in_social_base,
            MonthlyPayrollAdjustment.in_housing_base,
        ).where(
            MonthlyPayrollAdjustment.employee_id.in_(employee_ids),
            MonthlyPayrollAdjustment.period == period,
        )
    ).all()
    for employee_id, adjustment_type, amount_value, taxable, in_social, in_housing in rows:
        current = inputs.get(employee_id, _EMPTY_MONTHLY_ADJUSTMENT_INPUT)
        amount = Decimal(amount_value)
        if adjustment_type == PayrollAdjustmentType.PREV_MAKEUP:
            inputs[employee_id] = _MonthlyAdjustmentInputs(
                prev_makeup=amount,
                prev_deduct=current.prev_deduct,
                prev_makeup_taxable=taxable,
                prev_makeup_in_social_base=in_social,
                prev_makeup_in_housing_base=in_housing,
                prev_deduct_taxable=current.prev_deduct_taxable,
                prev_deduct_in_social_base=current.prev_deduct_in_social_base,
                prev_deduct_in_housing_base=current.prev_deduct_in_housing_base,
            )
        else:
            inputs[employee_id] = _MonthlyAdjustmentInputs(
                prev_makeup=current.prev_makeup,
                prev_deduct=amount,
                prev_makeup_taxable=current.prev_makeup_taxable,
                prev_makeup_in_social_base=current.prev_makeup_in_social_base,
                prev_makeup_in_housing_base=current.prev_makeup_in_housing_base,
                prev_deduct_taxable=taxable,
                prev_deduct_in_social_base=in_social,
                prev_deduct_in_housing_base=in_housing,
            )
    return inputs


@dataclass(frozen=True)
class _PolicyPayrollInputs:
    """All S12 inputs selected before an employee reaches the pure engine."""

    payroll_policy: PayrollPolicyContext | None
    monthly_special_deduction: Decimal
    tax_ytd: TaxYearToDate
    tax_employment_months: int | None
    tax_opening: TaxOpeningProvenance | None
    source_exceptions: tuple[str, ...]


def _select_effective_policy(
    policies: Iterable[PayrollPolicy], *, city: str, on_date: date
) -> PayrollPolicy | None:
    """Choose the newest finalized city policy already effective on ``on_date``."""

    candidates = [
        policy
        for policy in policies
        if policy.city == city and policy.is_finalized and policy.effective_from <= on_date
    ]
    return max(candidates, key=lambda policy: policy.effective_from, default=None)


def _policy_context_from_record(
    policy: PayrollPolicy,
) -> tuple[PayrollPolicyContext | None, str | None]:
    """Turn persisted, JSON policy data into a validated immutable engine input."""

    try:
        derived_income_rules = getattr(policy, "derived_income_rules", [])
        if (
            not isinstance(policy.social_rules, list)
            or not isinstance(policy.tax_brackets, list)
            or not isinstance(derived_income_rules, list)
        ):
            raise PolicyValidationError("policy rules, brackets and derived income must be arrays")
        social = SocialInsurancePolicyInput(
            city=policy.city,
            rules=tuple(
                ContributionRule(
                    kind=ContributionKind(str(rule["kind"])),
                    employee_rate=Decimal(str(rule["employee_rate"])),
                    employer_rate=Decimal(str(rule["employer_rate"])),
                    base_min=Decimal(str(rule["base_min"])),
                    base_max=(
                        Decimal(str(rule["base_max"])) if rule.get("base_max") is not None else None
                    ),
                )
                for rule in policy.social_rules
                if isinstance(rule, Mapping)
            ),
        )
        tax = TaxPolicyInput(
            monthly_basic_deduction=Decimal(str(policy.monthly_basic_deduction)),
            brackets=tuple(
                TaxBracket(
                    upper_bound=(
                        Decimal(str(bracket["upper_bound"]))
                        if bracket.get("upper_bound") is not None
                        else None
                    ),
                    rate=Decimal(str(bracket["rate"])),
                    quick_deduction=Decimal(str(bracket["quick_deduction"])),
                )
                for bracket in policy.tax_brackets
                if isinstance(bracket, Mapping)
            ),
        )
        derived = tuple(
            DerivedIncomeRule(
                code=str(rule["code"]),
                taxable=rule["taxable"],
                in_social_base=rule["in_social_base"],
                in_housing_base=rule["in_housing_base"],
            )
            for rule in derived_income_rules
            if isinstance(rule, Mapping)
        )
        if (
            len(social.rules) != len(policy.social_rules)
            or len(tax.brackets) != len(policy.tax_brackets)
            or len(derived) != len(derived_income_rules)
        ):
            raise PolicyValidationError("policy has a non-object rule, bracket or derived income")
        validate_social_insurance_policy(social)
        validate_tax_policy(tax)
        validate_derived_income_rules(derived)
    except (
        InvalidOperation,
        KeyError,
        PolicyValidationError,
        TypeError,
        ValueError,
    ) as exc:
        return None, f"Payroll policy {policy.id} is invalid: {exc}"
    return (
        PayrollPolicyContext(
            policy_id=policy.id,
            city=policy.city,
            effective_from=policy.effective_from,
            social_policy=social,
            tax_policy=tax,
            derived_income_rules=derived,
        ),
        None,
    )


def _tax_ytd_from_locked_rows(
    rows: Iterable[tuple[str | None, PersistedPayrollResult]],
) -> tuple[TaxYearToDate, tuple[str, ...], frozenset[str]]:
    """Aggregate immutable, structured tax facts from the active result per batch.

    The query caller already limits rows to earlier, current-year, LOCKED batch
    versions.  This helper nevertheless chooses the highest employee result
    version per batch so a reopened/recomputed round cannot be double-counted,
    and keeps the covered calendar periods for the fail-closed gap check.
    """

    latest_by_batch: dict[int, tuple[str | None, PersistedPayrollResult]] = {}
    for period, result in rows:
        prior = latest_by_batch.get(result.batch_id)
        if prior is None or result.version > prior[1].version:
            latest_by_batch[result.batch_id] = (period, result)

    taxable = Decimal("0")
    contribution = Decimal("0")
    special = Decimal("0")
    withheld = Decimal("0")
    employment_months = 0
    errors: list[str] = []
    covered_periods: set[str] = set()
    fields = (
        "current_taxable_income",
        "current_employee_contribution",
        "current_special_deduction",
        "current_tax_withheld",
    )
    for batch_id in sorted(latest_by_batch):
        period, result = latest_by_batch[batch_id]
        snapshot = result.input_snapshot
        state = snapshot.get("tax_withholding") if isinstance(snapshot, Mapping) else None
        if not isinstance(state, Mapping):
            errors.append("Locked tax history has no structured withholding snapshot.")
            continue
        try:
            values = {field: Decimal(str(state[field])) for field in fields}
            if any(not value.is_finite() or value < 0 for value in values.values()):
                raise ValueError("tax state amounts must be finite and non-negative")
            state_months = state["employment_months_to_date"]
            if (
                isinstance(state_months, bool)
                or not isinstance(state_months, int)
                or not 1 <= state_months <= 12
            ):
                raise ValueError("employment-month count is invalid")
        except (InvalidOperation, KeyError, TypeError, ValueError):
            errors.append("Locked tax history has an invalid withholding snapshot.")
            continue
        if period is not None:
            try:
                parsed_period = _period_start(period)
                if period != f"{parsed_period.year:04d}-{parsed_period.month:02d}":
                    raise ValueError("non-canonical payroll period")
            except (AttributeError, TypeError, ValueError):
                errors.append("Locked tax history has an invalid payroll period.")
                continue
        taxable += values["current_taxable_income"]
        contribution += values["current_employee_contribution"]
        special += values["current_special_deduction"]
        withheld += values["current_tax_withheld"]
        employment_months = max(employment_months, state_months)
        if period is not None:
            covered_periods.add(period)
    return (
        TaxYearToDate(
            taxable_income_before=taxable,
            employee_contribution_before=contribution,
            special_deduction_before=special,
            tax_withheld_before=withheld,
            employment_months_before=employment_months,
        ),
        tuple(dict.fromkeys(errors)),
        frozenset(covered_periods),
    )


def _tax_ytd_from_locked_results(
    results: Iterable[PersistedPayrollResult],
) -> tuple[TaxYearToDate, tuple[str, ...]]:
    """Compatibility wrapper for aggregate-only unit callers."""

    tax_ytd, errors, _covered_periods = _tax_ytd_from_locked_rows(
        (None, result) for result in results
    )
    return tax_ytd, errors


def _tax_ytd_from_opening(
    opening: EmployeeTaxYtdOpening | None,
) -> tuple[TaxYearToDate, tuple[str, ...]]:
    """Return finalized, auditable carried-in facts without reading live payroll."""

    if opening is None:
        return TaxYearToDate(), ()
    try:
        year, month = (int(part) for part in opening.through_period.split("-"))
        if year != opening.tax_year or not 1 <= month <= 12:
            raise ValueError("through period does not belong to the tax year")
        amounts = (
            Decimal(opening.taxable_income),
            Decimal(opening.employee_contribution),
            Decimal(opening.special_deduction),
            Decimal(opening.tax_withheld),
        )
        if any(not amount.is_finite() or amount < 0 for amount in amounts):
            raise ValueError("opening amounts must be finite and non-negative")
        months_to_date = opening.employment_months_to_date
        if (
            isinstance(months_to_date, bool)
            or not isinstance(months_to_date, int)
            or not 0 <= months_to_date <= 12
        ):
            raise ValueError("opening employment-month count is invalid")
        if not opening.evidence_ref.strip():
            raise ValueError("opening evidence reference is required")
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        return TaxYearToDate(), ("Audited tax opening balance is invalid.",)
    return (
        TaxYearToDate(
            taxable_income_before=amounts[0],
            employee_contribution_before=amounts[1],
            special_deduction_before=amounts[2],
            tax_withheld_before=amounts[3],
            employment_months_before=months_to_date,
        ),
        (),
    )


def _tax_opening_coverage_error(opening: EmployeeTaxYtdOpening, employee: Employee) -> str | None:
    """Ensure an opening's claimed employment months fit its named period.

    A finalized opening is allowed to replace unavailable earlier snapshots,
    but its month count must remain independently derivable from the employee
    record.  Otherwise a bad opening could silently hide a missing period of
    cumulative-tax history.
    """

    try:
        year, month = (int(part) for part in opening.through_period.split("-"))
        if year != opening.tax_year or not 1 <= month <= 12:
            raise ValueError("through period does not belong to the tax year")
        hire_date = employee.hire_date
        if hire_date is None:
            raise ValueError("employee hire date is required")
        period_end = _period_end(opening.through_period)
        if hire_date >= period_end:
            return "Audited tax opening cannot predate the employee hire month."
        employment_start = max(hire_date, date(year, 1, 1))
        expected_months = (year - employment_start.year) * 12 + month - employment_start.month + 1
        actual_months = opening.employment_months_to_date
        if isinstance(actual_months, bool) or not isinstance(actual_months, int):
            raise ValueError("opening employment-month count is invalid")
    except (AttributeError, TypeError, ValueError):
        return "Audited tax opening employment-month count is invalid."
    if actual_months != expected_months:
        return "Audited tax opening employment-month count does not match its through period."
    return None


def _tax_opening_provenance(
    opening: EmployeeTaxYtdOpening | None,
) -> tuple[TaxOpeningProvenance | None, str | None]:
    """Capture the approved opening identity in the payroll input snapshot."""

    if opening is None:
        return None, None
    try:
        if not opening.is_finalized:
            raise ValueError("opening is not finalized")
        provenance = TaxOpeningProvenance(
            opening_id=int(opening.id),
            revision=int(opening.revision),
            tax_year=int(opening.tax_year),
            through_period=str(opening.through_period),
            evidence_ref=str(opening.evidence_ref),
            finalized_by=(int(opening.finalized_by) if opening.finalized_by is not None else None),
            finalized_at=opening.finalized_at,
        )
        if (
            provenance.opening_id < 1
            or provenance.revision < 1
            or not provenance.evidence_ref.strip()
            or provenance.finalized_at is not None
            and provenance.finalized_at.tzinfo is None
        ):
            raise ValueError("opening provenance is incomplete")
    except (AttributeError, TypeError, ValueError):
        return None, "Audited tax opening provenance is invalid."
    return provenance, None


def _combine_tax_ytd(*states: TaxYearToDate) -> TaxYearToDate:
    """Merge opening facts and later locked monthly facts without parsing lines."""

    return TaxYearToDate(
        taxable_income_before=sum((state.taxable_income_before for state in states), Decimal("0")),
        employee_contribution_before=sum(
            (state.employee_contribution_before for state in states), Decimal("0")
        ),
        special_deduction_before=sum(
            (state.special_deduction_before for state in states), Decimal("0")
        ),
        tax_withheld_before=sum((state.tax_withheld_before for state in states), Decimal("0")),
        employment_months_before=max(
            (state.employment_months_before for state in states), default=0
        ),
    )


def _tax_employment_months_to_date(employee: Employee, on_date: date) -> int:
    """Count the employee's current-year employment months through this period.

    A single hire date cannot describe a previous leave-and-rehire gap.  The
    caller compares this derived count with locked YTD state and fail-closes on
    an inconsistency instead of guessing a statutory deduction.
    """

    hire_date = employee.hire_date
    period_end = _period_end(f"{on_date.year:04d}-{on_date.month:02d}")
    if hire_date is None:
        raise ValueError("employee hire date is required for cumulative withholding")
    if hire_date >= period_end:
        raise ValueError("employee is not employed during this payroll period")
    employment_start = max(hire_date, date(on_date.year, 1, 1))
    return (on_date.year - employment_start.year) * 12 + on_date.month - employment_start.month + 1


def _tax_history_coverage_error(tax_ytd: TaxYearToDate, *, employment_months: int) -> str | None:
    """Reject a guessed zero YTD when earlier employment months have no facts."""

    if tax_ytd.employment_months_before == employment_months - 1:
        return None
    return (
        "Locked tax history does not cover every prior employment month; "
        "an audited opening balance or correction is required."
    )


def _tax_history_period_coverage_error(
    *,
    employee: Employee,
    on_date: date,
    opening: EmployeeTaxYtdOpening | None,
    locked_periods: frozenset[str] | set[str],
) -> str | None:
    """Require a structured result for every uncovered prior employment month."""

    try:
        hire_date = employee.hire_date
        if hire_date is None:
            raise ValueError("employee hire date is required")
        first_employment_month = max(hire_date, date(on_date.year, 1, 1)).replace(day=1)
        current_month = on_date.replace(day=1)
        if first_employment_month >= current_month:
            return None
        opening_period = opening.through_period if opening is not None else None
        if opening_period is not None:
            parsed_opening = _period_start(opening_period)
            if opening_period != f"{parsed_opening.year:04d}-{parsed_opening.month:02d}":
                raise ValueError("non-canonical opening period")
        period = first_employment_month
        while period < current_month:
            value = f"{period.year:04d}-{period.month:02d}"
            if (opening_period is None or value > opening_period) and value not in locked_periods:
                return f"Locked tax history is missing employment period {value}."
            period = _period_end(value)
    except (AttributeError, TypeError, ValueError):
        return "Locked tax history cannot be matched to employee employment periods."
    return None


def _effective_special_deductions(
    rows: Iterable[EmployeeTaxDeduction],
) -> dict[int, Decimal]:
    """Pick the newest effective employee special deduction from ordered rows."""

    deductions: dict[int, Decimal] = {}
    for row in rows:
        deductions.setdefault(row.employee_id, Decimal(row.monthly_special_deduction))
    return deductions


def _policy_inputs_by_employee(
    session: Session, employees: list[Employee], period: str
) -> dict[int, _PolicyPayrollInputs]:
    """Bulk-load all effective S12 inputs and convert missing data into blocks."""

    if not employees:
        return {}
    on_date = _period_start(period)
    employee_ids = {employee.id for employee in employees}
    cities = {employee.social_city.strip() for employee in employees if employee.social_city}
    policies = list(
        session.scalars(
            select(PayrollPolicy).where(
                PayrollPolicy.city.in_(cities or {""}),
                PayrollPolicy.is_finalized.is_(True),
                PayrollPolicy.effective_from <= on_date,
            )
        ).all()
    )
    contexts: dict[str, tuple[PayrollPolicyContext | None, str | None]] = {}
    for city in cities:
        policy = _select_effective_policy(policies, city=city, on_date=on_date)
        contexts[city] = (
            _policy_context_from_record(policy)
            if policy is not None
            else (None, f"No finalized payroll policy applies to social city {city}.")
        )

    deductions = _effective_special_deductions(
        session.scalars(
            select(EmployeeTaxDeduction)
            .where(
                EmployeeTaxDeduction.employee_id.in_(employee_ids),
                EmployeeTaxDeduction.effective_from <= on_date,
            )
            .order_by(
                EmployeeTaxDeduction.employee_id,
                EmployeeTaxDeduction.effective_from.desc(),
            )
        ).all()
    )
    openings_by_employee = {
        opening.employee_id: opening
        for opening in session.scalars(
            select(EmployeeTaxYtdOpening).where(
                EmployeeTaxYtdOpening.employee_id.in_(employee_ids),
                EmployeeTaxYtdOpening.tax_year == on_date.year,
                EmployeeTaxYtdOpening.through_period < period,
                EmployeeTaxYtdOpening.is_finalized.is_(True),
                EmployeeTaxYtdOpening.superseded_at.is_(None),
            )
        ).all()
    }
    locked_results_by_employee: dict[int, list[tuple[str, PersistedPayrollResult]]] = {
        employee_id: [] for employee_id in employee_ids
    }
    year_start = f"{on_date.year:04d}-01"
    for result, result_period in session.execute(
        select(PersistedPayrollResult, PayrollBatch.period)
        .join(PayrollBatch, PayrollBatch.id == PersistedPayrollResult.batch_id)
        .where(
            PersistedPayrollResult.employee_id.in_(employee_ids),
            PayrollBatch.period >= year_start,
            PayrollBatch.period < period,
            PayrollBatch.status == BatchStatus.LOCKED,
            PersistedPayrollResult.batch_version == PayrollBatch.version,
        )
        .order_by(
            PersistedPayrollResult.employee_id,
            PayrollBatch.period,
            PersistedPayrollResult.version.desc(),
        )
    ).all():
        locked_results_by_employee[result.employee_id].append((result_period, result))

    out: dict[int, _PolicyPayrollInputs] = {}
    for employee in employees:
        errors: list[str] = []
        city = employee.social_city.strip() if employee.social_city else ""
        context: PayrollPolicyContext | None = None
        if not city:
            errors.append("Employee has no social-insurance city.")
        else:
            context, context_error = contexts[city]
            if context_error is not None:
                errors.append(context_error)
        opening = openings_by_employee.get(employee.id)
        opening_ytd, opening_errors = _tax_ytd_from_opening(opening)
        errors.extend(opening_errors)
        tax_opening, opening_provenance_error = _tax_opening_provenance(opening)
        if opening_provenance_error is not None:
            errors.append(opening_provenance_error)
        if opening is not None:
            opening_coverage_error = _tax_opening_coverage_error(opening, employee)
            if opening_coverage_error is not None:
                errors.append(opening_coverage_error)
        locked_result_rows = locked_results_by_employee[employee.id]
        if opening is not None:
            overlapping_periods = [
                result_period
                for result_period, _result in locked_result_rows
                if result_period <= opening.through_period
            ]
            if overlapping_periods:
                errors.append("Audited tax opening overlaps locked payroll tax history.")
            locked_result_rows = [
                (result_period, result)
                for result_period, result in locked_result_rows
                if result_period > opening.through_period
            ]
        locked_ytd, history_errors, locked_periods = _tax_ytd_from_locked_rows(locked_result_rows)
        errors.extend(history_errors)
        tax_ytd = _combine_tax_ytd(opening_ytd, locked_ytd)
        try:
            employment_months = _tax_employment_months_to_date(employee, on_date)
            history_coverage_error = _tax_history_coverage_error(
                tax_ytd, employment_months=employment_months
            )
            if history_coverage_error is not None:
                errors.append(history_coverage_error)
            period_coverage_error = _tax_history_period_coverage_error(
                employee=employee,
                on_date=on_date,
                opening=opening,
                locked_periods=locked_periods,
            )
            if period_coverage_error is not None:
                errors.append(period_coverage_error)
        except ValueError as exc:
            employment_months = None
            errors.append(f"Payroll tax employment history is invalid: {exc}.")
        out[employee.id] = _PolicyPayrollInputs(
            payroll_policy=context,
            monthly_special_deduction=deductions.get(employee.id, Decimal("0")),
            tax_ytd=tax_ytd,
            tax_employment_months=employment_months,
            tax_opening=tax_opening,
            source_exceptions=tuple(dict.fromkeys(errors)),
        )
    return out


def _period_start(period: str) -> date:
    year, month = period.split("-")
    return date(int(year), int(month), 1)


def _days_in_month(period: str) -> Decimal:
    year, month = (int(x) for x in period.split("-"))
    return Decimal(calendar.monthrange(year, month)[1])


def _period_end(period: str) -> date:
    year, month = (int(x) for x in period.split("-"))
    if month == 12:
        return date(year + 1, 1, 1)
    return date(year, month + 1, 1)


def _same_period(d: date | None, period: str) -> bool:
    return d is not None and f"{d.year:04d}-{d.month:02d}" == period


def _structure_selection_date(employee: Employee, period: str) -> date:
    """Select new-hire terms on the hire date; later changes remain next-period.

    Normal monthly structures are read at the first calendar day.  A newly
    hired employee cannot have terms before their employment begins, so their
    first-period structure is instead selected on the actual hire date.  This
    keeps mid-month salary revisions for existing employees on the established
    next-month boundary while allowing first-month payroll to be calculated.
    """
    period_start = _period_start(period)
    if _same_period(employee.hire_date, period):
        return employee.hire_date or period_start
    return period_start


def _employee_lifecycle_errors(employee: Employee) -> tuple[str, ...]:
    if employee.hire_date is None:
        return ("Employee hire date is required for payroll eligibility.",)
    errors: list[str] = []
    if employee.probation_end is not None and employee.probation_end < employee.hire_date:
        errors.append("Employee probation end cannot precede the hire date.")
    if employee.leave_date is not None and employee.leave_date < employee.hire_date:
        errors.append("Employee leave date cannot precede the hire date.")
    return tuple(errors)


def _carry_input_from_result(result: PersistedPayrollResult) -> tuple[Decimal, Decimal, Decimal]:
    """Return the unpaid wage, deductions, and deposit from a locked result.

    Older result rows predate the two deferred-obligation columns.  Their
    immutable snapshot/line detail still lets us recover the one supported
    legacy case (a new hire's unpaid first-month payroll) without treating a
    malformed value as zero.
    """
    carry_forward = Decimal(result.carry_forward)
    deferred_deductions = Decimal(getattr(result, "deferred_deductions", 0))
    deferred_deposit = Decimal(getattr(result, "deferred_deposit", 0))
    if carry_forward <= 0 and deferred_deductions <= 0 and deferred_deposit <= 0:
        return _EMPTY_CARRY_INPUT

    if deferred_deductions == 0:
        for line in result.lines:
            if not isinstance(line, dict) or line.get("code") != "DEDUCTION":
                continue
            deferred_deductions = abs(Decimal(str(line["amount"])))
            break
    if deferred_deposit == 0:
        snapshot = result.input_snapshot
        if (
            isinstance(snapshot, dict)
            and bool(snapshot.get("is_new_employee"))
            and Decimal(result.deposit) == 0
        ):
            deferred_deposit = RuleConfig().deposit_amount
    return carry_forward, deferred_deductions, deferred_deposit


def _latest_locked_carry_input(
    session: Session, employee_id: int, period: str
) -> tuple[Decimal, Decimal, Decimal]:
    """Read only the latest final prior period, never a mutable draft result."""
    result = session.scalars(
        select(PersistedPayrollResult)
        .join(PayrollBatch, PayrollBatch.id == PersistedPayrollResult.batch_id)
        .where(
            PersistedPayrollResult.employee_id == employee_id,
            PayrollBatch.period < period,
            PayrollBatch.status == BatchStatus.LOCKED,
            PersistedPayrollResult.batch_version == PayrollBatch.version,
        )
        .order_by(PayrollBatch.period.desc(), PersistedPayrollResult.version.desc())
        .limit(1)
    ).first()
    return _carry_input_from_result(result) if result is not None else _EMPTY_CARRY_INPUT


def _locked_carry_inputs(
    session: Session, employee_ids: set[int], period: str
) -> dict[int, tuple[Decimal, Decimal, Decimal]]:
    """Bulk counterpart of :func:`_latest_locked_carry_input` for batch runs."""
    if not employee_ids:
        return {}
    rows = session.scalars(
        select(PersistedPayrollResult)
        .join(PayrollBatch, PayrollBatch.id == PersistedPayrollResult.batch_id)
        .where(
            PersistedPayrollResult.employee_id.in_(employee_ids),
            PayrollBatch.period < period,
            PayrollBatch.status == BatchStatus.LOCKED,
            PersistedPayrollResult.batch_version == PayrollBatch.version,
        )
        .order_by(
            PersistedPayrollResult.employee_id,
            PayrollBatch.period.desc(),
            PersistedPayrollResult.version.desc(),
        )
    ).all()
    carry_inputs: dict[int, tuple[Decimal, Decimal, Decimal]] = {}
    for result in rows:
        if result.employee_id not in carry_inputs:
            carry_inputs[result.employee_id] = _carry_input_from_result(result)
    return carry_inputs


def _load_holiday_calendar(
    session: Session, period: str
) -> tuple[bool, list[StatutoryHolidayDate], tuple[str, ...]]:
    """Load a HR-finalized statutory calendar or a deterministic block reason."""
    calendar_period = session.scalars(
        select(HolidayCalendarPeriod).where(HolidayCalendarPeriod.period == period)
    ).first()
    if calendar_period is None or not calendar_period.is_finalized:
        return False, [], ()
    start = _period_start(period)
    end = _period_end(period)
    holidays = list(
        session.scalars(
            select(StatutoryHolidayDate)
            .where(
                StatutoryHolidayDate.holiday_date >= start,
                StatutoryHolidayDate.holiday_date < end,
            )
            .order_by(StatutoryHolidayDate.holiday_date)
        ).all()
    )
    return True, holidays, ()


def _eligible_holiday_inputs(
    employee: Employee,
    holidays: list[StatutoryHolidayDate],
    worked_by_date: dict[date, bool],
) -> tuple[tuple[StatutoryHoliday, ...], tuple[str, ...]]:
    """Validate policy JSON and select the dates applicable to one employee."""
    valid_types = {employment_type.value for employment_type in EmploymentType}
    result: list[StatutoryHoliday] = []
    for holiday in holidays:
        eligible_types = holiday.eligible_employment_types
        if (
            not isinstance(eligible_types, list)
            or not eligible_types
            or any(
                not isinstance(value, str) or value not in valid_types for value in eligible_types
            )
            or len(set(eligible_types)) != len(eligible_types)
        ):
            return (), (f"法定节假日 {holiday.holiday_date} 的适用用工类型配置无效",)
        if employee.employment_type.value not in eligible_types:
            continue
        if employee.hire_date is not None and holiday.holiday_date < employee.hire_date:
            continue
        if employee.leave_date is not None and holiday.holiday_date > employee.leave_date:
            continue
        result.append(
            StatutoryHoliday(
                day=holiday.holiday_date,
                worked=worked_by_date.get(holiday.holiday_date, False),
            )
        )
    return tuple(result), ()


def _holiday_work_map(
    records: list[HolidayWorkRecord], holidays: list[StatutoryHolidayDate]
) -> tuple[dict[date, bool], tuple[str, ...]]:
    configured_dates = {holiday.holiday_date for holiday in holidays}
    out_of_calendar = sorted(
        {record.holiday_date for record in records if record.holiday_date not in configured_dates}
    )
    if out_of_calendar:
        dates = ", ".join(value.isoformat() for value in out_of_calendar)
        return {}, (f"法定节假日出勤记录包含非日历日期：{dates}",)
    return {record.holiday_date: record.worked for record in records}, ()


def _attendance_input(
    session: Session,
    employee: Employee,
    period: str,
    record: AttendanceRecord | None,
) -> tuple[Attendance | None, Decimal | None, int | None, tuple[str, ...]]:
    """Use the auditable schedule baseline stored with the attendance record.

    A batch must never recalculate expected days from a rule that was added or
    edited after attendance was entered.  Legacy rows without the two
    provenance fields are deliberately blocked until HR runs the explicit,
    audited schedule-generation action.
    """
    if record is None:
        return None, None, None, ()
    expected_days = record.expected_days
    generated_days = record.generated_expected_days
    rule_id = record.expected_days_rule_id
    errors: tuple[str, ...] = ()
    if generated_days is None or rule_id is None:
        errors = (
            "Attendance expected days lack schedule provenance; "
            "HR must generate the schedule before payroll.",
        )
    return (
        Attendance(
            expected_days=expected_days,
            actual_days=record.actual_days,
            worked_hours=record.worked_hours,
            rest_days=record.rest_days,
            overtime_hours=record.overtime_hours,
            holiday_worked_days=record.holiday_worked_days,
        ),
        generated_days,
        rule_id,
        errors,
    )


def build_input(
    session: Session, employee: Employee, period: str
) -> tuple[EmployeeInput, list[int]]:
    """装配引擎输入；返回 (输入, 无法解析的组件 id 列表)。"""
    on_date = _structure_selection_date(employee, period)
    ess = current_structure(session, employee.id, on_date)
    comp_meta = {
        cid: (
            code,
            ctype,
            akind,
            taxable,
            in_social_base,
            in_housing_base,
            prorate_by_attendance,
        )
        for (
            cid,
            code,
            ctype,
            akind,
            taxable,
            in_social_base,
            in_housing_base,
            prorate_by_attendance,
        ) in session.execute(
            select(
                SalaryComponentDef.id,
                SalaryComponentDef.code,
                SalaryComponentDef.component_type,
                SalaryComponentDef.allowance_kind,
                SalaryComponentDef.taxable,
                SalaryComponentDef.in_social_base,
                SalaryComponentDef.in_housing_base,
                SalaryComponentDef.prorate_by_attendance,
            ).where(SalaryComponentDef.id.in_({r.component_id for r in ess} or {0}))
        ).all()
    }
    missing = sorted({r.component_id for r in ess if r.component_id not in comp_meta})
    structure = [
        StructureComponent(
            code=comp_meta[r.component_id][0],
            component_type=comp_meta[r.component_id][1],
            amount=r.amount,
            allowance_kind=comp_meta[r.component_id][2],
            taxable=comp_meta[r.component_id][3],
            in_social_base=comp_meta[r.component_id][4],
            in_housing_base=comp_meta[r.component_id][5],
            prorate_by_attendance=comp_meta[r.component_id][6],
        )
        for r in ess
        if r.component_id in comp_meta
    ]

    att = session.scalars(
        select(AttendanceRecord).where(
            AttendanceRecord.employee_id == employee.id, AttendanceRecord.period == period
        )
    ).first()
    performance = session.scalars(
        select(PerformanceRecord).where(
            PerformanceRecord.employee_id == employee.id,
            PerformanceRecord.period == period,
        )
    ).first()
    attendance, generated_expected_days, expected_days_rule_id, schedule_errors = _attendance_input(
        session, employee, period, att
    )

    calendar_finalized, calendar_holidays, calendar_errors = _load_holiday_calendar(session, period)
    holiday_inputs: tuple[StatutoryHoliday, ...] = ()
    holiday_errors = calendar_errors
    if calendar_finalized:
        work_records = list(
            session.scalars(
                select(HolidayWorkRecord).where(
                    HolidayWorkRecord.employee_id == employee.id,
                    HolidayWorkRecord.holiday_date >= _period_start(period),
                    HolidayWorkRecord.holiday_date < _period_end(period),
                )
            ).all()
        )
        worked_by_date, work_errors = _holiday_work_map(work_records, calendar_holidays)
        if not work_errors:
            holiday_inputs, holiday_errors = _eligible_holiday_inputs(
                employee, calendar_holidays, worked_by_date
            )
        else:
            holiday_errors = work_errors

    is_new = _same_period(employee.hire_date, period)
    is_hire_or_leave = is_new or _same_period(employee.leave_date, period)
    carry_forward, deferred_deductions, deferred_deposit = _latest_locked_carry_input(
        session, employee.id, period
    )
    monthly_adjustments = _monthly_adjustment_inputs(session, {employee.id}, period).get(
        employee.id, _EMPTY_MONTHLY_ADJUSTMENT_INPUT
    )
    policy_inputs = _policy_inputs_by_employee(session, [employee], period)[employee.id]
    # v2 简化：入职晚于周期首日则视为不享法定节假日（缺法定日历，属已知简化）
    lifecycle_errors = _employee_lifecycle_errors(employee)
    if lifecycle_errors:
        holiday_inputs = ()
    holiday_eligible = (
        not lifecycle_errors and employee.hire_date is not None and employee.hire_date <= on_date
    )
    effective_special_position = employee.is_special_position or requires_approved_attendance_days(
        employee.position_title
    )

    inp = EmployeeInput(
        employee_id=employee.id,
        period=period,
        days_in_month=_days_in_month(period),
        employment_type=employee.employment_type,
        department=employee.department,
        is_special_position=effective_special_position,
        structure=structure,
        attendance=attendance,
        generated_expected_days=generated_expected_days,
        expected_days_rule_id=expected_days_rule_id,
        performance_coefficient=(
            performance.coefficient if performance is not None else Decimal("1")
        ),
        # 日历必须由人事确认；服务层按日历、劳动关系和用工类型装配逐日输入。
        statutory_holiday_days=Decimal("0"),
        holiday_eligible=holiday_eligible,
        holiday_calendar_finalized=calendar_finalized,
        statutory_holidays=holiday_inputs,
        hire_date=employee.hire_date,
        probation_end=employee.probation_end,
        leave_date=employee.leave_date,
        is_new_employee=is_new,
        is_hire_or_leave_month=is_hire_or_leave,
        prev_makeup=monthly_adjustments.prev_makeup,
        prev_deduct=monthly_adjustments.prev_deduct,
        prev_makeup_taxable=monthly_adjustments.prev_makeup_taxable,
        prev_makeup_in_social_base=monthly_adjustments.prev_makeup_in_social_base,
        prev_makeup_in_housing_base=monthly_adjustments.prev_makeup_in_housing_base,
        prev_deduct_taxable=monthly_adjustments.prev_deduct_taxable,
        prev_deduct_in_social_base=monthly_adjustments.prev_deduct_in_social_base,
        prev_deduct_in_housing_base=monthly_adjustments.prev_deduct_in_housing_base,
        prior_carry_forward=carry_forward,
        prior_deferred_deductions=deferred_deductions,
        prior_deferred_deposit=deferred_deposit,
        payroll_policy=policy_inputs.payroll_policy,
        monthly_special_deduction=policy_inputs.monthly_special_deduction,
        tax_ytd=policy_inputs.tax_ytd,
        tax_employment_months=policy_inputs.tax_employment_months,
        tax_opening=policy_inputs.tax_opening,
        source_exceptions=(
            *lifecycle_errors,
            *schedule_errors,
            *holiday_errors,
            *policy_inputs.source_exceptions,
        ),
    )
    return inp, missing


def build_inputs(
    session: Session, employees: list[Employee], period: str
) -> dict[int, tuple[EmployeeInput, list[int]]]:
    """Build a payroll cohort's inputs with bounded bulk database reads.

    ``run_batch`` holds the source-data advisory lock, so one query per person
    turns a large payroll into a long write outage.  This bulk variant loads the
    effective structures, their component metadata, and attendance once for the
    whole cohort while preserving ``build_input``'s individual calculation
    semantics.
    """
    if not employees:
        return {}
    employee_ids = {employee.id for employee in employees}
    on_date = _period_start(period)
    structure_dates = {
        employee.id: _structure_selection_date(employee, period) for employee in employees
    }
    earliest_structure_date = min(structure_dates.values())
    latest_structure_date = max(structure_dates.values())
    structures_by_employee: dict[int, list[EmployeeSalaryStructure]] = {
        employee_id: [] for employee_id in employee_ids
    }
    for structure in session.scalars(
        select(EmployeeSalaryStructure)
        .where(
            EmployeeSalaryStructure.employee_id.in_(employee_ids),
            EmployeeSalaryStructure.effective_from <= latest_structure_date,
            (EmployeeSalaryStructure.effective_to.is_(None))
            | (EmployeeSalaryStructure.effective_to > earliest_structure_date),
        )
        .order_by(
            EmployeeSalaryStructure.employee_id,
            EmployeeSalaryStructure.component_id,
            EmployeeSalaryStructure.effective_from,
        )
    ).all():
        selection_date = structure_dates[structure.employee_id]
        if structure.effective_from <= selection_date and (
            structure.effective_to is None or structure.effective_to > selection_date
        ):
            structures_by_employee[structure.employee_id].append(structure)

    component_ids = {
        structure.component_id
        for structures in structures_by_employee.values()
        for structure in structures
    }
    comp_meta = {
        component_id: (
            code,
            component_type,
            allowance_kind,
            taxable,
            in_social_base,
            in_housing_base,
            prorate_by_attendance,
        )
        for (
            component_id,
            code,
            component_type,
            allowance_kind,
            taxable,
            in_social_base,
            in_housing_base,
            prorate_by_attendance,
        ) in session.execute(
            select(
                SalaryComponentDef.id,
                SalaryComponentDef.code,
                SalaryComponentDef.component_type,
                SalaryComponentDef.allowance_kind,
                SalaryComponentDef.taxable,
                SalaryComponentDef.in_social_base,
                SalaryComponentDef.in_housing_base,
                SalaryComponentDef.prorate_by_attendance,
            ).where(SalaryComponentDef.id.in_(component_ids or {0}))
        ).all()
    }
    attendance_by_employee = {
        record.employee_id: record
        for record in session.scalars(
            select(AttendanceRecord).where(
                AttendanceRecord.employee_id.in_(employee_ids),
                AttendanceRecord.period == period,
            )
        ).all()
    }
    performance_by_employee = {
        record.employee_id: record
        for record in session.scalars(
            select(PerformanceRecord).where(
                PerformanceRecord.employee_id.in_(employee_ids),
                PerformanceRecord.period == period,
            )
        ).all()
    }
    calendar_finalized, calendar_holidays, calendar_errors = _load_holiday_calendar(session, period)
    holiday_records_by_employee: dict[int, list[HolidayWorkRecord]] = {
        employee_id: [] for employee_id in employee_ids
    }
    if calendar_finalized:
        for record in session.scalars(
            select(HolidayWorkRecord).where(
                HolidayWorkRecord.employee_id.in_(employee_ids),
                HolidayWorkRecord.holiday_date >= on_date,
                HolidayWorkRecord.holiday_date < _period_end(period),
            )
        ).all():
            holiday_records_by_employee[record.employee_id].append(record)
    carry_inputs = _locked_carry_inputs(session, employee_ids, period)
    monthly_adjustment_inputs = _monthly_adjustment_inputs(session, employee_ids, period)
    policy_inputs = _policy_inputs_by_employee(session, employees, period)

    days_in_month = _days_in_month(period)
    inputs: dict[int, tuple[EmployeeInput, list[int]]] = {}
    for employee in employees:
        effective_special_position = (
            employee.is_special_position
            or requires_approved_attendance_days(employee.position_title)
        )
        structures = structures_by_employee[employee.id]
        missing = sorted(
            {
                structure.component_id
                for structure in structures
                if structure.component_id not in comp_meta
            }
        )
        payroll_structure = [
            StructureComponent(
                code=comp_meta[structure.component_id][0],
                component_type=comp_meta[structure.component_id][1],
                amount=structure.amount,
                allowance_kind=comp_meta[structure.component_id][2],
                taxable=comp_meta[structure.component_id][3],
                in_social_base=comp_meta[structure.component_id][4],
                in_housing_base=comp_meta[structure.component_id][5],
                prorate_by_attendance=comp_meta[structure.component_id][6],
            )
            for structure in structures
            if structure.component_id in comp_meta
        ]
        attendance, generated_expected_days, expected_days_rule_id, schedule_errors = (
            _attendance_input(
                session,
                employee,
                period,
                attendance_by_employee.get(employee.id),
            )
        )
        holiday_inputs: tuple[StatutoryHoliday, ...] = ()
        holiday_errors = calendar_errors
        if calendar_finalized and not holiday_errors:
            worked_by_date, work_errors = _holiday_work_map(
                holiday_records_by_employee[employee.id], calendar_holidays
            )
            if work_errors:
                holiday_errors = work_errors
            else:
                holiday_inputs, holiday_errors = _eligible_holiday_inputs(
                    employee, calendar_holidays, worked_by_date
                )
        is_new = _same_period(employee.hire_date, period)
        carry_forward, deferred_deductions, deferred_deposit = carry_inputs.get(
            employee.id, _EMPTY_CARRY_INPUT
        )
        monthly_adjustments = monthly_adjustment_inputs.get(
            employee.id, _EMPTY_MONTHLY_ADJUSTMENT_INPUT
        )
        policy_input = policy_inputs[employee.id]
        lifecycle_errors = _employee_lifecycle_errors(employee)
        if lifecycle_errors:
            holiday_inputs = ()
        inputs[employee.id] = (
            EmployeeInput(
                employee_id=employee.id,
                period=period,
                days_in_month=days_in_month,
                employment_type=employee.employment_type,
                department=employee.department,
                is_special_position=effective_special_position,
                structure=payroll_structure,
                attendance=attendance,
                generated_expected_days=generated_expected_days,
                expected_days_rule_id=expected_days_rule_id,
                performance_coefficient=(
                    performance_by_employee[employee.id].coefficient
                    if employee.id in performance_by_employee
                    else Decimal("1")
                ),
                statutory_holiday_days=Decimal("0"),
                holiday_eligible=(
                    not lifecycle_errors
                    and employee.hire_date is not None
                    and employee.hire_date <= on_date
                ),
                holiday_calendar_finalized=calendar_finalized,
                statutory_holidays=holiday_inputs,
                hire_date=employee.hire_date,
                probation_end=employee.probation_end,
                leave_date=employee.leave_date,
                is_new_employee=is_new,
                is_hire_or_leave_month=is_new or _same_period(employee.leave_date, period),
                prev_makeup=monthly_adjustments.prev_makeup,
                prev_deduct=monthly_adjustments.prev_deduct,
                prev_makeup_taxable=monthly_adjustments.prev_makeup_taxable,
                prev_makeup_in_social_base=monthly_adjustments.prev_makeup_in_social_base,
                prev_makeup_in_housing_base=monthly_adjustments.prev_makeup_in_housing_base,
                prev_deduct_taxable=monthly_adjustments.prev_deduct_taxable,
                prev_deduct_in_social_base=monthly_adjustments.prev_deduct_in_social_base,
                prev_deduct_in_housing_base=monthly_adjustments.prev_deduct_in_housing_base,
                prior_carry_forward=carry_forward,
                prior_deferred_deductions=deferred_deductions,
                prior_deferred_deposit=deferred_deposit,
                payroll_policy=policy_input.payroll_policy,
                monthly_special_deduction=policy_input.monthly_special_deduction,
                tax_ytd=policy_input.tax_ytd,
                tax_employment_months=policy_input.tax_employment_months,
                tax_opening=policy_input.tax_opening,
                source_exceptions=(
                    *lifecycle_errors,
                    *schedule_errors,
                    *holiday_errors,
                    *policy_input.source_exceptions,
                ),
            ),
            missing,
        )
    return inputs


def preview(
    session: Session, employee: Employee, period: str, cfg: RuleConfig | None = None
) -> PayrollResult:
    inp, missing = build_input(session, employee, period)
    result = compute(inp, cfg)
    if missing:
        result.exceptions.append(f"存在无法解析的薪资组件(id={missing})，已阻断出账")
    return result
