"""薪资批次核算运行 + 复核/异议/自动重算 + 锁定/解锁（规格第 1、8 节）。"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased

from app.comp.service import StructureError, set_component_amount
from app.models.attendance import AttendanceRecord, PerformanceRecord
from app.models.comp import (
    AllowanceKind,
    ComponentType,
    EmployeeSalaryStructure,
    SalaryComponentDef,
)
from app.models.employee import Department, Employee, EmploymentType
from app.models.holiday import HolidayWorkRecord
from app.models.org import OrgType, OrgUnit
from app.models.payroll_adjustment import (
    MonthlyPayrollAdjustment,
    MonthlyPayrollAdjustmentRevision,
    PayrollAdjustmentType,
)
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import (
    AdjustmentRecord,
    BatchConfirmation,
    CompDispute,
    ConfirmStatus,
    DisputeEvent,
    DisputeEventType,
    DisputeStatus,
    PayrollResult,
)
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    RuleConfig,
    StatutoryHoliday,
    StructureComponent,
    TaxOpeningProvenance,
    TaxYearToDate,
    compute,
)
from app.payroll.engine import PayrollResult as EngineResult
from app.payroll.guards import lock_payroll_input_mutation
from app.payroll.service import build_input, build_inputs
from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    DerivedIncomeRule,
    PayrollPolicyContext,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
    validate_derived_income_rules,
)


class BatchError(Exception):
    pass


_SUPPORTED_RECOMPUTE_RULE_VERSIONS = frozenset({"v2", "v3", "v4"})


def _now(session: Session) -> datetime:
    return session.scalar(select(func.now()))  # type: ignore[return-value]


def _lock_batch(session: Session, batch: PayrollBatch) -> None:
    """Refresh and lock the state-machine row for the duration of the transaction."""
    session.refresh(batch, with_for_update=True)


def _lines_json(res: EngineResult) -> list[dict]:
    return [
        {"code": li.code, "category": li.category, "formula": li.formula, "amount": str(li.amount)}
        for li in res.lines
    ]


def _result_line_amount(lines: list, code: str) -> Decimal:
    total = Decimal("0")
    for line in lines:
        if not isinstance(line, Mapping) or line.get("code") != code:
            continue
        try:
            amount = Decimal(str(line.get("amount", "0")))
        except (InvalidOperation, TypeError, ValueError):
            continue
        if amount.is_finite():
            total += amount
    return total.quantize(Decimal("0.01"))


def _result_audit_snapshot(
    result: PayrollResult, *, status: str | None = None
) -> dict[str, object]:
    """Serialize every linked payroll output required by adjustment audit."""
    snapshot: dict[str, object] = {
        "version": result.version,
        "batch_version": result.batch_version,
        "rule_version": result.rule_version,
        "input_snapshot": result.input_snapshot,
        "actual_attendance_days": str(result.actual_attendance_days),
        "statutory_holiday_days": str(result.statutory_holiday_days),
        "statutory_holiday_worked_days": str(result.statutory_holiday_worked_days),
        "statutory_holiday_pay": str(_result_line_amount(result.lines, "HOLIDAY")),
        "gross": str(result.gross),
        "deposit": str(result.deposit),
        "net": str(result.net),
        "carry_forward": str(result.carry_forward),
        "deferred_deductions": str(result.deferred_deductions),
        "deferred_deposit": str(result.deferred_deposit),
        "lines": list(result.lines),
        "exceptions": list(result.exceptions),
        "warnings": list(result.warnings),
    }
    if status is not None:
        snapshot["status"] = status
    return snapshot


def _result_calculation_signature(result: PayrollResult) -> tuple[object, ...]:
    """Return calculation outputs, excluding identity and mutable audit metadata."""
    line_amounts = tuple(
        (
            str(line.get("code")),
            str(line.get("amount")),
        )
        for line in result.lines
        if isinstance(line, Mapping)
    )
    return (
        str(result.actual_attendance_days),
        str(result.statutory_holiday_days),
        str(result.statutory_holiday_worked_days),
        str(result.gross),
        str(result.deposit),
        str(result.net),
        str(result.carry_forward),
        str(result.deferred_deductions),
        str(result.deferred_deposit),
        line_amounts,
        bool(result.has_error),
        tuple(str(value) for value in result.exceptions),
    )


def _enum_value(value: object | None) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _snapshot_optional_decimal(value: object | None, field: str) -> Decimal | None:
    if value is None:
        return None
    return _snapshot_decimal(value, field)


def _holiday_snapshot_entry(raw: StatutoryHoliday | Mapping[str, object]) -> dict[str, object]:
    if isinstance(raw, StatutoryHoliday):
        return {"date": raw.day.isoformat(), "worked": raw.worked}
    if not isinstance(raw, Mapping):
        raise BatchError("Statutory holiday snapshot entry is invalid.")
    raw_day = raw.get("date")
    if isinstance(raw_day, date):
        day = raw_day
    else:
        try:
            day = date.fromisoformat(str(raw_day))
        except (TypeError, ValueError) as exc:
            raise BatchError("Statutory holiday snapshot date is invalid.") from exc
    worked = raw.get("worked", False)
    if not isinstance(worked, bool):
        raise BatchError("Statutory holiday snapshot work state is invalid.")
    return {"date": day.isoformat(), "worked": worked}


def _snapshot_holidays(value: object) -> tuple[StatutoryHoliday, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise BatchError("The persisted input snapshot has invalid statutory holidays.")
    holidays: list[StatutoryHoliday] = []
    for raw in value:
        entry = _holiday_snapshot_entry(raw) if isinstance(raw, Mapping) else None
        if entry is None:
            raise BatchError("The persisted input snapshot has invalid statutory holidays.")
        holidays.append(
            StatutoryHoliday(
                day=date.fromisoformat(str(entry["date"])), worked=bool(entry["worked"])
            )
        )
    return tuple(holidays)


def _snapshot_date(value: object | None, field: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise BatchError(f"Persisted input snapshot has an invalid date for {field}.") from exc


def _snapshot_datetime(value: object | None, field: str) -> datetime | None:
    if value is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError) as exc:
        raise BatchError(f"Persisted input snapshot has an invalid datetime for {field}.") from exc
    if parsed.tzinfo is None:
        raise BatchError(f"Persisted input snapshot has a timezone-naive datetime for {field}.")
    return parsed


def _snapshot_bool(value: object, field: str) -> bool:
    if not isinstance(value, bool):
        raise BatchError(f"Persisted input snapshot has an invalid boolean for {field}.")
    return value


def _snapshot_optional_bool(value: object | None, field: str) -> bool | None:
    if value is None:
        return None
    return _snapshot_bool(value, field)


def _snapshot_int(value: object, field: str, *, minimum: int = 0, maximum: int = 12) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise BatchError(f"Persisted input snapshot has an invalid integer for {field}.")
    return value


def _tax_context_snapshot(inp: EmployeeInput) -> dict[str, object]:
    """Serialize cumulative tax facts even when policy selection failed."""

    return {
        "monthly_special_deduction": str(inp.monthly_special_deduction),
        "employment_months": inp.tax_employment_months,
        "ytd": {
            "taxable_income_before": str(inp.tax_ytd.taxable_income_before),
            "employee_contribution_before": str(inp.tax_ytd.employee_contribution_before),
            "special_deduction_before": str(inp.tax_ytd.special_deduction_before),
            "tax_withheld_before": str(inp.tax_ytd.tax_withheld_before),
            "employment_months_before": inp.tax_ytd.employment_months_before,
        },
        "opening": (
            {
                "opening_id": inp.tax_opening.opening_id,
                "revision": inp.tax_opening.revision,
                "tax_year": inp.tax_opening.tax_year,
                "through_period": inp.tax_opening.through_period,
                "evidence_ref": inp.tax_opening.evidence_ref,
                "finalized_by": inp.tax_opening.finalized_by,
                "finalized_at": (
                    inp.tax_opening.finalized_at.isoformat()
                    if inp.tax_opening.finalized_at is not None
                    else None
                ),
            }
            if inp.tax_opening is not None
            else None
        ),
    }


def _payroll_tax_snapshot(inp: EmployeeInput) -> dict[str, object]:
    """Serialize the selected policy and cumulative inputs without live DB pointers."""

    context = _tax_context_snapshot(inp)
    policy = inp.payroll_policy
    if policy is None:
        # A missing/invalid policy intentionally produces an errored result,
        # but its audited YTD source facts still matter for investigation and
        # a later correction-round recomputation.
        return {"schema_version": 2, "policy": None, **context}
    return {
        "schema_version": 2,
        "policy": {
            "policy_id": policy.policy_id,
            "city": policy.city,
            "effective_from": policy.effective_from.isoformat(),
            "social_rules": [
                {
                    "kind": rule.kind.value,
                    "employee_rate": str(rule.employee_rate),
                    "employer_rate": str(rule.employer_rate),
                    "base_min": str(rule.base_min),
                    "base_max": str(rule.base_max) if rule.base_max is not None else None,
                }
                for rule in policy.social_policy.rules
            ],
            "monthly_basic_deduction": str(policy.tax_policy.monthly_basic_deduction),
            "tax_brackets": [
                {
                    "upper_bound": (
                        str(bracket.upper_bound) if bracket.upper_bound is not None else None
                    ),
                    "rate": str(bracket.rate),
                    "quick_deduction": str(bracket.quick_deduction),
                }
                for bracket in policy.tax_policy.brackets
            ],
            "derived_income_rules": [
                {
                    "code": rule.code,
                    "taxable": rule.taxable,
                    "in_social_base": rule.in_social_base,
                    "in_housing_base": rule.in_housing_base,
                }
                for rule in policy.derived_income_rules
            ],
        },
        **context,
    }


def _tax_context_from_snapshot(
    value: Mapping[str, object],
) -> tuple[Decimal, TaxYearToDate, int | None, TaxOpeningProvenance | None]:
    """Restore cumulative tax facts independently from a selected policy."""

    ytd = value.get("ytd")
    if not isinstance(ytd, Mapping):
        raise BatchError("The persisted input snapshot has an incomplete payroll tax context.")
    try:
        tax_ytd = TaxYearToDate(
            taxable_income_before=_snapshot_decimal(
                ytd["taxable_income_before"], "tax_ytd.taxable_income_before"
            ),
            employee_contribution_before=_snapshot_decimal(
                ytd["employee_contribution_before"], "tax_ytd.employee_contribution_before"
            ),
            special_deduction_before=_snapshot_decimal(
                ytd["special_deduction_before"], "tax_ytd.special_deduction_before"
            ),
            tax_withheld_before=_snapshot_decimal(
                ytd["tax_withheld_before"], "tax_ytd.tax_withheld_before"
            ),
            employment_months_before=(
                _snapshot_int(ytd["employment_months_before"], "tax_ytd.employment_months_before")
                if "employment_months_before" in ytd
                else 0
            ),
        )
        special = _snapshot_decimal(value["monthly_special_deduction"], "monthly_special_deduction")
        employment_months = (
            _snapshot_int(value["employment_months"], "tax_employment_months", minimum=1)
            if value.get("employment_months") is not None
            else None
        )
        opening_value = value.get("opening")
        tax_opening: TaxOpeningProvenance | None = None
        if opening_value is not None:
            if not isinstance(opening_value, Mapping):
                raise BatchError(
                    "The persisted input snapshot has an invalid tax opening provenance."
                )
            tax_opening = TaxOpeningProvenance(
                opening_id=_snapshot_int(
                    opening_value["opening_id"],
                    "tax_opening.opening_id",
                    minimum=1,
                    maximum=2**63 - 1,
                ),
                revision=_snapshot_int(
                    opening_value["revision"],
                    "tax_opening.revision",
                    minimum=1,
                    maximum=2**31 - 1,
                ),
                tax_year=_snapshot_int(
                    opening_value["tax_year"], "tax_opening.tax_year", minimum=2000, maximum=9999
                ),
                through_period=str(opening_value["through_period"]),
                evidence_ref=str(opening_value["evidence_ref"]),
                finalized_by=(
                    _snapshot_int(
                        opening_value["finalized_by"],
                        "tax_opening.finalized_by",
                        minimum=1,
                        maximum=2**63 - 1,
                    )
                    if opening_value.get("finalized_by") is not None
                    else None
                ),
                finalized_at=_snapshot_datetime(
                    opening_value.get("finalized_at"), "tax_opening.finalized_at"
                ),
            )
            if (
                not tax_opening.evidence_ref.strip()
                or _snapshot_date(tax_opening.through_period + "-01", "tax_opening.through_period")
                is None
            ):
                raise BatchError("The persisted input snapshot has invalid tax opening provenance.")
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchError(
            "The persisted input snapshot has an invalid payroll tax context."
        ) from exc
    return special, tax_ytd, employment_months, tax_opening


def _payroll_tax_from_snapshot(
    value: object,
    *,
    rule_version: str,
) -> tuple[
    PayrollPolicyContext | None,
    Decimal,
    TaxYearToDate,
    int | None,
    TaxOpeningProvenance | None,
]:
    """Restore a policy context using the original snapshot schema version."""

    if value is None:
        return None, Decimal("0"), TaxYearToDate(), None, None
    if not isinstance(value, Mapping):
        raise BatchError("The persisted input snapshot has an invalid payroll tax context.")
    if "schema_version" in value:
        _snapshot_int(value["schema_version"], "payroll_tax.schema_version", minimum=2, maximum=2)
    special, tax_ytd, employment_months, tax_opening = _tax_context_from_snapshot(value)
    raw_policy = value.get("policy", value)
    if raw_policy is None:
        return None, special, tax_ytd, employment_months, tax_opening
    if rule_version == "v4" and employment_months is None:
        raise BatchError("The persisted v4 tax snapshot has no employment-month count.")
    if not isinstance(raw_policy, Mapping):
        raise BatchError("The persisted input snapshot has an invalid payroll tax policy.")
    social_rules = raw_policy.get("social_rules")
    tax_brackets = raw_policy.get("tax_brackets")
    derived_income_rules = raw_policy.get("derived_income_rules", [])
    if (
        not isinstance(social_rules, list)
        or not isinstance(tax_brackets, list)
        or not isinstance(derived_income_rules, list)
    ):
        raise BatchError("The persisted input snapshot has an incomplete payroll tax policy.")
    try:
        effective_from = _snapshot_date(raw_policy["effective_from"], "policy.effective_from")
        if effective_from is None:
            raise BatchError("The persisted input snapshot has no payroll policy effective date.")
        city = str(raw_policy["city"])
        policy = PayrollPolicyContext(
            policy_id=int(raw_policy["policy_id"]),
            city=city,
            effective_from=effective_from,
            social_policy=SocialInsurancePolicyInput(
                city=city,
                rules=tuple(
                    ContributionRule(
                        kind=ContributionKind(str(rule["kind"])),
                        employee_rate=_snapshot_decimal(
                            rule["employee_rate"], "policy.social_rules.employee_rate"
                        ),
                        employer_rate=_snapshot_decimal(
                            rule["employer_rate"], "policy.social_rules.employer_rate"
                        ),
                        base_min=_snapshot_decimal(
                            rule["base_min"], "policy.social_rules.base_min"
                        ),
                        base_max=(
                            _snapshot_decimal(rule["base_max"], "policy.social_rules.base_max")
                            if rule.get("base_max") is not None
                            else None
                        ),
                    )
                    for rule in social_rules
                    if isinstance(rule, Mapping)
                ),
            ),
            tax_policy=TaxPolicyInput(
                monthly_basic_deduction=_snapshot_decimal(
                    raw_policy["monthly_basic_deduction"], "policy.monthly_basic_deduction"
                ),
                brackets=tuple(
                    TaxBracket(
                        upper_bound=(
                            _snapshot_decimal(
                                bracket["upper_bound"], "policy.tax_brackets.upper_bound"
                            )
                            if bracket.get("upper_bound") is not None
                            else None
                        ),
                        rate=_snapshot_decimal(bracket["rate"], "policy.tax_brackets.rate"),
                        quick_deduction=_snapshot_decimal(
                            bracket["quick_deduction"], "policy.tax_brackets.quick_deduction"
                        ),
                    )
                    for bracket in tax_brackets
                    if isinstance(bracket, Mapping)
                ),
            ),
            derived_income_rules=tuple(
                DerivedIncomeRule(
                    code=str(rule["code"]),
                    taxable=rule["taxable"],
                    in_social_base=rule["in_social_base"],
                    in_housing_base=rule["in_housing_base"],
                )
                for rule in derived_income_rules
                if isinstance(rule, Mapping)
            ),
        )
        if (
            len(policy.social_policy.rules) != len(social_rules)
            or len(policy.tax_policy.brackets) != len(tax_brackets)
            or len(policy.derived_income_rules) != len(derived_income_rules)
        ):
            raise BatchError(
                "The persisted input snapshot has an invalid payroll tax policy entry."
            )
        validate_derived_income_rules(policy.derived_income_rules)
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchError(
            "The persisted input snapshot has an invalid payroll tax context."
        ) from exc
    return policy, special, tax_ytd, employment_months, tax_opening


def _input_snapshot(inp: EmployeeInput, missing_component_ids: list[int]) -> dict:
    """Return a JSON-safe, immutable representation of the engine inputs."""
    attendance = None
    if inp.attendance is not None:
        attendance = {
            "expected_days": str(inp.attendance.expected_days),
            "actual_days": str(inp.attendance.actual_days),
            "worked_hours": (
                str(inp.attendance.worked_hours)
                if inp.attendance.worked_hours is not None
                else None
            ),
            "rest_days": str(inp.attendance.rest_days),
            "overtime_hours": str(inp.attendance.overtime_hours),
            "holiday_worked_days": str(inp.attendance.holiday_worked_days),
        }
    return {
        "employee_id": inp.employee_id,
        "period": inp.period,
        "days_in_month": str(inp.days_in_month),
        "employment_type": _enum_value(inp.employment_type),
        "department": _enum_value(inp.department),
        "is_special_position": inp.is_special_position,
        "attendance": attendance,
        "generated_expected_days": (
            str(inp.generated_expected_days) if inp.generated_expected_days is not None else None
        ),
        "expected_days_rule_id": inp.expected_days_rule_id,
        "performance_coefficient": (
            str(inp.performance_coefficient) if inp.performance_coefficient is not None else None
        ),
        "is_new_employee": inp.is_new_employee,
        "is_hire_or_leave_month": inp.is_hire_or_leave_month,
        "holiday_eligible": inp.holiday_eligible,
        "statutory_holiday_days": str(inp.statutory_holiday_days),
        "holiday_calendar_finalized": inp.holiday_calendar_finalized,
        "statutory_holidays": [
            _holiday_snapshot_entry(holiday) for holiday in inp.statutory_holidays
        ],
        "hire_date": inp.hire_date.isoformat() if inp.hire_date else None,
        "probation_end": inp.probation_end.isoformat() if inp.probation_end else None,
        "leave_date": inp.leave_date.isoformat() if inp.leave_date else None,
        "prev_makeup": str(inp.prev_makeup),
        "prev_deduct": str(inp.prev_deduct),
        "prev_makeup_taxable": inp.prev_makeup_taxable,
        "prev_makeup_in_social_base": inp.prev_makeup_in_social_base,
        "prev_makeup_in_housing_base": inp.prev_makeup_in_housing_base,
        "prev_deduct_taxable": inp.prev_deduct_taxable,
        "prev_deduct_in_social_base": inp.prev_deduct_in_social_base,
        "prev_deduct_in_housing_base": inp.prev_deduct_in_housing_base,
        "prior_carry_forward": str(inp.prior_carry_forward),
        "prior_deferred_deductions": str(inp.prior_deferred_deductions),
        "prior_deferred_deposit": str(inp.prior_deferred_deposit),
        "payroll_tax": _payroll_tax_snapshot(inp),
        "source_exceptions": list(inp.source_exceptions),
        "structure": [
            {
                "code": component.code,
                "component_type": _enum_value(component.component_type),
                "amount": str(component.amount),
                "allowance_kind": _enum_value(component.allowance_kind),
                "taxable": component.taxable,
                "in_social_base": component.in_social_base,
                "in_housing_base": component.in_housing_base,
                "prorate_by_attendance": component.prorate_by_attendance,
            }
            for component in inp.structure
        ],
        "missing_component_ids": list(missing_component_ids),
    }


def _tax_withholding_snapshot(res: EngineResult) -> dict[str, str | int] | None:
    """Persist structured tax facts; never make later periods parse display lines."""

    state = getattr(res, "tax_state", None)
    if state is None:
        return None
    snapshot: dict[str, str | int] = {
        "current_taxable_income": str(state.current_taxable_income),
        "current_employee_contribution": str(state.current_employee_contribution),
        "current_special_deduction": str(state.current_special_deduction),
        "current_tax_withheld": str(state.current_tax_withheld),
        "cumulative_taxable_income": str(state.cumulative_taxable_income),
        "cumulative_tax_due": str(state.cumulative_tax_due),
    }
    if state.employment_months_to_date is not None:
        snapshot["employment_months_to_date"] = state.employment_months_to_date
    return snapshot


def _social_contribution_snapshot(
    inp: EmployeeInput, res: EngineResult
) -> dict[str, dict[str, str]] | None:
    """Persist explicit social-fund totals, including legitimate zeroes.

    Calculation lines intentionally omit zero amounts for a compact payslip.
    Regulatory exports cannot infer whether an absent line means a lawful zero
    or a damaged/legacy result, so the immutable input snapshot carries every
    configured contribution kind explicitly.
    """

    if inp.payroll_policy is None:
        return None
    line_amounts = {line.code: line.amount for line in res.lines}
    snapshot: dict[str, dict[str, str]] = {}
    for rule in inp.payroll_policy.social_policy.rules:
        prefix = (
            "HOUSING_FUND" if rule.kind is ContributionKind.HOUSING else f"SOCIAL_{rule.kind.value}"
        )
        employee_amount = line_amounts.get(f"{prefix}_EMPLOYEE", Decimal(0))
        employer_amount = line_amounts.get(f"{prefix}_EMPLOYER", Decimal(0))
        snapshot[rule.kind.value] = {
            "employee": str(abs(employee_amount)),
            "employer": str(abs(employer_amount)),
        }
    return snapshot


def _result_input_snapshot(
    inp: EmployeeInput, missing_component_ids: list[int], result: EngineResult
) -> dict:
    snapshot = _input_snapshot(inp, missing_component_ids)
    snapshot["tax_withholding"] = _tax_withholding_snapshot(result)
    snapshot["social_contributions"] = _social_contribution_snapshot(inp, result)
    return snapshot


def _calculate_result(session: Session, emp: Employee, period: str) -> tuple[EngineResult, dict]:
    """Build once so the persisted result and its input snapshot cannot drift."""
    inp, missing_component_ids = build_input(session, emp, period)
    return _calculate_loaded_input(inp, missing_component_ids)


def _calculate_loaded_input(
    inp: EmployeeInput, missing_component_ids: list[int]
) -> tuple[EngineResult, dict]:
    """Calculate a preloaded input without issuing any database reads."""
    result = compute(inp)
    if missing_component_ids:
        result.exceptions.append(
            f"Unresolved salary component ids {missing_component_ids}; payroll output is blocked."
        )
    return result, _result_input_snapshot(inp, missing_component_ids, result)


def _snapshot_decimal(value: object, field: str) -> Decimal:
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise BatchError(f"Persisted input snapshot has an invalid decimal for {field}.") from exc


def _snapshot_positive_int(value: object, field: str) -> int:
    if isinstance(value, bool):
        raise BatchError(f"Persisted input snapshot has an invalid integer for {field}.")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise BatchError(f"Persisted input snapshot has an invalid integer for {field}.") from exc
    if parsed <= 0:
        raise BatchError(f"Persisted input snapshot has an invalid integer for {field}.")
    return parsed


def _snapshot_string_tuple(value: object, field: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise BatchError(f"Persisted input snapshot has an invalid list for {field}.")
    return tuple(str(item) for item in value)


def _decimal_scale(value: Decimal, field: str) -> int:
    """Return decimal places after finite-value validation, with explicit typing."""

    exponent = value.as_tuple().exponent
    if not isinstance(exponent, int):
        raise BatchError(f"{field} must be a finite decimal.")
    return max(-exponent, 0)


def _input_from_snapshot(
    prior_result: PayrollResult,
    attendance_changes: Mapping[str, Decimal],
    *,
    source_snapshot: Mapping[str, object] | None = None,
) -> tuple[EmployeeInput, list[int]]:
    """Rebuild the engine input from the prior immutable snapshot, not live employee data."""
    if prior_result.rule_version not in _SUPPORTED_RECOMPUTE_RULE_VERSIONS:
        raise BatchError("The historical payroll rule version is unavailable for recomputation.")
    snapshot = source_snapshot if source_snapshot is not None else prior_result.input_snapshot
    raw_attendance = snapshot.get("attendance")
    if not isinstance(raw_attendance, Mapping):
        raise BatchError("The persisted input snapshot has no attendance source data.")

    attendance_values = dict(raw_attendance)
    attendance_values.update({field: str(value) for field, value in attendance_changes.items()})
    try:
        attendance = Attendance(
            expected_days=_snapshot_decimal(attendance_values["expected_days"], "expected_days"),
            actual_days=_snapshot_decimal(attendance_values["actual_days"], "actual_days"),
            worked_hours=_snapshot_optional_decimal(
                attendance_values["worked_hours"], "worked_hours"
            ),
            rest_days=_snapshot_decimal(attendance_values["rest_days"], "rest_days"),
            overtime_hours=_snapshot_decimal(attendance_values["overtime_hours"], "overtime_hours"),
            holiday_worked_days=_snapshot_decimal(
                attendance_values["holiday_worked_days"], "holiday_worked_days"
            ),
        )
    except KeyError as exc:
        raise BatchError(
            "The persisted input snapshot has incomplete attendance source data."
        ) from exc

    raw_structure = snapshot.get("structure")
    if not isinstance(raw_structure, list):
        raise BatchError("The persisted input snapshot has no salary structure.")
    try:
        (
            payroll_policy,
            monthly_special_deduction,
            tax_ytd,
            tax_employment_months,
            tax_opening,
        ) = _payroll_tax_from_snapshot(
            snapshot.get("payroll_tax"), rule_version=prior_result.rule_version
        )
        structure = [
            StructureComponent(
                code=str(component["code"]),
                component_type=ComponentType(str(component["component_type"])),
                amount=_snapshot_decimal(component["amount"], "structure.amount"),
                allowance_kind=(
                    AllowanceKind(str(component["allowance_kind"]))
                    if component.get("allowance_kind") is not None
                    else None
                ),
                # Pre-S12 snapshots have no component flags.  Their legacy
                # engine behavior remains unchanged because they also have no
                # payroll-tax context.
                taxable=_snapshot_bool(component.get("taxable", True), "structure.taxable"),
                in_social_base=_snapshot_bool(
                    component.get("in_social_base", False), "structure.in_social_base"
                ),
                in_housing_base=_snapshot_bool(
                    component.get("in_housing_base", False), "structure.in_housing_base"
                ),
                # Pre-D13 snapshots did not support attendance-prorated allowances.
                prorate_by_attendance=_snapshot_bool(
                    component.get("prorate_by_attendance", False),
                    "structure.prorate_by_attendance",
                ),
            )
            for component in raw_structure
            if isinstance(component, Mapping)
        ]
        if len(structure) != len(raw_structure):
            raise BatchError("The persisted input snapshot has an invalid salary structure entry.")
        performance = snapshot.get("performance_coefficient")
        inp = EmployeeInput(
            employee_id=_snapshot_positive_int(snapshot["employee_id"], "employee_id"),
            period=str(snapshot["period"]),
            days_in_month=_snapshot_decimal(snapshot["days_in_month"], "days_in_month"),
            employment_type=EmploymentType(str(snapshot["employment_type"])),
            department=Department(str(snapshot["department"])),
            is_special_position=bool(snapshot["is_special_position"]),
            structure=structure,
            attendance=attendance,
            generated_expected_days=_snapshot_optional_decimal(
                snapshot.get("generated_expected_days"), "generated_expected_days"
            ),
            expected_days_rule_id=(
                _snapshot_positive_int(snapshot["expected_days_rule_id"], "expected_days_rule_id")
                if snapshot.get("expected_days_rule_id") is not None
                else None
            ),
            performance_coefficient=(
                _snapshot_decimal(performance, "performance_coefficient")
                if performance is not None
                else None
            ),
            is_new_employee=bool(snapshot["is_new_employee"]),
            is_hire_or_leave_month=bool(snapshot["is_hire_or_leave_month"]),
            holiday_eligible=bool(snapshot["holiday_eligible"]),
            statutory_holiday_days=_snapshot_decimal(
                snapshot["statutory_holiday_days"], "statutory_holiday_days"
            ),
            holiday_calendar_finalized=bool(snapshot.get("holiday_calendar_finalized", True)),
            statutory_holidays=_snapshot_holidays(snapshot.get("statutory_holidays", [])),
            hire_date=_snapshot_date(snapshot.get("hire_date"), "hire_date"),
            probation_end=_snapshot_date(snapshot.get("probation_end"), "probation_end"),
            leave_date=_snapshot_date(snapshot.get("leave_date"), "leave_date"),
            prev_makeup=_snapshot_decimal(snapshot["prev_makeup"], "prev_makeup"),
            prev_deduct=_snapshot_decimal(snapshot["prev_deduct"], "prev_deduct"),
            prev_makeup_taxable=_snapshot_optional_bool(
                snapshot.get("prev_makeup_taxable"), "prev_makeup_taxable"
            ),
            prev_makeup_in_social_base=_snapshot_optional_bool(
                snapshot.get("prev_makeup_in_social_base"),
                "prev_makeup_in_social_base",
            ),
            prev_makeup_in_housing_base=_snapshot_optional_bool(
                snapshot.get("prev_makeup_in_housing_base"),
                "prev_makeup_in_housing_base",
            ),
            prev_deduct_taxable=_snapshot_optional_bool(
                snapshot.get("prev_deduct_taxable"), "prev_deduct_taxable"
            ),
            prev_deduct_in_social_base=_snapshot_optional_bool(
                snapshot.get("prev_deduct_in_social_base"),
                "prev_deduct_in_social_base",
            ),
            prev_deduct_in_housing_base=_snapshot_optional_bool(
                snapshot.get("prev_deduct_in_housing_base"),
                "prev_deduct_in_housing_base",
            ),
            # Snapshots written before carry-over obligations were introduced
            # remain reproducible: they had no persisted cross-period input.
            prior_carry_forward=_snapshot_decimal(
                snapshot.get("prior_carry_forward", "0"), "prior_carry_forward"
            ),
            prior_deferred_deductions=_snapshot_decimal(
                snapshot.get("prior_deferred_deductions", "0"), "prior_deferred_deductions"
            ),
            prior_deferred_deposit=_snapshot_decimal(
                snapshot.get("prior_deferred_deposit", "0"), "prior_deferred_deposit"
            ),
            payroll_policy=payroll_policy,
            monthly_special_deduction=monthly_special_deduction,
            tax_ytd=tax_ytd,
            tax_employment_months=tax_employment_months,
            tax_opening=tax_opening,
            source_exceptions=_snapshot_string_tuple(
                snapshot.get("source_exceptions", []), "source_exceptions"
            ),
        )
        raw_missing_component_ids = snapshot["missing_component_ids"]
        if not isinstance(raw_missing_component_ids, list):
            raise BatchError("The persisted input snapshot has invalid missing component ids.")
        missing_component_ids = [
            _snapshot_positive_int(component_id, "missing_component_ids")
            for component_id in raw_missing_component_ids
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchError("The persisted input snapshot is invalid for recomputation.") from exc
    return inp, missing_component_ids


def _recompute_result_from_snapshot(
    prior_result: PayrollResult,
    attendance_changes: Mapping[str, Decimal],
    *,
    source_snapshot: Mapping[str, object] | None = None,
) -> tuple[EngineResult, dict]:
    inp, missing_component_ids = _input_from_snapshot(
        prior_result,
        attendance_changes,
        source_snapshot=source_snapshot,
    )
    result = compute(inp, RuleConfig(version=prior_result.rule_version))
    if missing_component_ids:
        result.exceptions.append(
            f"Unresolved salary component ids {missing_component_ids}; payroll output is blocked."
        )
    return result, _result_input_snapshot(inp, missing_component_ids, result)


def _write_result(
    session: Session,
    batch: PayrollBatch,
    emp: Employee,
    res: EngineResult,
    version: int,
    *,
    input_snapshot: dict,
    scope: tuple[int | None, Department] | None = None,
    identity_snapshot: PayrollResult | None = None,
    flush: bool = True,
) -> PayrollResult:
    org_unit_id, department = scope or (emp.org_unit_id, emp.department)

    def identity_value(snapshot_field: str, employee_field: str) -> object | None:
        if identity_snapshot is not None:
            # A missing legacy snapshot must remain missing and fail regulated
            # exports closed; never fill it from mutable current master data.
            return getattr(identity_snapshot, snapshot_field, None)
        return getattr(emp, employee_field, None)

    row = PayrollResult(
        batch_id=batch.id,
        employee_id=emp.id,
        batch_version=batch.version,
        version=version,
        org_unit_id=org_unit_id,
        department=department,
        emp_no_snapshot=identity_value("emp_no_snapshot", "emp_no"),
        employee_name_snapshot=identity_value("employee_name_snapshot", "name"),
        id_card_snapshot=identity_value("id_card_snapshot", "id_card"),
        bank_account_snapshot=identity_value("bank_account_snapshot", "bank_account"),
        social_city_snapshot=identity_value("social_city_snapshot", "social_city"),
        actual_attendance_days=res.actual_attendance_days,
        statutory_holiday_days=getattr(res, "statutory_holiday_days", Decimal("0")),
        statutory_holiday_worked_days=getattr(res, "statutory_holiday_worked_days", Decimal("0")),
        gross=res.gross,
        deposit=res.deposit,
        net=res.net,
        carry_forward=res.carry_forward,
        deferred_deductions=getattr(res, "deferred_deductions", Decimal("0")),
        deferred_deposit=getattr(res, "deferred_deposit", Decimal("0")),
        rule_version=res.rule_version,
        input_snapshot=input_snapshot,
        lines=_lines_json(res),
        exceptions=list(res.exceptions),
        warnings=list(getattr(res, "warnings", [])),
        has_error=res.has_error,
    )
    session.add(row)
    if flush:
        session.flush()
    return row


def _latest_result(
    session: Session,
    batch_id: int,
    employee_id: int,
    *,
    batch_version: int | None = None,
    for_update: bool = False,
) -> PayrollResult | None:
    """Return the latest result, optionally limited to the active review round."""
    statement = (
        select(PayrollResult)
        .where(PayrollResult.batch_id == batch_id, PayrollResult.employee_id == employee_id)
        .order_by(PayrollResult.version.desc())
    )
    if batch_version is not None:
        statement = statement.where(PayrollResult.batch_version == batch_version)
    if for_update:
        statement = statement.with_for_update()
    return session.scalars(statement).first()


def _eligible_employees(session: Session, batch: PayrollBatch) -> list[Employee]:
    """Load the calculation cohort only after the batch row has been locked."""
    return list(
        session.scalars(
            select(Employee)
            .join(OrgUnit, OrgUnit.id == Employee.org_unit_id)
            .where(
                Employee.is_deleted.is_(False),
                or_(Employee.hire_date.is_(None), Employee.hire_date <= batch.attendance_end),
                or_(Employee.leave_date.is_(None), Employee.leave_date >= batch.attendance_start),
                OrgUnit.is_deleted.is_(False),
                OrgUnit.type == OrgType.STORE,
            )
            .with_for_update()
        ).all()
    )


def _assert_no_excluded_outstanding_carry(
    session: Session, batch: PayrollBatch, cohort_employee_ids: set[int]
) -> None:
    """Fail closed when an old obligation would vanish outside this cohort.

    The latest active result of each prior locked batch is authoritative.  It
    is important to choose that result *before* checking its carry value: an
    older unpaid result may already have been settled in a newer locked month.
    """
    prior_results = session.scalars(
        select(PayrollResult)
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .where(
            PayrollBatch.period < batch.period,
            PayrollBatch.status == BatchStatus.LOCKED,
            PayrollResult.batch_version == PayrollBatch.version,
        )
        .order_by(
            PayrollResult.employee_id,
            PayrollBatch.period.desc(),
            PayrollResult.version.desc(),
        )
    ).all()
    latest_by_employee: dict[int, PayrollResult] = {}
    for result in prior_results:
        latest_by_employee.setdefault(result.employee_id, result)

    outstanding_employee_ids: list[int] = []
    for employee_id, result in latest_by_employee.items():
        if employee_id in cohort_employee_ids:
            continue
        carry_forward = Decimal(result.carry_forward)
        deferred_deductions = Decimal(getattr(result, "deferred_deductions", 0))
        deferred_deposit = Decimal(getattr(result, "deferred_deposit", 0))
        if carry_forward != 0 or deferred_deductions != 0 or deferred_deposit != 0:
            outstanding_employee_ids.append(employee_id)

    if outstanding_employee_ids:
        employees = ", ".join(str(employee_id) for employee_id in outstanding_employee_ids)
        raise BatchError(
            "Cannot run payroll while excluded employees have outstanding carry "
            f"obligations ({employees}); complete a final settlement first."
        )


def _invalid_eligible_employee_count(session: Session, batch: PayrollBatch) -> int:
    """Detect legacy employees that cannot be assigned a valid review scope."""
    return (
        session.scalar(
            select(func.count())
            .select_from(Employee)
            .outerjoin(OrgUnit, OrgUnit.id == Employee.org_unit_id)
            .where(
                Employee.is_deleted.is_(False),
                or_(Employee.hire_date.is_(None), Employee.hire_date <= batch.attendance_end),
                or_(Employee.leave_date.is_(None), Employee.leave_date >= batch.attendance_start),
                or_(
                    OrgUnit.id.is_(None),
                    OrgUnit.is_deleted.is_(True),
                    OrgUnit.type != OrgType.STORE,
                ),
            )
        )
        or 0
    )


def run_batch(
    session: Session, batch: PayrollBatch, employees: list[Employee] | None = None
) -> int:
    """核算：对每名员工算薪写结果(version 1)，建立 (门店,部门) 确认行，状态→待门店确认。"""
    lock_payroll_input_mutation(session)
    _lock_batch(session, batch)
    if batch.status != BatchStatus.DRAFT:
        raise BatchError("仅草稿状态批次可核算")
    _assert_sequential_batch_progress(session, batch)
    loaded_inputs: dict[int, tuple[EmployeeInput, list[int]]] | None = None
    if employees is None:
        if _invalid_eligible_employee_count(session, batch):
            raise BatchError("Eligible employees must belong to an active store organization.")
        employees = _eligible_employees(session, batch)
        _assert_no_excluded_outstanding_carry(
            session, batch, {employee.id for employee in employees}
        )
        loaded_inputs = build_inputs(session, employees, batch.period)
    if not employees:
        raise BatchError("Cannot run a batch with no eligible employees.")
    batch.status = BatchStatus.CALCULATING
    session.flush()

    previous_versions: dict[int, int] = {}
    previous_identity_results: dict[int, PayrollResult] = {}
    if loaded_inputs is not None:
        for prior in session.scalars(
            select(PayrollResult)
            .where(
                PayrollResult.batch_id == batch.id,
                PayrollResult.employee_id.in_({employee.id for employee in employees}),
            )
            .with_for_update()
        ).all():
            previous_versions[prior.employee_id] = max(
                previous_versions.get(prior.employee_id, 0), prior.version
            )
            current_identity = previous_identity_results.get(prior.employee_id)
            if current_identity is None or (prior.batch_version, prior.version) > (
                current_identity.batch_version,
                current_identity.version,
            ):
                previous_identity_results[prior.employee_id] = prior

    scopes: set[tuple[int, Department]] = set()
    recalculated_results: dict[int, PayrollResult] = {}
    for emp in employees:
        identity_snapshot: PayrollResult | None = None
        if loaded_inputs is None:
            res, input_snapshot = _calculate_result(session, emp, batch.period)
            previous = _latest_result(session, batch.id, emp.id, for_update=True)
            result_version = (previous.version + 1) if previous is not None else 1
            if batch.version > 1:
                identity_snapshot = previous
        else:
            inp, missing_component_ids = loaded_inputs[emp.id]
            res, input_snapshot = _calculate_loaded_input(inp, missing_component_ids)
            result_version = previous_versions.get(emp.id, 0) + 1
            if batch.version > 1:
                identity_snapshot = previous_identity_results.get(emp.id)
        row = _write_result(
            session,
            batch,
            emp,
            res,
            version=result_version,
            input_snapshot=input_snapshot,
            identity_snapshot=identity_snapshot,
            flush=False,
        )
        recalculated_results[emp.id] = row
        if row.org_unit_id is None:
            raise BatchError("Payroll results require an organization-unit scope for confirmation.")
        scopes.add((row.org_unit_id, row.department))

    # Persist the whole result cohort in one flush while the payroll advisory
    # lock is held, rather than paying one INSERT/RETURNING round trip per
    # employee.
    session.flush()

    for org_unit_id, dept in sorted(scopes, key=lambda scope: (scope[0], scope[1].value)):
        session.add(
            BatchConfirmation(
                batch_id=batch.id,
                batch_version=batch.version,
                org_unit_id=org_unit_id,
                department=dept,
            )
        )
    _reconcile_pending_direct_corrections(session, batch, recalculated_results)
    batch.status = BatchStatus.PENDING_STORE_CONFIRM
    batch.calculated_at = _now(session)
    session.flush()
    return len(employees)


def _reconcile_pending_direct_corrections(
    session: Session,
    batch: PayrollBatch,
    recalculated_results: Mapping[int, PayrollResult] | None = None,
) -> None:
    """Attach a completed recalculation snapshot to direct HR corrections."""
    for adjustment in session.scalars(
        select(AdjustmentRecord).where(AdjustmentRecord.batch_id == batch.id).with_for_update()
    ).all():
        pending = adjustment.recompute_result
        if not isinstance(pending, Mapping):
            continue
        if (
            pending.get("status") != "PENDING_RERUN"
            or pending.get("batch_version") != batch.version
        ):
            continue
        result = (
            recalculated_results.get(adjustment.employee_id)
            if recalculated_results is not None
            else _latest_result(
                session,
                batch.id,
                adjustment.employee_id,
                batch_version=batch.version,
                for_update=True,
            )
        )
        if result is None:
            raise BatchError("A direct payroll correction has no recalculated employee result.")
        prior_result = session.scalars(
            select(PayrollResult)
            .where(
                PayrollResult.batch_id == batch.id,
                PayrollResult.employee_id == adjustment.employee_id,
                PayrollResult.batch_version < batch.version,
            )
            .order_by(PayrollResult.batch_version.desc(), PayrollResult.version.desc())
            .limit(1)
        ).first()
        if prior_result is None:
            raise BatchError("A direct payroll correction has no prior payroll result to compare.")
        if _result_calculation_signature(prior_result) == _result_calculation_signature(result):
            raise BatchError(
                "A direct payroll correction must materially change the employee payroll result."
            )
        adjustment.recompute_result = _result_audit_snapshot(result, status="RECOMPUTED")


def _confirmations(session: Session, batch: PayrollBatch) -> list[BatchConfirmation]:
    return list(
        session.scalars(
            select(BatchConfirmation)
            .where(
                BatchConfirmation.batch_id == batch.id,
                BatchConfirmation.batch_version == batch.version,
            )
            .with_for_update()
        ).all()
    )


_CONFIRMABLE_BATCH_STATES = frozenset({BatchStatus.PENDING_STORE_CONFIRM})
_DISPUTEABLE_BATCH_STATES = frozenset({BatchStatus.PENDING_STORE_CONFIRM, BatchStatus.HAS_DISPUTE})
_RESOLVABLE_BATCH_STATES = frozenset({BatchStatus.HAS_DISPUTE})
_APPROVABLE_BATCH_STATES = frozenset({BatchStatus.PENDING_HR})
_LOCKABLE_BATCH_STATES = frozenset({BatchStatus.CONFIRMED})


def _require_batch_state(
    batch: PayrollBatch, *, allowed: frozenset[BatchStatus], action: str
) -> None:
    if batch.status not in allowed:
        allowed_names = ", ".join(state.value for state in allowed)
        message = f"Cannot {action} while batch is {batch.status.value}; "
        raise BatchError(f"{message}expected {allowed_names}.")


def confirm_scope(
    session: Session, batch: PayrollBatch, org_unit_id: int, department: str, user_id: int
) -> BatchConfirmation:
    _lock_batch(session, batch)
    _require_batch_state(batch, allowed=_CONFIRMABLE_BATCH_STATES, action="confirm a scope")
    conf = session.scalars(
        select(BatchConfirmation)
        .where(
            BatchConfirmation.batch_id == batch.id,
            BatchConfirmation.batch_version == batch.version,
            BatchConfirmation.org_unit_id == org_unit_id,
            BatchConfirmation.department == department,
        )
        .with_for_update()
    ).first()
    if conf is None:
        raise BatchError("该门店/部门无待确认记录")
    if conf is None or conf.status != ConfirmStatus.PENDING:
        raise BatchError("This scope is not pending confirmation.")
    conf.status = ConfirmStatus.CONFIRMED
    conf.confirmed_by = user_id
    conf.confirmed_at = _now(session)
    session.flush()
    # 全部门确认后仍须经过人事最终审核，不能直接跳过 PENDING_HR 锁定。
    if all(c.status == ConfirmStatus.CONFIRMED for c in _confirmations(session, batch)):
        batch.status = BatchStatus.PENDING_HR
    session.flush()
    return conf


def approve_batch(session: Session, batch: PayrollBatch, user_id: int) -> None:
    """Record the HR final-review transition before a batch may be locked."""
    _lock_batch(session, batch)
    _require_batch_state(batch, allowed=_APPROVABLE_BATCH_STATES, action="approve a batch")
    batch.status = BatchStatus.CONFIRMED
    batch.hr_reviewed_by = user_id
    batch.hr_reviewed_at = _now(session)
    session.flush()


def raise_dispute(
    session: Session,
    batch: PayrollBatch,
    employee: Employee,
    salary_item: str,
    opinion: str,
    user_id: int,
) -> CompDispute:
    # 锁定是工资数据的最终保护边界。任何异议必须先经人事解锁，
    # 否则会绕过版本保留与重新确认流程。
    _lock_batch(session, batch)
    if batch.status == BatchStatus.LOCKED:
        raise BatchError("批次已锁定，请先解锁后再提交异议")
    _require_batch_state(batch, allowed=_DISPUTEABLE_BATCH_STATES, action="raise a dispute")
    if batch.status not in (BatchStatus.PENDING_STORE_CONFIRM, BatchStatus.HAS_DISPUTE):
        raise BatchError("当前批次状态不允许提交异议")
    result = _latest_result(
        session,
        batch.id,
        employee.id,
        batch_version=batch.version,
        for_update=True,
    )
    if result is None:
        raise BatchError("The employee has no payroll result in this batch.")
    if result.org_unit_id is None:
        raise BatchError("The payroll result has no organization-unit scope.")
    item_codes = {
        str(line.get("code"))
        for line in getattr(result, "lines", [])
        if isinstance(line, Mapping) and isinstance(line.get("code"), str)
    }
    if salary_item not in item_codes:
        raise BatchError("The disputed salary item is not present in the employee payroll result.")
    dispute = CompDispute(
        batch_id=batch.id,
        batch_version=batch.version,
        employee_id=employee.id,
        salary_item=salary_item,
        opinion=opinion,
        raised_by=user_id,
    )
    session.add(dispute)
    session.flush()
    session.add(
        DisputeEvent(
            dispute_id=dispute.id,
            event_type=DisputeEventType.RAISED.value,
            note=opinion,
            actor_id=user_id,
            attachment_url=None,
        )
    )
    conf = session.scalars(
        select(BatchConfirmation)
        .where(
            BatchConfirmation.batch_id == batch.id,
            BatchConfirmation.batch_version == batch.version,
            BatchConfirmation.org_unit_id == result.org_unit_id,
            BatchConfirmation.department == result.department,
        )
        .with_for_update()
    ).first()
    if conf is not None:
        conf.status = ConfirmStatus.DISPUTED
    batch.status = BatchStatus.HAS_DISPUTE
    session.flush()
    return dispute


def recompute_employee(
    session: Session,
    batch: PayrollBatch,
    emp: Employee,
    *,
    attendance_changes: Mapping[str, Decimal],
) -> PayrollResult:
    """重算并写入新版本结果（保留旧版本，不覆盖历史）。"""
    _lock_batch(session, batch)
    prior_result = _latest_result(
        session,
        batch.id,
        emp.id,
        batch_version=batch.version,
        for_update=True,
    )
    if prior_result is None:
        raise BatchError("Cannot recompute an employee without an existing payroll result.")
    res, input_snapshot = _recompute_result_from_snapshot(prior_result, attendance_changes)
    return _write_result(
        session,
        batch,
        emp,
        res,
        version=prior_result.version + 1,
        input_snapshot=input_snapshot,
        scope=(prior_result.org_unit_id, prior_result.department),
        identity_snapshot=prior_result,
    )


_ATTENDANCE_CHANGE_LIMITS = {
    "expected_days": Decimal("31"),
    "actual_days": Decimal("31"),
    "worked_hours": Decimal("744"),
    "rest_days": Decimal("31"),
    "overtime_hours": Decimal("744"),
}
_ATTENDANCE_DECIMAL_QUANTUM = Decimal("0.01")
# S13 only implements source-data corrections for attendance-derived lines.
# Structure, housing, deduction, and carry-over lines need their own audited
# source-correction workflow; approving them through attendance would be wrong.
_ATTENDANCE_DISPUTE_ITEM_CODES = frozenset({"ATTEND_WAGE", "OVERTIME"})
_ATTENDANCE_DISPUTE_MAX_FIELDS = {
    "ATTEND_WAGE": frozenset({"expected_days", "actual_days", "worked_hours", "rest_days"}),
    "OVERTIME": frozenset({"overtime_hours"}),
}

_POLICY_LINE_PREFIXES = (
    "SOCIAL_",
    "HOUSING_FUND_",
)
_POLICY_LINE_CODES = frozenset({"IIT_WITHHOLDING"})
_CARRY_LINE_CODES = frozenset({"CARRY_FORWARD_WAGE", "CARRY_FORWARD_DEDUCTION"})


def _source_snapshot_copy(result: PayrollResult) -> dict[str, object]:
    snapshot = getattr(result, "input_snapshot", None)
    if not isinstance(snapshot, Mapping):
        raise BatchError("The historical payroll result has no valid source snapshot.")
    return deepcopy(dict(snapshot))


def _snapshot_structure_rows(result: PayrollResult) -> list[Mapping[str, object]]:
    snapshot = getattr(result, "input_snapshot", None)
    rows = snapshot.get("structure") if isinstance(snapshot, Mapping) else None
    if not isinstance(rows, list) or any(not isinstance(row, Mapping) for row in rows):
        return []
    return list(rows)


def _structure_selection_date_from_snapshot(result: PayrollResult) -> date:
    snapshot = getattr(result, "input_snapshot", None)
    if not isinstance(snapshot, Mapping):
        raise BatchError("The historical payroll result has no valid source snapshot.")
    try:
        period_start = date.fromisoformat(f"{snapshot['period']}-01")
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchError("The historical payroll result has an invalid payroll period.") from exc
    hire_date = _snapshot_date(snapshot.get("hire_date"), "hire_date")
    if (
        hire_date is not None
        and hire_date.year == period_start.year
        and hire_date.month == period_start.month
        and hire_date > period_start
    ):
        return hire_date
    return period_start


def _structure_target_types_and_codes(
    result: PayrollResult, salary_item: str
) -> tuple[frozenset[ComponentType], frozenset[str]]:
    rows = _snapshot_structure_rows(result)
    if salary_item == "HOUSING":
        return frozenset({ComponentType.HOUSING}), frozenset(
            str(row.get("code"))
            for row in rows
            if row.get("component_type") == ComponentType.HOUSING.value
        )
    if salary_item == "DEDUCTION":
        return frozenset({ComponentType.DEDUCTION}), frozenset(
            str(row.get("code"))
            for row in rows
            if row.get("component_type") == ComponentType.DEDUCTION.value
        )
    matching_types = frozenset(
        ComponentType(str(row["component_type"]))
        for row in rows
        if row.get("code") == salary_item
        and row.get("component_type") in {member.value for member in ComponentType}
    )
    return matching_types, frozenset({salary_item}) if matching_types else frozenset()


def _structure_source_rows(
    session: Session,
    result: PayrollResult,
    salary_item: str,
    *,
    for_update: bool = False,
) -> list[tuple[EmployeeSalaryStructure, SalaryComponentDef]]:
    component_types, codes = _structure_target_types_and_codes(result, salary_item)
    if not component_types or not codes:
        return []
    selection_date = _structure_selection_date_from_snapshot(result)
    statement = (
        select(EmployeeSalaryStructure, SalaryComponentDef)
        .join(SalaryComponentDef, SalaryComponentDef.id == EmployeeSalaryStructure.component_id)
        .where(
            EmployeeSalaryStructure.employee_id == result.employee_id,
            SalaryComponentDef.code.in_(codes),
            SalaryComponentDef.component_type.in_(component_types),
            SalaryComponentDef.is_deleted.is_(False),
            EmployeeSalaryStructure.effective_from <= selection_date,
            (EmployeeSalaryStructure.effective_to.is_(None))
            | (EmployeeSalaryStructure.effective_to > selection_date),
        )
        .order_by(SalaryComponentDef.code, EmployeeSalaryStructure.id)
    )
    if for_update:
        statement = statement.with_for_update()
    return [(row, component) for row, component in session.execute(statement).all()]


def _workflow_option(salary_item: str) -> dict[str, object]:
    if salary_item in _POLICY_LINE_CODES or salary_item.startswith(_POLICY_LINE_PREFIXES):
        return {
            "kind": "WORKFLOW",
            "label": "个税/社保专用来源流程",
            "workflow": "PAYROLL_POLICY_OR_TAX_OPENING",
            "reason": (
                "该项目涉及政策或累计计税来源，必须在专用来源流程核验后驳回或要求补充材料。"
            ),
        }
    if salary_item in _CARRY_LINE_CODES:
        return {
            "kind": "WORKFLOW",
            "label": "前序批次结转处理流程",
            "workflow": "PRIOR_BATCH_CARRY",
            "reason": "该项目来自前序锁定批次，必须先处理前序来源，再驳回或要求补充材料。",
        }
    return {
        "kind": "WORKFLOW",
        "label": "专用来源核验流程",
        "workflow": "MANUAL_SOURCE_REVIEW",
        "reason": "当前工资项没有可安全自动更正的单一来源；可要求补充材料或核验后驳回。",
    }


def dispute_correction_options(
    session: Session, result: PayrollResult, salary_item: str
) -> list[dict[str, object]]:
    """Describe only correction paths that can atomically update a real source."""

    attendance_fields = allowed_attendance_fields(result, salary_item)
    if attendance_fields:
        return [
            {
                "kind": "ATTENDANCE",
                "label": "考勤源数据",
                "fields": list(attendance_fields),
            }
        ]

    if salary_item == "HOLIDAY":
        snapshot = getattr(result, "input_snapshot", None)
        raw_dates = snapshot.get("statutory_holidays") if isinstance(snapshot, Mapping) else None
        if isinstance(raw_dates, list) and raw_dates:
            dates: list[dict[str, object]] = []
            for raw in raw_dates:
                if not isinstance(raw, Mapping):
                    return [_workflow_option(salary_item)]
                try:
                    entry = _holiday_snapshot_entry(raw)
                except BatchError:
                    return [_workflow_option(salary_item)]
                dates.append(
                    {
                        "holiday_date": str(entry["date"]),
                        "worked": bool(entry["worked"]),
                    }
                )
            return [
                {
                    "kind": "HOLIDAY_WORK",
                    "label": "法定节假日逐日出勤",
                    "holiday_dates": dates,
                }
            ]
        return [_workflow_option(salary_item)]

    if salary_item in {member.value for member in PayrollAdjustmentType}:
        adjustment_type = PayrollAdjustmentType(salary_item)
        record = session.scalars(
            select(MonthlyPayrollAdjustment).where(
                MonthlyPayrollAdjustment.employee_id == result.employee_id,
                MonthlyPayrollAdjustment.period
                == str(getattr(result, "input_snapshot", {}).get("period", "")),
                MonthlyPayrollAdjustment.adjustment_type == adjustment_type,
            )
        ).first()
        if record is not None:
            return [
                {
                    "kind": "MONTHLY_ADJUSTMENT",
                    "label": "月度补发/补扣来源",
                    "adjustment_type": adjustment_type.value,
                    "amount": str(record.amount),
                    "taxable": record.taxable,
                    "in_social_base": record.in_social_base,
                    "in_housing_base": record.in_housing_base,
                }
            ]
        return [_workflow_option(salary_item)]

    structure_rows = _snapshot_structure_rows(result)
    matching_performance = any(
        row.get("code") == salary_item
        and row.get("component_type") == ComponentType.PERFORMANCE.value
        for row in structure_rows
    )
    if matching_performance:
        period = str(getattr(result, "input_snapshot", {}).get("period", ""))
        performance = session.scalars(
            select(PerformanceRecord).where(
                PerformanceRecord.employee_id == result.employee_id,
                PerformanceRecord.period == period,
            )
        ).first()
        snapshot_coefficient = getattr(result, "input_snapshot", {}).get("performance_coefficient")
        return [
            {
                "kind": "PERFORMANCE",
                "label": "当月绩效记录",
                "coefficient": str(
                    performance.coefficient
                    if performance is not None
                    else snapshot_coefficient or "1.000"
                ),
                "score": (
                    str(performance.score)
                    if performance and performance.score is not None
                    else None
                ),
                "remark": performance.remark if performance else None,
            }
        ]

    source_rows = _structure_source_rows(session, result, salary_item)
    if source_rows and all(
        component.component_type
        in {ComponentType.ALLOWANCE, ComponentType.HOUSING, ComponentType.DEDUCTION}
        for _source, component in source_rows
    ):
        return [
            {
                "kind": "SALARY_STRUCTURE",
                "label": "受审计薪资结构",
                "components": [
                    {
                        "component_id": component.id,
                        "code": component.code,
                        "name": component.name,
                        "amount": str(source.amount),
                    }
                    for source, component in source_rows
                ],
            }
        ]
    return [_workflow_option(salary_item)]


def allowed_attendance_fields(result: PayrollResult, salary_item: str) -> tuple[str, ...]:
    """Return only source fields consumed by this immutable calculation path.

    Attendance wage inputs differ by rule version, employment type, department,
    and special-position status.  Those facts must come from the payroll result
    snapshot, never from mutable current employee master data.  An incomplete or
    unknown historical path fails closed rather than exposing a broader editor.
    """

    rule_version = getattr(result, "rule_version", None)
    if rule_version not in _SUPPORTED_RECOMPUTE_RULE_VERSIONS:
        return ()
    if salary_item == "OVERTIME":
        return ("overtime_hours",)
    if salary_item != "ATTEND_WAGE":
        return ()

    snapshot = getattr(result, "input_snapshot", None)
    if not isinstance(snapshot, Mapping):
        return ()
    department = snapshot.get("department")
    is_special = snapshot.get("is_special_position")
    if department not in {member.value for member in Department} or not isinstance(
        is_special, bool
    ):
        return ()

    if rule_version == "v4":
        employment_type = snapshot.get("employment_type")
        if employment_type not in {member.value for member in EmploymentType}:
            return ()
        if is_special:
            return ("actual_days", "expected_days")
        if employment_type == EmploymentType.PART_TIME_HOURLY.value:
            return ("worked_hours",)
        if department in {Department.DINING.value, Department.KITCHEN.value}:
            return ("expected_days", "worked_hours")
        if department == Department.OTHER.value:
            return ("actual_days", "expected_days")
        return ()

    if rule_version == "v3":
        if is_special:
            return ("expected_days", "rest_days")
        if department in {Department.DINING.value, Department.KITCHEN.value}:
            return ("expected_days", "worked_hours")
        if department == Department.OTHER.value:
            return ("actual_days", "expected_days")
        return ()

    if rule_version == "v2":
        if is_special or department == Department.OTHER.value:
            return ("expected_days", "rest_days")
        if department in {Department.DINING.value, Department.KITCHEN.value}:
            return ("expected_days", "worked_hours")
    return ()


def _validate_dispute_item_attendance_fields(
    result: PayrollResult | None,
    salary_item: str,
    changes: Mapping[str, object] | None,
) -> None:
    if salary_item not in _ATTENDANCE_DISPUTE_ITEM_CODES:
        raise BatchError(
            "This salary item requires its dedicated source-data correction workflow before "
            "the dispute can be approved."
        )
    item_fields = _ATTENDANCE_DISPUTE_MAX_FIELDS[salary_item]
    unsupported_item_fields = set(changes or ()).difference(item_fields)
    if unsupported_item_fields:
        field_names = ", ".join(sorted(unsupported_item_fields))
        raise BatchError(
            f"Salary item {salary_item} cannot correct attendance fields: {field_names}."
        )
    if result is None:
        return
    allowed_fields = frozenset(allowed_attendance_fields(result, salary_item))
    if not allowed_fields:
        raise BatchError(
            "The historical payroll result has no supported attendance correction path."
        )
    unsupported = set(changes or ()).difference(allowed_fields)
    if unsupported:
        field_names = ", ".join(sorted(unsupported))
        raise BatchError(
            f"Salary item {salary_item} does not use attendance fields on this calculation "
            f"path: {field_names}."
        )


def _disputed_calculation_changed(
    prior_result: PayrollResult, result: EngineResult, salary_item: str
) -> bool:
    """Require an approval to change the disputed output, not merely any source cell."""

    before_amount = _result_line_amount(prior_result.lines, salary_item)
    after_amount = _result_line_amount(_lines_json(result), salary_item)
    if before_amount != after_amount:
        return True
    if salary_item == "ATTEND_WAGE":
        return Decimal(str(prior_result.actual_attendance_days)) != result.actual_attendance_days
    return False


def _attendance_values(attendance: AttendanceRecord) -> dict[str, str | None]:
    return {
        "expected_days": str(attendance.expected_days),
        "expected_days_adjust_reason": getattr(attendance, "expected_days_adjust_reason", None),
        "actual_days": str(attendance.actual_days),
        "worked_hours": (
            str(attendance.worked_hours) if attendance.worked_hours is not None else None
        ),
        "rest_days": str(attendance.rest_days),
        "overtime_hours": str(attendance.overtime_hours),
        "holiday_worked_days": str(attendance.holiday_worked_days),
    }


def _validated_attendance_changes(
    attendance: AttendanceRecord, changes: Mapping[str, object] | None
) -> dict[str, Decimal]:
    if not isinstance(changes, Mapping) or not changes:
        raise BatchError("Approved disputes require at least one attendance source-data change.")
    unsupported = set(changes).difference(_ATTENDANCE_CHANGE_LIMITS)
    if unsupported:
        field_names = ", ".join(sorted(unsupported))
        raise BatchError(f"Unsupported attendance adjustment fields: {field_names}.")

    normalized: dict[str, Decimal] = {}
    for field, raw_value in changes.items():
        try:
            value = Decimal(str(raw_value))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BatchError(f"Invalid decimal value for attendance field {field}.") from exc
        if not value.is_finite():
            raise BatchError(f"Attendance field {field} must be finite.")
        if field == "expected_days":
            if value <= 0:
                raise BatchError("expected_days must be greater than zero for payroll computation.")
        elif value < 0:
            raise BatchError(f"Attendance field {field} cannot be negative.")
        if value > _ATTENDANCE_CHANGE_LIMITS[field]:
            raise BatchError(f"Attendance field {field} exceeds its allowed maximum.")
        try:
            canonical_value = value.quantize(_ATTENDANCE_DECIMAL_QUANTUM)
        except InvalidOperation as exc:
            raise BatchError(
                f"Attendance field {field} cannot be stored with two decimal places."
            ) from exc
        if canonical_value != value:
            raise BatchError(f"Attendance field {field} cannot have more than two decimal places.")
        normalized[field] = canonical_value

    no_source_values_changed = all(
        getattr(attendance, field) is not None and Decimal(str(getattr(attendance, field))) == value
        for field, value in normalized.items()
    )
    if no_source_values_changed:
        raise BatchError("Approved disputes must change at least one attendance source value.")
    return normalized


@dataclass(frozen=True)
class _SourceCorrectionPlan:
    result: EngineResult
    input_snapshot: dict
    before: dict[str, object]
    after: dict[str, object]
    apply: Callable[[], None]


def _recompute_source_snapshot(
    prior_result: PayrollResult, snapshot: Mapping[str, object]
) -> tuple[EngineResult, dict]:
    return _recompute_result_from_snapshot(
        prior_result,
        {},
        source_snapshot=snapshot,
    )


def _holiday_correction_plan(
    session: Session,
    *,
    batch: PayrollBatch,
    prior_result: PayrollResult,
    payload: Mapping[str, object],
    resolution: str,
    attachment_url: str,
    approver_id: int,
) -> _SourceCorrectionPlan:
    try:
        holiday_date = date.fromisoformat(str(payload["holiday_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise BatchError("A holiday-work correction requires a valid holiday_date.") from exc
    worked = payload.get("worked")
    if not isinstance(worked, bool):
        raise BatchError("A holiday-work correction requires a boolean worked value.")
    if f"{holiday_date.year:04d}-{holiday_date.month:02d}" != batch.period:
        raise BatchError("The holiday-work correction date is outside the payroll period.")

    snapshot = _source_snapshot_copy(prior_result)
    holidays = snapshot.get("statutory_holidays")
    if not isinstance(holidays, list):
        raise BatchError("The historical payroll result has no daily holiday source path.")
    target = holiday_date.isoformat()
    proposed_holidays: list[dict[str, object]] = []
    before_worked: bool | None = None
    for raw in holidays:
        if not isinstance(raw, Mapping):
            raise BatchError("The historical payroll result has invalid holiday source data.")
        entry = _holiday_snapshot_entry(raw)
        if entry["date"] == target:
            before_worked = bool(entry["worked"])
            entry["worked"] = worked
        proposed_holidays.append(entry)
    if before_worked is None:
        raise BatchError("The selected date is not an eligible holiday in this payroll result.")
    if before_worked == worked:
        raise BatchError("Approved disputes must change the holiday-work source value.")

    source = session.scalars(
        select(HolidayWorkRecord)
        .where(
            HolidayWorkRecord.employee_id == prior_result.employee_id,
            HolidayWorkRecord.holiday_date == holiday_date,
        )
        .with_for_update()
    ).first()
    persisted_worked = source.worked if source is not None else False
    if persisted_worked != before_worked:
        raise BatchError("The holiday-work source changed after payroll calculation; reload first.")
    snapshot["statutory_holidays"] = proposed_holidays
    prospective, input_snapshot = _recompute_source_snapshot(prior_result, snapshot)
    before = {
        "record_exists": source is not None,
        "holiday_date": target,
        "worked": before_worked,
        "reason": source.reason if source is not None else None,
        "evidence_url": source.evidence_url if source is not None else None,
    }
    after = {
        "record_exists": True,
        "holiday_date": target,
        "worked": worked,
        "reason": resolution,
        "evidence_url": attachment_url,
    }

    def apply() -> None:
        nonlocal source
        if source is None:
            source = HolidayWorkRecord(
                employee_id=prior_result.employee_id,
                org_unit_id=prior_result.org_unit_id,
                holiday_date=holiday_date,
            )
            session.add(source)
        source.worked = worked
        source.reason = resolution
        source.evidence_url = attachment_url
        source.recorded_by = approver_id
        source.recorded_at = _now(session)

    return _SourceCorrectionPlan(prospective, input_snapshot, before, after, apply)


def _performance_correction_plan(
    session: Session,
    *,
    batch: PayrollBatch,
    prior_result: PayrollResult,
    payload: Mapping[str, object],
) -> _SourceCorrectionPlan:
    try:
        coefficient = Decimal(str(payload["coefficient"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise BatchError("A performance correction requires a valid coefficient.") from exc
    if not coefficient.is_finite() or coefficient < 0 or coefficient > Decimal("5"):
        raise BatchError("Performance coefficient must be between 0 and 5.")
    if _decimal_scale(coefficient, "Performance coefficient") > 3:
        raise BatchError("Performance coefficient cannot have more than three decimal places.")
    coefficient = coefficient.quantize(Decimal("0.001"))
    raw_score = payload.get("score")
    score: Decimal | None
    if raw_score is None:
        score = None
    else:
        try:
            score = Decimal(str(raw_score))
        except (InvalidOperation, TypeError, ValueError) as exc:
            raise BatchError("Performance score is invalid.") from exc
        if (
            not score.is_finite()
            or score < 0
            or score > 100
            or _decimal_scale(score, "Performance score") > 2
        ):
            raise BatchError("Performance score must be between 0 and 100 with two decimals.")
        score = score.quantize(Decimal("0.01"))
    remark_value = payload.get("remark")
    if remark_value is not None and not isinstance(remark_value, str):
        raise BatchError("Performance remark must be text.")
    remark = remark_value.strip() or None if isinstance(remark_value, str) else None
    if remark is not None and len(remark) > 255:
        raise BatchError("Performance remark cannot exceed 255 characters.")

    source = session.scalars(
        select(PerformanceRecord)
        .where(
            PerformanceRecord.employee_id == prior_result.employee_id,
            PerformanceRecord.period == batch.period,
        )
        .with_for_update()
    ).first()
    snapshot = _source_snapshot_copy(prior_result)
    snapshot_before = snapshot.get("performance_coefficient")
    persisted_before = source.coefficient if source is not None else Decimal("1.000")
    if snapshot_before is not None and Decimal(str(snapshot_before)) != persisted_before:
        raise BatchError("The performance source changed after payroll calculation; reload first.")
    before = {
        "record_exists": source is not None,
        "coefficient": str(persisted_before),
        "score": str(source.score) if source is not None and source.score is not None else None,
        "remark": source.remark if source is not None else None,
    }
    after = {
        "record_exists": True,
        "coefficient": str(coefficient),
        "score": str(score) if score is not None else None,
        "remark": remark,
    }
    if before == after:
        raise BatchError("Approved disputes must change the performance source.")
    snapshot["performance_coefficient"] = str(coefficient)
    prospective, input_snapshot = _recompute_source_snapshot(prior_result, snapshot)

    def apply() -> None:
        nonlocal source
        if source is None:
            source = PerformanceRecord(
                employee_id=prior_result.employee_id,
                period=batch.period,
            )
            session.add(source)
        source.coefficient = coefficient
        source.score = score
        source.remark = remark

    return _SourceCorrectionPlan(prospective, input_snapshot, before, after, apply)


def _append_monthly_adjustment_revision(
    session: Session,
    source: MonthlyPayrollAdjustment,
    *,
    changed_by: int,
) -> None:
    latest = session.scalar(
        select(func.max(MonthlyPayrollAdjustmentRevision.revision)).where(
            MonthlyPayrollAdjustmentRevision.adjustment_id == source.id
        )
    )
    session.add(
        MonthlyPayrollAdjustmentRevision(
            adjustment_id=source.id,
            revision=int(latest or 0) + 1,
            employee_id=source.employee_id,
            org_unit_id=source.org_unit_id,
            period=source.period,
            adjustment_type=source.adjustment_type,
            amount=source.amount,
            reason=source.reason,
            attachment_url=source.attachment_url,
            taxable=source.taxable,
            in_social_base=source.in_social_base,
            in_housing_base=source.in_housing_base,
            changed_by=changed_by,
        )
    )


def _monthly_adjustment_correction_plan(
    session: Session,
    *,
    batch: PayrollBatch,
    prior_result: PayrollResult,
    salary_item: str,
    payload: Mapping[str, object],
    resolution: str,
    attachment_url: str,
    approver_id: int,
) -> _SourceCorrectionPlan:
    try:
        adjustment_type = PayrollAdjustmentType(salary_item)
    except ValueError as exc:
        raise BatchError("This line is not backed by a monthly payroll adjustment.") from exc
    try:
        amount = Decimal(str(payload["amount"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise BatchError("A monthly adjustment correction requires a valid amount.") from exc
    if (
        not amount.is_finite()
        or amount <= 0
        or _decimal_scale(amount, "Monthly adjustment amount") > 2
        or amount >= Decimal("1000000000000")
    ):
        raise BatchError("Monthly adjustment amount must be positive with at most two decimals.")
    amount = amount.quantize(Decimal("0.01"))
    classification_fields = ("taxable", "in_social_base", "in_housing_base")
    if any(not isinstance(payload.get(field), bool) for field in classification_fields):
        raise BatchError("Monthly adjustment classification requires all three boolean flags.")
    classification: dict[str, bool] = {
        field: bool(payload[field]) for field in classification_fields
    }
    source = session.scalars(
        select(MonthlyPayrollAdjustment)
        .where(
            MonthlyPayrollAdjustment.employee_id == prior_result.employee_id,
            MonthlyPayrollAdjustment.period == batch.period,
            MonthlyPayrollAdjustment.adjustment_type == adjustment_type,
        )
        .with_for_update()
    ).first()
    if source is None:
        raise BatchError(
            "The monthly adjustment source no longer exists; use its dedicated workflow."
        )
    before: dict[str, object] = {
        "amount": str(source.amount),
        "reason": source.reason,
        "attachment_url": source.attachment_url,
        "taxable": source.taxable,
        "in_social_base": source.in_social_base,
        "in_housing_base": source.in_housing_base,
    }
    after: dict[str, object] = {
        "amount": str(amount),
        "reason": resolution,
        "attachment_url": attachment_url,
        **classification,
    }
    if (
        source.amount == amount
        and source.taxable == classification["taxable"]
        and source.in_social_base == classification["in_social_base"]
        and source.in_housing_base == classification["in_housing_base"]
    ):
        raise BatchError("Approved disputes must change the monthly adjustment source.")
    snapshot = _source_snapshot_copy(prior_result)
    prefix = (
        "prev_makeup" if adjustment_type is PayrollAdjustmentType.PREV_MAKEUP else "prev_deduct"
    )
    snapshot[prefix] = str(amount)
    snapshot[f"{prefix}_taxable"] = classification["taxable"]
    snapshot[f"{prefix}_in_social_base"] = classification["in_social_base"]
    snapshot[f"{prefix}_in_housing_base"] = classification["in_housing_base"]
    prospective, input_snapshot = _recompute_source_snapshot(prior_result, snapshot)

    def apply() -> None:
        source.amount = amount
        source.reason = resolution
        source.attachment_url = attachment_url
        source.taxable = classification["taxable"]
        source.in_social_base = classification["in_social_base"]
        source.in_housing_base = classification["in_housing_base"]
        source.updated_by = approver_id
        _append_monthly_adjustment_revision(session, source, changed_by=approver_id)

    return _SourceCorrectionPlan(prospective, input_snapshot, before, after, apply)


def _structure_correction_plan(
    session: Session,
    *,
    batch: PayrollBatch,
    prior_result: PayrollResult,
    salary_item: str,
    payload: Mapping[str, object],
    resolution: str,
    attachment_url: str,
) -> _SourceCorrectionPlan:
    raw_component_id = payload.get("component_id")
    if isinstance(raw_component_id, bool):
        raise BatchError("A salary-structure correction requires a component_id.")
    try:
        component_id = int(str(raw_component_id))
        amount = Decimal(str(payload["amount"]))
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise BatchError("A salary-structure correction requires a component and amount.") from exc
    if component_id <= 0:
        raise BatchError("Salary component id must be positive.")
    if (
        not amount.is_finite()
        or amount < 0
        or _decimal_scale(amount, "Salary component amount") > 2
        or amount >= Decimal("1000000000000")
    ):
        raise BatchError("Salary component amount must be non-negative with at most two decimals.")
    amount = amount.quantize(Decimal("0.01"))
    source_rows = _structure_source_rows(
        session,
        prior_result,
        salary_item,
        for_update=True,
    )
    selected = next(
        ((source, component) for source, component in source_rows if component.id == component_id),
        None,
    )
    if selected is None:
        raise BatchError("The selected salary component does not back the disputed payroll line.")
    source, component = selected
    if component.component_type not in {
        ComponentType.ALLOWANCE,
        ComponentType.HOUSING,
        ComponentType.DEDUCTION,
    }:
        raise BatchError("This salary component requires its dedicated source workflow.")
    if source.amount == amount:
        raise BatchError("Approved disputes must change the salary-structure source amount.")
    if (
        session.scalar(
            select(PayrollBatch.id)
            .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
            .where(
                PayrollBatch.period > batch.period,
                PayrollResult.employee_id == prior_result.employee_id,
            )
            .limit(1)
        )
        is not None
    ):
        raise BatchError(
            "A later payroll result depends on this structure; "
            "use the dedicated versioned workflow."
        )
    selection_date = _structure_selection_date_from_snapshot(prior_result)
    snapshot = _source_snapshot_copy(prior_result)
    raw_structure = snapshot.get("structure")
    if not isinstance(raw_structure, list):
        raise BatchError("The historical payroll result has no salary structure snapshot.")
    found = False
    proposed_structure: list[object] = []
    for raw in raw_structure:
        if not isinstance(raw, Mapping):
            raise BatchError("The historical payroll result has invalid salary structure data.")
        entry = dict(raw)
        if entry.get("code") == component.code:
            if Decimal(str(entry.get("amount"))) != source.amount:
                raise BatchError("The salary-structure source changed after payroll calculation.")
            entry["amount"] = str(amount)
            found = True
        proposed_structure.append(entry)
    if not found:
        raise BatchError("The selected salary component is absent from the payroll snapshot.")
    snapshot["structure"] = proposed_structure
    prospective, input_snapshot = _recompute_source_snapshot(prior_result, snapshot)
    before: dict[str, object] = {
        "component_id": component.id,
        "code": component.code,
        "amount": str(source.amount),
        "effective_from": source.effective_from.isoformat(),
        "structure_id": source.id,
    }
    after: dict[str, object] = {
        "component_id": component.id,
        "code": component.code,
        "amount": str(amount),
        "effective_from": selection_date.isoformat(),
    }

    def apply() -> None:
        try:
            set_component_amount(
                session,
                employee_id=prior_result.employee_id,
                component_id=component.id,
                amount=amount,
                effective_from=selection_date,
                source_reason=resolution,
                source_attachment_url=attachment_url,
            )
        except StructureError as exc:
            raise BatchError(str(exc)) from exc

    return _SourceCorrectionPlan(prospective, input_snapshot, before, after, apply)


def _source_correction_plan(
    session: Session,
    *,
    batch: PayrollBatch,
    prior_result: PayrollResult,
    salary_item: str,
    payload: Mapping[str, object],
    resolution: str,
    attachment_url: str,
    approver_id: int,
) -> _SourceCorrectionPlan:
    kind = payload.get("kind")
    options = dispute_correction_options(session, prior_result, salary_item)
    allowed_kinds = {
        str(option.get("kind")) for option in options if option.get("kind") != "WORKFLOW"
    }
    if kind not in allowed_kinds:
        raise BatchError(
            "The selected source correction does not safely map to the disputed payroll item."
        )
    if kind == "HOLIDAY_WORK":
        return _holiday_correction_plan(
            session,
            batch=batch,
            prior_result=prior_result,
            payload=payload,
            resolution=resolution,
            attachment_url=attachment_url,
            approver_id=approver_id,
        )
    if kind == "PERFORMANCE":
        return _performance_correction_plan(
            session,
            batch=batch,
            prior_result=prior_result,
            payload=payload,
        )
    if kind == "MONTHLY_ADJUSTMENT":
        return _monthly_adjustment_correction_plan(
            session,
            batch=batch,
            prior_result=prior_result,
            salary_item=salary_item,
            payload=payload,
            resolution=resolution,
            attachment_url=attachment_url,
            approver_id=approver_id,
        )
    if kind == "SALARY_STRUCTURE":
        return _structure_correction_plan(
            session,
            batch=batch,
            prior_result=prior_result,
            salary_item=salary_item,
            payload=payload,
            resolution=resolution,
            attachment_url=attachment_url,
        )
    raise BatchError("Unsupported source correction kind.")


def resolve_dispute(
    session: Session,
    dispute: CompDispute,
    *,
    decision: DisputeStatus,
    resolution: str,
    approver_id: int,
    attendance_changes: dict | None = None,
    source_correction: Mapping[str, object] | None = None,
    attachment_url: str | None = None,
) -> CompDispute:
    """人事处理异议。APPROVED 时改源数据(考勤)并自动重算，生成修改记录。"""
    if decision not in (DisputeStatus.APPROVED, DisputeStatus.REJECTED, DisputeStatus.NEED_MORE):
        raise BatchError("非法的处理结论")
    lock_payroll_input_mutation(session)
    batch = session.get(PayrollBatch, dispute.batch_id)
    if batch is None:
        raise BatchError("批次不存在")

    _lock_batch(session, batch)
    # Acquire the dispute row after the batch lock, matching the lock order used
    # by every other batch transition.  Checking a stale dispute object here would
    # allow two HR users to resolve the same dispute concurrently.
    session.refresh(dispute, with_for_update=True)
    if getattr(dispute, "batch_version", batch.version) != batch.version:
        raise BatchError("The dispute belongs to a historical payroll review round.")
    if dispute.status not in (DisputeStatus.OPEN, DisputeStatus.NEED_MORE):
        raise BatchError("异议已处理")
    if batch.status == BatchStatus.LOCKED:
        raise BatchError("批次已锁定")
    _require_batch_state(batch, allowed=_RESOLVABLE_BATCH_STATES, action="resolve a dispute")

    if decision == DisputeStatus.APPROVED and not attachment_url:
        raise BatchError("Approved source-data corrections require a proof attachment.")
    if decision != DisputeStatus.APPROVED and (
        attendance_changes is not None or source_correction is not None
    ):
        raise BatchError("Only approved disputes may include source-data corrections.")
    if attendance_changes is not None and source_correction is not None:
        raise BatchError("Choose exactly one source-data correction payload.")

    def _close(status: DisputeStatus) -> None:
        dispute.status = status
        dispute.resolution = resolution
        dispute.resolved_by = approver_id
        dispute.resolved_at = _now(session)
        session.add(
            DisputeEvent(
                dispute_id=dispute.id,
                event_type=status.value,
                note=resolution,
                actor_id=approver_id,
                attachment_url=attachment_url,
            )
        )

    # NEED_MORE：补充材料，异议仍未决，批次保持 HAS_DISPUTE
    if decision == DisputeStatus.NEED_MORE:
        _close(DisputeStatus.NEED_MORE)
        session.flush()
        return dispute

    # REJECTED：驳回，不改源数据；仅关闭异议
    if decision == DisputeStatus.REJECTED:
        result = _latest_result(
            session,
            batch.id,
            dispute.employee_id,
            batch_version=batch.version,
            for_update=True,
        )
        if result is None:
            raise BatchError("Cannot reset review state without a payroll result.")
        _close(DisputeStatus.REJECTED)
        session.flush()
        _reopen_if_settled(session, batch)
        return dispute

    # APPROVED：改源数据 → 自动重算 → 修改记录（规格 8.2：禁止只改最终金额）
    att: AttendanceRecord | None = None
    if source_correction is None:
        _validate_dispute_item_attendance_fields(None, dispute.salary_item, attendance_changes)
    emp = session.get(Employee, dispute.employee_id)
    if emp is None:
        raise BatchError("员工不存在")
    if source_correction is None:
        att = session.scalars(
            select(AttendanceRecord)
            .where(
                AttendanceRecord.employee_id == emp.id,
                AttendanceRecord.period == batch.period,
            )
            .with_for_update()
        ).first()
        if att is None:
            raise BatchError("无考勤记录可调整")

    prior_result = _latest_result(
        session,
        batch.id,
        emp.id,
        batch_version=batch.version,
        for_update=True,
    )
    if prior_result is None:
        raise BatchError("Cannot approve a dispute without an existing payroll result.")
    if source_correction is not None:
        plan = _source_correction_plan(
            session,
            batch=batch,
            prior_result=prior_result,
            salary_item=dispute.salary_item,
            payload=source_correction,
            resolution=resolution,
            attachment_url=attachment_url or "",
            approver_id=approver_id,
        )
    else:
        if att is None:
            raise BatchError("无考勤记录可调整")
        _validate_dispute_item_attendance_fields(
            prior_result,
            dispute.salary_item,
            attendance_changes,
        )
        validated_changes = _validated_attendance_changes(att, attendance_changes)
        prospective_result, input_snapshot = _recompute_result_from_snapshot(
            prior_result,
            validated_changes,
        )
        before: dict[str, object] = dict(_attendance_values(att))
        after: dict[str, object] = dict(before)
        after.update({field: str(value) for field, value in validated_changes.items()})
        if "expected_days" in validated_changes:
            after["expected_days_adjust_reason"] = resolution

        def apply_attendance() -> None:
            for field, value in validated_changes.items():
                setattr(att, field, value)
            if "expected_days" in validated_changes:
                att.expected_days_adjust_reason = resolution

        plan = _SourceCorrectionPlan(
            prospective_result,
            input_snapshot,
            before,
            after,
            apply_attendance,
        )

    if not _disputed_calculation_changed(prior_result, plan.result, dispute.salary_item):
        raise BatchError(
            "Approved corrections must change the disputed payroll calculation result."
        )
    plan.apply()
    session.flush()

    new_result = _write_result(
        session,
        batch,
        emp,
        plan.result,
        version=prior_result.version + 1,
        input_snapshot=plan.input_snapshot,
        scope=(prior_result.org_unit_id, prior_result.department),
        identity_snapshot=prior_result,
    )
    session.add(
        AdjustmentRecord(
            batch_id=batch.id,
            batch_version=batch.version,
            employee_id=emp.id,
            dispute_id=dispute.id,
            item=dispute.salary_item,
            before_value=plan.before,
            after_value=plan.after,
            reason=resolution,
            applicant_id=dispute.raised_by,
            approver_id=approver_id,
            attachment_url=attachment_url,
            recompute_result=_result_audit_snapshot(new_result),
        )
    )
    _close(DisputeStatus.APPROVED)
    session.flush()
    _reopen_if_settled(session, batch)
    return dispute


def supplement_dispute(
    session: Session,
    dispute: CompDispute,
    *,
    note: str,
    attachment_url: str,
    actor_id: int,
) -> CompDispute:
    """Append requested evidence and return the dispute to HR's open queue."""
    batch = session.get(PayrollBatch, dispute.batch_id)
    if batch is None:
        raise BatchError("批次不存在")
    _lock_batch(session, batch)
    session.refresh(dispute, with_for_update=True)
    if getattr(dispute, "batch_version", batch.version) != batch.version:
        raise BatchError("The dispute belongs to a historical payroll review round.")
    if batch.status == BatchStatus.LOCKED:
        raise BatchError("批次已锁定")
    _require_batch_state(batch, allowed=_RESOLVABLE_BATCH_STATES, action="supplement a dispute")
    if dispute.status != DisputeStatus.NEED_MORE:
        raise BatchError("Only a dispute awaiting more material can accept a supplement.")
    dispute.status = DisputeStatus.OPEN
    dispute.resolution = None
    dispute.resolved_by = None
    dispute.resolved_at = None
    session.add(
        DisputeEvent(
            dispute_id=dispute.id,
            event_type=DisputeEventType.SUPPLEMENTED.value,
            note=note,
            actor_id=actor_id,
            attachment_url=attachment_url,
        )
    )
    session.flush()
    return dispute


def _open_dispute_count(session: Session, batch: PayrollBatch) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(CompDispute)
            .where(
                CompDispute.batch_id == batch.id,
                CompDispute.batch_version == batch.version,
                CompDispute.status.in_([DisputeStatus.OPEN, DisputeStatus.NEED_MORE]),
            )
        )
        or 0
    )


def _reopen_if_settled(session: Session, batch: PayrollBatch) -> None:
    """异议全部处理完 → 回到待门店确认；受影响门店/部门须重新确认（规格 8.2）。"""
    if _open_dispute_count(session, batch) == 0:
        batch.status = BatchStatus.PENDING_STORE_CONFIRM
        for confirmation in session.scalars(
            select(BatchConfirmation)
            .where(
                BatchConfirmation.batch_id == batch.id,
                BatchConfirmation.batch_version == batch.version,
                BatchConfirmation.status == ConfirmStatus.DISPUTED,
            )
            .with_for_update()
        ).all():
            confirmation.status = ConfirmStatus.PENDING
            confirmation.confirmed_by = None
            confirmation.confirmed_at = None
        session.flush()


def _lock_batch_results(session: Session, batch: PayrollBatch) -> None:
    session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch.id,
            PayrollResult.batch_version == batch.version,
        )
        .with_for_update()
    ).all()


def _lock_batch_confirmations(session: Session, batch: PayrollBatch) -> None:
    session.scalars(
        select(BatchConfirmation)
        .where(
            BatchConfirmation.batch_id == batch.id,
            BatchConfirmation.batch_version == batch.version,
        )
        .with_for_update()
    ).all()


def _result_count(session: Session, batch: PayrollBatch) -> int:
    return (
        session.scalar(
            select(func.count())
            .select_from(PayrollResult)
            .where(
                PayrollResult.batch_id == batch.id,
                PayrollResult.batch_version == batch.version,
            )
        )
        or 0
    )


def _latest_error_result_count(session: Session, batch: PayrollBatch) -> int:
    result = aliased(PayrollResult)
    newer_result = aliased(PayrollResult)
    has_newer_result = (
        select(newer_result.id)
        .where(
            newer_result.batch_id == result.batch_id,
            newer_result.batch_version == result.batch_version,
            newer_result.employee_id == result.employee_id,
            newer_result.version > result.version,
        )
        .exists()
    )
    return (
        session.scalar(
            select(func.count())
            .select_from(result)
            .where(
                result.batch_id == batch.id,
                result.batch_version == batch.version,
                result.has_error.is_(True),
                ~has_newer_result,
            )
        )
        or 0
    )


def lock_batch(session: Session, batch: PayrollBatch, user_id: int) -> None:
    """全部确认且无未处理异议后锁定。"""
    _lock_batch(session, batch)
    if batch.status == BatchStatus.LOCKED:
        raise BatchError("批次已锁定")
    _require_batch_state(batch, allowed=_LOCKABLE_BATCH_STATES, action="lock a batch")
    _assert_sequential_batch_progress(session, batch)
    _lock_batch_results(session, batch)
    _lock_batch_confirmations(session, batch)
    if _result_count(session, batch) == 0:
        raise BatchError("Cannot lock a batch without payroll results.")
    if _latest_error_result_count(session, batch):
        raise BatchError("Cannot lock a batch with errored payroll results.")
    if _open_dispute_count(session, batch):
        raise BatchError("存在未处理异议，无法锁定")
    pending = session.scalar(
        select(func.count())
        .select_from(BatchConfirmation)
        .where(
            BatchConfirmation.batch_id == batch.id,
            BatchConfirmation.batch_version == batch.version,
            BatchConfirmation.status != ConfirmStatus.CONFIRMED,
        )
    )
    if pending:
        raise BatchError("存在未确认的门店/部门，无法锁定")
    batch.status = BatchStatus.LOCKED
    batch.locked_at = _now(session)
    batch.locked_by = user_id
    session.flush()


def _has_started_later_batch(session: Session, batch: PayrollBatch) -> bool:
    """Return whether a later period has a calculation snapshot to protect.

    Carry-forward obligations are consumed from the latest locked result.  If
    an earlier period is reopened after a later review round has started, a
    correction can silently invalidate that later snapshot and lose (or
    duplicate) a cross-period obligation.  Operators must reopen affected
    periods from newest to oldest before correcting the earlier source.
    """
    if not batch.period:
        # A persisted PayrollBatch always has a period; this keeps lightweight
        # state-machine unit doubles focused on the transition under test.
        return False
    return (
        session.scalar(
            select(PayrollBatch.id)
            .where(
                PayrollBatch.period > batch.period,
                PayrollBatch.status != BatchStatus.DRAFT,
            )
            .limit(1)
        )
        is not None
    )


def _has_unsettled_prior_batch(session: Session, batch: PayrollBatch) -> bool:
    """Return whether an earlier review round must be settled first.

    A later payroll run only carries forward obligations from locked results.
    Starting a later period while an earlier calculated (or reopened) period is
    unfinished would therefore either omit its obligations or make a later
    rerun consume them twice.  An untouched v1 draft has no calculation
    snapshot and is intentionally not treated as a prerequisite.
    """
    if not batch.period:
        # Persisted batches always have a period.  Keep state-machine unit
        # doubles focused on the transition under test.
        return False
    return (
        session.scalar(
            select(PayrollBatch.id)
            .where(
                PayrollBatch.period < batch.period,
                or_(
                    PayrollBatch.status.notin_((BatchStatus.LOCKED, BatchStatus.DRAFT)),
                    (PayrollBatch.status == BatchStatus.DRAFT) & (PayrollBatch.version > 1),
                ),
            )
            .limit(1)
        )
        is not None
    )


def _assert_sequential_batch_progress(session: Session, batch: PayrollBatch) -> None:
    """Keep cross-period carry obligations in one settled chronological chain."""
    if _has_started_later_batch(session, batch):
        raise BatchError(
            "A later payroll batch has started; reopen later periods from newest to oldest first."
        )
    if _has_unsettled_prior_batch(session, batch):
        raise BatchError(
            "An earlier payroll batch has not been locked; settle it before starting this period."
        )


def unlock_batch(session: Session, batch: PayrollBatch, user_id: int, reason: str) -> None:
    """解锁：version+1，回到待确认；保留旧结果版本不覆盖（规格 8.4）。"""
    # A reopened historical batch changes YTD facts read by later batch runs.
    # Serialize this transition with source writes and run_batch before taking
    # the batch row lock, so no later snapshot can see superseded history.
    lock_payroll_input_mutation(session)
    _lock_batch(session, batch)
    if batch.status != BatchStatus.LOCKED:
        raise BatchError("仅锁定批次可解锁")
    if not reason.strip():
        raise BatchError("解锁须填写原因")
    if _open_dispute_count(session, batch):
        raise BatchError("Resolve all open disputes before unlocking the batch.")
    if _has_started_later_batch(session, batch):
        raise BatchError(
            "A later payroll batch has started; reopen later periods from newest to oldest first."
        )
    batch.version += 1
    batch.status = BatchStatus.DRAFT
    batch.calculated_at = None
    batch.hr_reviewed_by = None
    batch.hr_reviewed_at = None
    batch.locked_at = None
    batch.locked_by = None
    # 解锁后必须重新分发并确认，不能沿用锁定前的确认结论。
    session.flush()


def reopen_batch(session: Session, batch: PayrollBatch, user_id: int, reason: str) -> None:
    """Return an unlocked, non-draft batch to an HR correction draft round.

    This is the recovery path for result/input errors that arise before a batch
    can be locked.  Historical results, confirmations, and settled disputes are
    retained under their previous ``batch_version``; the next run creates an
    independent round.
    """
    lock_payroll_input_mutation(session)
    _lock_batch(session, batch)
    if batch.status in {BatchStatus.DRAFT, BatchStatus.CALCULATING, BatchStatus.LOCKED}:
        raise BatchError("Only a completed, unlocked review round can be reopened for correction.")
    if not reason.strip():
        raise BatchError("A correction reason is required.")
    if _open_dispute_count(session, batch):
        raise BatchError("Resolve all open disputes before reopening the batch.")
    if _has_started_later_batch(session, batch):
        raise BatchError(
            "A later payroll batch has started; reopen later periods from newest to oldest first."
        )
    _ = user_id
    batch.version += 1
    batch.status = BatchStatus.DRAFT
    batch.calculated_at = None
    batch.hr_reviewed_by = None
    batch.hr_reviewed_at = None
    batch.locked_at = None
    batch.locked_by = None
    session.flush()
