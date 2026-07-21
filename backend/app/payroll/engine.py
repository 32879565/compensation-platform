"""薪资计算规则引擎 v4（按业务规格 docs/payroll-batch-spec.md 的真实公式）。

不变量：全程 Decimal、逐项 quantize(0.01, ROUND_HALF_UP)、确定性、逐项可追溯、
缺输入进 exceptions 且 has_error 阻断出账。策略参数集中在 RuleConfig（版本化 v4）。

核心公式：
- 实际计薪出勤天数（两套标准，无最低工时，允许小数）：
  · 非特殊岗位·厅面 = 出勤工时 ÷ 9；厨房 = 出勤工时 ÷ 9.5
  · 特殊岗位 / 其他部门 = 应出勤天数 − 休息天数
  · 上限：不超过当月天数
- 出勤工资 = 综合薪资 ÷ 应出勤天数 × 实际计薪出勤天数
- 法定节假日工资 = 3000 ÷ 应出勤 ×（出勤天数×3 + 未出勤天数×1）；入职晚于法定日不享
- 房补：入离职当月按比例；否则 >15 天全额、≤15 天按出勤折算
- 押金：新员工当月扣 600；应发不足扣则当月不发放、结转下月
- 应发 = 出勤工资+加班+法定节假日+固定补贴+浮动补贴+房补+上月补发−上月补扣
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal

from app.models.comp import AllowanceKind, ComponentType
from app.models.employee import Department, EmploymentType
from app.payroll.social_tax import (
    CumulativeTaxInput,
    DerivedIncomeRule,
    PayrollPolicyContext,
    PolicyValidationError,
    calculate_cumulative_tax,
    calculate_social_insurance,
    validate_derived_income_rules,
)

_CENTS = Decimal("0.01")
ZERO = Decimal("0")
ONE = Decimal("1")


def q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RuleConfig:
    version: str = "v4"
    dining_divisor: Decimal = Decimal("9")  # 厅面：出勤工时÷9
    kitchen_divisor: Decimal = Decimal("9.5")  # 厨房：出勤工时÷9.5
    holiday_base: Decimal = Decimal("3000")  # 法定节假日固定基数
    deposit_amount: Decimal = Decimal("600")  # 新员工押金
    housing_full_threshold_days: Decimal = Decimal("15")  # 房补全额门槛
    overtime_multiplier: Decimal = Decimal("1.5")
    monthly_standard_days: Decimal = Decimal("21.75")
    hours_per_day: Decimal = Decimal("8")
    # Business has not approved a trial-period discount.  Keep the default as
    # a no-op while retaining an explicit, versioned calculation hook.
    probation_coefficient: Decimal = ONE


@dataclass(frozen=True)
class StructureComponent:
    code: str
    component_type: ComponentType
    amount: Decimal
    allowance_kind: AllowanceKind | None = None
    taxable: bool = True
    in_social_base: bool = False
    in_housing_base: bool = False
    prorate_by_attendance: bool = False


@dataclass(frozen=True)
class Attendance:
    expected_days: Decimal
    actual_days: Decimal = ZERO
    worked_hours: Decimal | None = None
    rest_days: Decimal = ZERO
    overtime_hours: Decimal = ZERO
    holiday_worked_days: Decimal = ZERO


@dataclass(frozen=True)
class StatutoryHoliday:
    """One configured statutory holiday and this employee's recorded work state."""

    day: date
    worked: bool = False


@dataclass(frozen=True)
class TaxYearToDate:
    """Only values from prior locked, current-year payroll snapshots."""

    taxable_income_before: Decimal = ZERO
    employee_contribution_before: Decimal = ZERO
    special_deduction_before: Decimal = ZERO
    tax_withheld_before: Decimal = ZERO
    employment_months_before: int = 0


@dataclass(frozen=True)
class TaxWithholdingState:
    """Structured monthly facts used by the next period's cumulative tax input."""

    current_taxable_income: Decimal
    current_employee_contribution: Decimal
    current_special_deduction: Decimal
    current_tax_withheld: Decimal
    cumulative_taxable_income: Decimal
    cumulative_tax_due: Decimal
    employment_months_to_date: int | None = None


@dataclass(frozen=True)
class TaxOpeningProvenance:
    """Immutable identity of the audited opening included in a YTD tax input."""

    opening_id: int
    revision: int
    tax_year: int
    through_period: str
    evidence_ref: str
    finalized_by: int | None
    finalized_at: datetime | None


@dataclass(frozen=True)
class EmployeeInput:
    employee_id: int
    period: str
    days_in_month: Decimal
    employment_type: EmploymentType
    department: Department
    is_special_position: bool
    structure: list[StructureComponent]
    attendance: Attendance | None = None
    generated_expected_days: Decimal | None = None
    expected_days_rule_id: int | None = None
    performance_coefficient: Decimal | None = None
    is_new_employee: bool = False  # 入职当月
    is_hire_or_leave_month: bool = False  # 入职或离职当月（房补按比例）
    holiday_eligible: bool = True  # 入职晚于法定日则 False
    statutory_holiday_days: Decimal = ZERO  # 当月法定节假日总天数
    holiday_calendar_finalized: bool = True
    # 新日历路径按每个法定日期独立判断劳动关系，不能把整月资格简化为
    # 一个布尔值。Mapping 兼容旧快照/测试导入，服务层只会构造
    # ``StatutoryHoliday``。
    statutory_holidays: tuple[StatutoryHoliday | Mapping[str, object], ...] = ()
    hire_date: date | None = None
    probation_end: date | None = None
    leave_date: date | None = None
    prev_makeup: Decimal = ZERO  # 上月补发
    prev_deduct: Decimal = ZERO  # 上月补扣
    # Prior-period corrections require an explicit payroll-policy treatment.
    # ``None`` identifies a legacy/unreviewed source and blocks policy payroll
    # rather than silently guessing its tax or contribution treatment.
    prev_makeup_taxable: bool | None = None
    prev_makeup_in_social_base: bool | None = None
    prev_makeup_in_housing_base: bool | None = None
    prev_deduct_taxable: bool | None = None
    prev_deduct_in_social_base: bool | None = None
    prev_deduct_in_housing_base: bool | None = None
    # 上一个已锁定周期因押金不足而未支付的工资和扣款义务。它们必须随
    # 下一期输入一起进入引擎，不能只停留在上一期结果展示中。
    prior_carry_forward: Decimal = ZERO
    prior_deferred_deductions: Decimal = ZERO
    prior_deferred_deposit: Decimal = ZERO
    # ``payroll_policy`` is a full immutable context, not a database pointer.
    # Batch snapshots serialize it so an audit recomputation never reads live
    # policy or employee tax-deduction records.
    payroll_policy: PayrollPolicyContext | None = None
    monthly_special_deduction: Decimal = ZERO
    tax_ytd: TaxYearToDate = TaxYearToDate()
    # Derived by the service from the employee's employment start and retained
    # in the input snapshot.  It must never be inferred from the calendar
    # month because a mid-year hire has fewer basic deductions.
    tax_employment_months: int | None = None
    # Numerical YTD values alone cannot identify a mid-year imported balance.
    # Preserve the finalized opening identity with the immutable batch input.
    tax_opening: TaxOpeningProvenance | None = None
    # 服务层遇到缺少/损坏的外部输入配置时，将确定的阻断原因传给纯引擎，
    # 保证批次仍可写出可审计的异常结果而不是吞掉该员工。
    source_exceptions: tuple[str, ...] = ()


@dataclass
class LineItem:
    code: str
    category: str
    formula: str
    amount: Decimal


@dataclass
class PayrollResult:
    employee_id: int
    period: str
    rule_version: str
    actual_attendance_days: Decimal = ZERO
    statutory_holiday_days: Decimal = ZERO
    statutory_holiday_worked_days: Decimal = ZERO
    lines: list[LineItem] = field(default_factory=list)
    gross: Decimal = ZERO  # 应发
    deposit: Decimal = ZERO  # 本月实扣押金
    net: Decimal = ZERO  # 实发（社保个税 S12 后补）
    carry_forward: Decimal = ZERO  # 结转下月金额（工资不足扣押金时）
    # 结转期还需保存未执行的扣款/押金义务；下一期只有同时读取这些值，
    # 才能完整清算而不漏扣或重复扣。
    deferred_deductions: Decimal = ZERO
    deferred_deposit: Decimal = ZERO
    tax_state: TaxWithholdingState | None = None
    exceptions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return bool(self.exceptions)

    def _add(self, code: str, category: str, formula: str, amount: Decimal) -> Decimal:
        amt = q(amount)
        self.lines.append(LineItem(code, category, formula, amt))
        return amt


def _sum(structure: list[StructureComponent], ctype: ComponentType) -> Decimal:
    return sum((c.amount for c in structure if c.component_type == ctype), ZERO)


def _sum_flagged(structure: list[StructureComponent], flag: str) -> Decimal:
    return sum((component.amount for component in structure if getattr(component, flag)), ZERO)


def _proportional_flagged_amount(
    amount: Decimal,
    structure: list[StructureComponent],
    component_type: ComponentType,
    flag: str,
) -> Decimal:
    """Allocate a calculated component amount using its configured source flags.

    A comprehensive component is prorated by attendance before it enters a tax
    or contribution base; using the monthly structure amount would overstate a
    partial-month employee's base.  Multiple source components are allocated
    proportionally, keeping the rule deterministic without inventing a priority
    order between them.
    """

    total = _sum(structure, component_type)
    selected = sum(
        (
            component.amount
            for component in structure
            if component.component_type == component_type and getattr(component, flag)
        ),
        ZERO,
    )
    return q(amount * selected / total) if total != ZERO and selected != ZERO else ZERO


def _policy_unclassified_component_errors(
    inp: EmployeeInput, *, performance_is_calculated: bool = False
) -> list[str]:
    """Reject a policy calculation rather than guessing an unimplemented basis."""

    errors: list[str] = []
    for component in inp.structure:
        uncalculated = {
            ComponentType.BASE,
            ComponentType.POSITION,
            ComponentType.OVERTIME,
        }
        if not performance_is_calculated:
            uncalculated.add(ComponentType.PERFORMANCE)
        if component.component_type in uncalculated and (
            component.taxable or component.in_social_base or component.in_housing_base
        ):
            errors.append(
                f"Payroll policy cannot classify uncalculated component {component.code}."
            )
        if component.component_type == ComponentType.DEDUCTION and (
            component.in_social_base or component.in_housing_base
        ):
            errors.append(
                f"Payroll policy cannot use deduction component {component.code} as a base."
            )
    return errors


def _derived_income_rule(policy: PayrollPolicyContext, code: str) -> DerivedIncomeRule | None:
    """Return the policy treatment for a calculated earnings line, if present."""

    return next((rule for rule in policy.derived_income_rules if rule.code == code), None)


def _factor_label(value: Decimal) -> str:
    """Render a Decimal factor without accidental exponent notation in payslips."""

    return format(value.normalize(), "f")


def _is_positive_finite(value: Decimal) -> bool:
    return value.is_finite() and value > ZERO


def _is_probation_period(inp: EmployeeInput) -> bool:
    """Whether the payroll month overlaps the employee's recorded probation."""

    if inp.probation_end is None:
        return False
    try:
        year, month = (int(part) for part in inp.period.split("-"))
        period_start = date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("计薪周期格式无效，无法判断试用期") from exc
    return inp.probation_end >= period_start


def _probation_factor(inp: EmployeeInput, cfg: RuleConfig) -> tuple[Decimal, bool]:
    """Return the configured multiplier only for a payroll month in probation."""

    factor = cfg.probation_coefficient
    if not factor.is_finite() or factor < ZERO:
        raise ValueError("试用期系数必须是非负有限数")
    is_probation = _is_probation_period(inp)
    return (factor if is_probation else ONE), is_probation


def _performance_factor(inp: EmployeeInput, res: PayrollResult) -> Decimal:
    """Use the recorded coefficient or its explicit neutral default safely."""

    factor = inp.performance_coefficient if inp.performance_coefficient is not None else ONE
    if not factor.is_finite() or factor < ZERO or factor > Decimal("5"):
        res.exceptions.append("绩效系数必须在 0 到 5 之间")
        return ONE
    return factor


def _allowance_amount(
    component: StructureComponent,
    actual_days: Decimal,
    expected_days: Decimal,
    res: PayrollResult,
) -> tuple[Decimal, str]:
    """Return the configured allowance amount or its attendance-prorated value."""

    if not component.prorate_by_attendance:
        return component.amount, "全额"
    if not _is_positive_finite(expected_days):
        res.exceptions.append(f"按出勤折算的补贴组件 {component.code} 缺少有效应出勤天数，无法核算")
        return ZERO, "按出勤折算(实际计薪出勤天数/应出勤天数，比例封顶1)"
    factor = min(max(actual_days / expected_days, ZERO), ONE)
    return (
        component.amount * factor,
        "按出勤折算(实际计薪出勤天数/应出勤天数，比例封顶1)",
    )


def _actual_attendance_days(inp: EmployeeInput, cfg: RuleConfig) -> Decimal:
    att = inp.attendance
    if att is None:
        raise ValueError("actual_attendance_days 要求 attendance 不为 None，请在上层先检查")
    # 规格中的第二套标准只适用于明确标记的特殊岗位；不能因为部门为
    # OTHER 就错误套用该规则。
    if inp.is_special_position:
        # v4 consumes the day count explicitly confirmed by store and HR.
        # Historical v3 snapshots predate that approval input and must retain
        # their original expected-days-minus-rest-days recomputation.
        if cfg.version == "v4":
            return att.actual_days
        return att.expected_days - att.rest_days
    if inp.department == Department.DINING:
        if att.worked_hours is None:
            raise ValueError("工时制岗位缺少出勤工时")
        return att.worked_hours / cfg.dining_divisor
    if inp.department == Department.KITCHEN:
        if att.worked_hours is None:
            raise ValueError("工时制岗位缺少出勤工时")
        return att.worked_hours / cfg.kitchen_divisor
    # 后勤/管理等 OTHER 普通岗位没有工时折算除数，使用已录入的实出勤
    # 天数。该分支在结果快照中可追溯，且不会把它伪装成特殊岗位规则。
    return att.actual_days


def _housing(inp: EmployeeInput, cfg: RuleConfig, actual: Decimal) -> Decimal:
    housing = _sum(inp.structure, ComponentType.HOUSING)
    if housing == 0:
        return ZERO
    expected = inp.attendance.expected_days if inp.attendance else ZERO
    if expected <= 0:
        return ZERO
    if inp.is_hire_or_leave_month:
        # 入离职当月按比例，封顶全额（比值不超过 1）
        return housing * min(actual / expected, Decimal("1"))
    if actual > cfg.housing_full_threshold_days:
        return housing  # >15 天全额
    return housing * actual / expected  # ≤15 天按出勤折算


def _coerce_holiday(raw: StatutoryHoliday | Mapping[str, object]) -> StatutoryHoliday:
    if isinstance(raw, StatutoryHoliday):
        return raw
    if not isinstance(raw, Mapping):
        raise ValueError("法定节假日条目必须包含日期和出勤状态")
    raw_day = raw.get("date")
    if isinstance(raw_day, date):
        day = raw_day
    else:
        try:
            day = date.fromisoformat(str(raw_day))
        except (TypeError, ValueError) as exc:
            raise ValueError("法定节假日日期无效") from exc
    worked = raw.get("worked", False)
    if not isinstance(worked, bool):
        raise ValueError("法定节假日出勤状态必须为布尔值")
    return StatutoryHoliday(day=day, worked=worked)


def _eligible_holidays(inp: EmployeeInput) -> list[StatutoryHoliday]:
    holidays = [_coerce_holiday(raw) for raw in inp.statutory_holidays]
    try:
        year, month = (int(part) for part in inp.period.split("-"))
        period_start = date(year, month, 1)
        period_end = date(year, month + 1, 1) if month < 12 else date(year + 1, 1, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("计薪周期格式无效，无法校验法定节假日") from exc
    if any(holiday.day < period_start or holiday.day >= period_end for holiday in holidays):
        raise ValueError("法定节假日日期不属于当前计薪周期")
    if len({holiday.day for holiday in holidays}) != len(holidays):
        raise ValueError("当前计薪周期存在重复的法定节假日日期")
    return [
        holiday
        for holiday in holidays
        if (inp.hire_date is None or holiday.day >= inp.hire_date)
        and (inp.leave_date is None or holiday.day <= inp.leave_date)
    ]


def _actual_attendance_days_v2(inp: EmployeeInput, cfg: RuleConfig) -> Decimal:
    """The v2 attendance rule retained for immutable historical snapshots."""

    att = inp.attendance
    if att is None:  # Defensive: callers check this before invoking the helper.
        raise ValueError("v2 payroll requires attendance")
    if inp.is_special_position or inp.department == Department.OTHER:
        return att.expected_days - att.rest_days
    worked_hours = att.worked_hours if att.worked_hours is not None else ZERO
    if inp.department == Department.DINING:
        return worked_hours / cfg.dining_divisor
    if inp.department == Department.KITCHEN:
        return worked_hours / cfg.kitchen_divisor
    return att.expected_days - att.rest_days


def _compute_v2(inp: EmployeeInput, cfg: RuleConfig) -> PayrollResult:
    """Reproduce the shipped v2 engine exactly for historical recomputation.

    This intentionally ignores later v3 concepts such as finalized holiday
    calendars, carry obligations, source exceptions, component tax flags, and
    policy deductions.  Those did not exist in the v2 result contract and
    applying them retroactively would change an immutable payroll result.
    """

    res = PayrollResult(inp.employee_id, inp.period, cfg.version)
    if inp.attendance is None:
        res.exceptions.append("缺少考勤数据，无法核算")
        return res
    att = inp.attendance
    if att.expected_days <= 0:
        res.exceptions.append("应出勤天数为 0，无法折算")
        return res

    comprehensive = _sum(inp.structure, ComponentType.COMPREHENSIVE)
    if comprehensive <= 0:
        res.exceptions.append("缺少综合薪资（计薪基数），无法核算出勤工资")

    worked_hours = att.worked_hours if att.worked_hours is not None else ZERO
    if (
        not inp.is_special_position
        and inp.department in (Department.DINING, Department.KITCHEN)
        and worked_hours <= 0
    ):
        res.exceptions.append("工时制岗位缺出勤工时，无法折算实际出勤")

    actual = _actual_attendance_days_v2(inp, cfg)
    actual = min(actual, inp.days_in_month)
    if actual < 0:
        actual = ZERO
    res.actual_attendance_days = q(actual)

    attend_wage = res._add(
        "ATTEND_WAGE",
        "出勤工资",
        "综合薪资÷应出勤×实际出勤",
        comprehensive / att.expected_days * actual,
    )

    overtime_wage = ZERO
    if att.overtime_hours > 0:
        hourly = comprehensive / cfg.monthly_standard_days / cfg.hours_per_day
        overtime_wage = res._add(
            "OVERTIME",
            "加班工资",
            "时薪×加班时长×倍数",
            hourly * att.overtime_hours * cfg.overtime_multiplier,
        )

    holiday_wage = ZERO
    if inp.holiday_eligible and inp.statutory_holiday_days > 0:
        worked = min(att.holiday_worked_days, inp.statutory_holiday_days)
        not_worked = inp.statutory_holiday_days - worked
        holiday_wage = res._add(
            "HOLIDAY",
            "法定节假日工资",
            "3000÷应出勤×(出勤×3+未出勤×1)",
            cfg.holiday_base / att.expected_days * (worked * 3 + not_worked),
        )

    fixed_allow = ZERO
    floating_allow = ZERO
    for component in inp.structure:
        if component.component_type != ComponentType.ALLOWANCE:
            continue
        allowance_amount, formula = _allowance_amount(component, actual, att.expected_days, res)
        if component.allowance_kind == AllowanceKind.FLOATING:
            floating_allow += res._add(component.code, "浮动补贴", formula, allowance_amount)
        else:
            fixed_allow += res._add(component.code, "固定补贴", formula, allowance_amount)

    housing = _housing(inp, cfg, actual)
    if housing != ZERO:
        res._add("HOUSING", "房补", "按 15 天/入离职规则", housing)
    housing = q(housing)

    prev_makeup = q(inp.prev_makeup)
    prev_deduct = q(inp.prev_deduct)
    if prev_makeup != ZERO:
        res._add("PREV_MAKEUP", "上月补发", "上月补发", prev_makeup)
    if prev_deduct != ZERO:
        res._add("PREV_DEDUCT", "上月补扣", "上月补扣", -prev_deduct)

    res.gross = q(
        attend_wage
        + overtime_wage
        + holiday_wage
        + fixed_allow
        + floating_allow
        + housing
        + prev_makeup
        - prev_deduct
    )

    other_deduct = -_sum(inp.structure, ComponentType.DEDUCTION)
    if other_deduct != ZERO:
        res._add("DEDUCTION", "其他扣款", "扣款", other_deduct)
    payable = res.gross + other_deduct
    if inp.is_new_employee and payable < cfg.deposit_amount:
        res.carry_forward = res.gross
        res.net = ZERO
        res.exceptions.append("新员工工资不足扣押金，当月不发放，结转下月")
    else:
        if inp.is_new_employee:
            res.deposit = cfg.deposit_amount
        res.net = q(payable - res.deposit)
        if res.net < ZERO:
            res.exceptions.append("实发为负，需人工复核")
    return res


def _compute_v3(inp: EmployeeInput, cfg: RuleConfig) -> PayrollResult:
    res = PayrollResult(inp.employee_id, inp.period, cfg.version)
    res.exceptions.extend(inp.source_exceptions)

    if inp.attendance is None:
        res.exceptions.append("缺少考勤数据，无法核算")
        return res
    att = inp.attendance
    is_hourly_employee = (
        cfg.version == "v4"
        and inp.employment_type == EmploymentType.PART_TIME_HOURLY
        and not inp.is_special_position
    )
    if cfg.version == "v4" and (not att.expected_days.is_finite() or att.expected_days < ZERO):
        res.exceptions.append("应出勤天数必须为非负有限数，无法核算")
        return res
    if not is_hourly_employee and att.expected_days <= 0:
        res.exceptions.append("应出勤天数为 0，无法折算")
        return res
    if not inp.holiday_calendar_finalized:
        res.exceptions.append("当月法定节假日日历尚未由人事确认，无法核算")

    comprehensive = _sum(inp.structure, ComponentType.COMPREHENSIVE)
    if comprehensive <= 0:
        if is_hourly_employee:
            res.exceptions.append("缺少时薪（综合薪资组件），无法核算兼职小时工工资")
        else:
            res.exceptions.append("缺少综合薪资（计薪基数），无法核算出勤工资")

    probation_factor = ONE
    is_probation = False
    if cfg.version == "v4":
        try:
            probation_factor, is_probation = _probation_factor(inp, cfg)
        except ValueError as exc:
            res.exceptions.append(str(exc))

    # 实际计薪出勤天数（两套标准，上限当月天数）。NULL 与真实零工时
    # 必须区分：前者阻断，后者是可支付为零的有效考勤。
    hourly_worked_hours = ZERO
    if is_hourly_employee:
        if att.worked_hours is None:
            res.exceptions.append("兼职小时工缺少出勤工时，无法核算时薪工资")
            actual = ZERO
        elif not att.worked_hours.is_finite() or att.worked_hours < ZERO:
            res.exceptions.append("兼职小时工出勤工时必须为非负有限数")
            actual = ZERO
        else:
            hourly_worked_hours = att.worked_hours
            if not _is_positive_finite(cfg.hours_per_day):
                res.exceptions.append("兼职小时工标准工时必须大于 0")
                actual = ZERO
            else:
                actual = hourly_worked_hours / cfg.hours_per_day
    elif (
        not inp.is_special_position
        and inp.department in (Department.DINING, Department.KITCHEN)
        and att.worked_hours is None
    ):
        res.exceptions.append("工时制岗位缺少出勤工时，无法折算实际出勤")
        actual = ZERO
    else:
        actual = _actual_attendance_days(inp, cfg)
    actual = min(actual, inp.days_in_month)
    if actual < 0:
        actual = ZERO
    res.actual_attendance_days = q(actual)

    # 1. 出勤工资 = 综合薪资 / 应出勤 × 实际
    if is_hourly_employee:
        attend_formula = "时薪×实际工时"
        if is_probation:
            attend_formula += f"×试用期系数({_factor_label(probation_factor)})"
        attend_wage = res._add(
            "ATTEND_WAGE",
            "时薪工资",
            attend_formula,
            comprehensive * hourly_worked_hours * probation_factor,
        )
    else:
        attend_formula = "综合薪资÷应出勤×实际出勤"
        if is_probation:
            attend_formula += f"×试用期系数({_factor_label(probation_factor)})"
        attend_wage = res._add(
            "ATTEND_WAGE",
            "出勤工资",
            attend_formula,
            comprehensive / att.expected_days * actual * probation_factor,
        )

    # 2. 加班工资 = 综合薪资/21.75/8 × 加班时长 × 倍数
    overtime_wage = ZERO
    if att.overtime_hours > 0:
        invalid_monthly_overtime_config = (
            cfg.version == "v4"
            and not is_hourly_employee
            and (
                not _is_positive_finite(cfg.monthly_standard_days)
                or not _is_positive_finite(cfg.hours_per_day)
            )
        )
        if invalid_monthly_overtime_config:
            res.exceptions.append("加班时薪配置必须使用正的有限月标准天数和日工时")
        else:
            hourly = (
                comprehensive
                if is_hourly_employee
                else comprehensive / cfg.monthly_standard_days / cfg.hours_per_day
            )
            overtime_wage = res._add(
                "OVERTIME",
                "加班工资",
                "时薪×加班时长×倍数",
                hourly * att.overtime_hours * cfg.overtime_multiplier,
            )

    # 3. 法定节假日工资。配置了日历后逐日判断劳动关系与出勤；旧的聚合
    # 输入只用于历史快照兼容，绝不覆盖新的逐日结果。
    holiday_wage = ZERO
    eligible_holidays: list[StatutoryHoliday] = []
    holiday_calendar_valid = True
    if inp.statutory_holidays:
        try:
            eligible_holidays = _eligible_holidays(inp)
        except ValueError as exc:
            holiday_calendar_valid = False
            res.exceptions.append(str(exc))

    if is_hourly_employee and (
        (holiday_calendar_valid and bool(eligible_holidays))
        or (
            not inp.statutory_holidays
            and inp.holiday_eligible
            and inp.statutory_holiday_days > ZERO
        )
    ):
        # The approved hourly rule is only "hourly rate × worked hours".  It
        # does not yet define paid statutory-holiday hours, so do not invent a
        # monthly-base conversion or silently omit the entitlement.
        res.exceptions.append("兼职小时工法定节假日计薪规则尚未配置，无法核算")
    elif inp.statutory_holidays and holiday_calendar_valid:
        res.statutory_holiday_days = q(Decimal(len(eligible_holidays)))
        res.statutory_holiday_worked_days = q(
            Decimal(sum(1 for holiday in eligible_holidays if holiday.worked))
        )
        holiday_units = sum(
            (Decimal("3") if holiday.worked else Decimal("1") for holiday in eligible_holidays),
            ZERO,
        )
        if holiday_units > ZERO:
            holiday_wage = res._add(
                "HOLIDAY",
                "法定节假日工资",
                "3000÷应出勤×逐日(出勤3倍/未出勤1倍)",
                cfg.holiday_base / att.expected_days * holiday_units,
            )
    elif not inp.statutory_holidays and inp.holiday_eligible and inp.statutory_holiday_days > 0:
        res.statutory_holiday_days = q(inp.statutory_holiday_days)
        worked = min(att.holiday_worked_days, inp.statutory_holiday_days)
        res.statutory_holiday_worked_days = q(worked)
        not_worked = inp.statutory_holiday_days - worked
        holiday_wage = res._add(
            "HOLIDAY",
            "法定节假日工资",
            "3000÷应出勤×(出勤×3+未出勤×1)",
            cfg.holiday_base / att.expected_days * (worked * 3 + not_worked),
        )

    # 4. 绩效组件：每项结构额均乘以同一期绩效系数；系数缺失时使用显式
    # 中性值 1，避免把未录绩效误解为零薪资。
    performance_wage = ZERO
    taxable_performance = ZERO
    social_performance = ZERO
    housing_performance = ZERO
    if cfg.version == "v4":
        performance_factor = _performance_factor(inp, res)
        performance_formula = f"绩效基数×绩效系数({_factor_label(performance_factor)})"
        for component in inp.structure:
            if component.component_type != ComponentType.PERFORMANCE:
                continue
            amount = res._add(
                component.code,
                "绩效工资",
                performance_formula,
                component.amount * performance_factor,
            )
            performance_wage += amount
            if component.taxable:
                taxable_performance += amount
            if component.in_social_base:
                social_performance += amount
            if component.in_housing_base:
                housing_performance += amount

    # 5. 固定补贴 / 6. 浮动补贴
    fixed_allow = ZERO
    floating_allow = ZERO
    taxable_allow = ZERO
    social_allow = ZERO
    housing_allow = ZERO
    for c in inp.structure:
        if c.component_type != ComponentType.ALLOWANCE:
            continue
        if c.allowance_kind is None:
            res.exceptions.append(f"补贴组件 {c.code} 缺少固定/浮动类型，无法核算")
            continue
        allowance_amount, formula = _allowance_amount(c, actual, att.expected_days, res)
        if c.allowance_kind == AllowanceKind.FLOATING:
            amount = res._add(c.code, "浮动补贴", formula, allowance_amount)
            floating_allow += amount
        else:
            amount = res._add(c.code, "固定补贴", formula, allowance_amount)
            fixed_allow += amount
        if c.taxable:
            taxable_allow += amount
        if c.in_social_base:
            social_allow += amount
        if c.in_housing_base:
            housing_allow += amount

    # 7. 房补
    housing = _housing(inp, cfg, actual)
    if housing != 0:
        res._add("HOUSING", "房补", "按 15 天/入离职规则", housing)
    housing = q(housing)

    # 8. 上月补发 / 补扣
    prev_makeup = q(inp.prev_makeup)
    prev_deduct = q(inp.prev_deduct)
    if prev_makeup != 0:
        res._add("PREV_MAKEUP", "上月补发", "上月补发", prev_makeup)
    if prev_deduct != 0:
        res._add("PREV_DEDUCT", "上月补扣", "上月补扣", -prev_deduct)

    # 9. 应发 = 出勤+加班+法定+绩效+固定+浮动+房补+上月补发−上月补扣
    current_gross = q(
        attend_wage
        + overtime_wage
        + holiday_wage
        + performance_wage
        + fixed_allow
        + floating_allow
        + housing
        + prev_makeup
        - prev_deduct
    )
    prior_carry_forward = q(inp.prior_carry_forward)
    if prior_carry_forward != ZERO:
        res._add("CARRY_FORWARD_WAGE", "上月未发工资", "上月工资结转", prior_carry_forward)
    res.gross = q(current_gross + prior_carry_forward)

    # 10. 城市政策：个人/单位社保公积金 + 累计预扣个税。
    # The policy context is selected and validated by the service layer.  This
    # engine still catches invalid data so a bad persisted snapshot is an
    # auditable employee-level error rather than a failed whole-batch write.
    employee_contribution = ZERO
    tax_withholding = ZERO
    policy = inp.payroll_policy
    if policy is not None:
        policy_errors = _policy_unclassified_component_errors(
            inp,
            performance_is_calculated=cfg.version == "v4",
        )
        derived_income: tuple[tuple[str, Decimal], ...] = ()
        derived_rules: dict[str, DerivedIncomeRule] = {}
        if cfg.version == "v4":
            try:
                validate_derived_income_rules(policy.derived_income_rules)
            except PolicyValidationError as exc:
                policy_errors.append(f"Payroll policy derived-income treatment is invalid: {exc}")
            derived_income = (("OVERTIME", overtime_wage), ("HOLIDAY", holiday_wage))
            for code, amount in derived_income:
                if amount == ZERO:
                    continue
                rule = _derived_income_rule(policy, code)
                if rule is None:
                    policy_errors.append(
                        f"Payroll policy has no derived-income treatment for {code}."
                    )
                else:
                    derived_rules[code] = rule
        else:
            # v3 snapshots predate policy-defined treatment of derived earnings;
            # preserving their original block is required for audit recomputation.
            if overtime_wage != ZERO:
                policy_errors.append(
                    "Payroll policy cannot classify overtime without component flags."
                )
            if holiday_wage != ZERO:
                policy_errors.append(
                    "Payroll policy cannot classify statutory-holiday pay without component flags."
                )
        prior_adjustments = (
            (
                "PREV_MAKEUP",
                prev_makeup,
                inp.prev_makeup_taxable,
                inp.prev_makeup_in_social_base,
                inp.prev_makeup_in_housing_base,
            ),
            (
                "PREV_DEDUCT",
                prev_deduct,
                inp.prev_deduct_taxable,
                inp.prev_deduct_in_social_base,
                inp.prev_deduct_in_housing_base,
            ),
        )
        for code, amount, taxable, in_social, in_housing in prior_adjustments:
            if amount != ZERO and None in (taxable, in_social, in_housing):
                policy_errors.append(
                    f"Payroll policy cannot classify prior-period adjustment {code} "
                    "without source flags."
                )
        if policy_errors:
            res.exceptions.extend(policy_errors)
        else:
            derived_social_base = sum(
                (
                    amount
                    for code, amount in derived_income
                    if amount != ZERO and derived_rules[code].in_social_base
                ),
                ZERO,
            )
            derived_housing_base = sum(
                (
                    amount
                    for code, amount in derived_income
                    if amount != ZERO and derived_rules[code].in_housing_base
                ),
                ZERO,
            )
            derived_taxable_income = sum(
                (
                    amount
                    for code, amount in derived_income
                    if amount != ZERO and derived_rules[code].taxable
                ),
                ZERO,
            )
            prior_social_base = (
                prev_makeup if inp.prev_makeup_in_social_base is True else ZERO
            ) - (prev_deduct if inp.prev_deduct_in_social_base is True else ZERO)
            prior_housing_base = (
                prev_makeup if inp.prev_makeup_in_housing_base is True else ZERO
            ) - (prev_deduct if inp.prev_deduct_in_housing_base is True else ZERO)
            prior_taxable_income = (prev_makeup if inp.prev_makeup_taxable is True else ZERO) - (
                prev_deduct if inp.prev_deduct_taxable is True else ZERO
            )
            social_base = q(
                _proportional_flagged_amount(
                    attend_wage, inp.structure, ComponentType.COMPREHENSIVE, "in_social_base"
                )
                + social_performance
                + social_allow
                + _proportional_flagged_amount(
                    housing, inp.structure, ComponentType.HOUSING, "in_social_base"
                )
                + derived_social_base
                + prior_social_base
            )
            housing_base = q(
                _proportional_flagged_amount(
                    attend_wage, inp.structure, ComponentType.COMPREHENSIVE, "in_housing_base"
                )
                + housing_performance
                + housing_allow
                + _proportional_flagged_amount(
                    housing, inp.structure, ComponentType.HOUSING, "in_housing_base"
                )
                + derived_housing_base
                + prior_housing_base
            )
            current_taxable_income = q(
                _proportional_flagged_amount(
                    attend_wage, inp.structure, ComponentType.COMPREHENSIVE, "taxable"
                )
                + taxable_performance
                + taxable_allow
                + _proportional_flagged_amount(
                    housing, inp.structure, ComponentType.HOUSING, "taxable"
                )
                + derived_taxable_income
                + prior_taxable_income
            )
            try:
                social_result = calculate_social_insurance(
                    policy=policy.social_policy,
                    social_base=social_base,
                    housing_base=housing_base,
                )
                employee_contribution = social_result.employee_total
                for contribution in social_result.lines:
                    prefix = (
                        "HOUSING_FUND"
                        if contribution.kind.value == "HOUSING"
                        else f"SOCIAL_{contribution.kind.value}"
                    )
                    if contribution.employee_amount != ZERO:
                        res._add(
                            f"{prefix}_EMPLOYEE",
                            "个人社保公积金",
                            f"{policy.city}政策基数×个人费率",
                            -contribution.employee_amount,
                        )
                    if contribution.employer_amount != ZERO:
                        res._add(
                            f"{prefix}_EMPLOYER",
                            "单位社保公积金",
                            f"{policy.city}政策基数×单位费率",
                            contribution.employer_amount,
                        )
                if cfg.version == "v3":
                    try:
                        employment_months = int(inp.period.split("-")[1])
                    except (IndexError, ValueError) as exc:
                        raise PolicyValidationError(
                            "payroll period must contain a valid month"
                        ) from exc
                else:
                    tax_employment_months = inp.tax_employment_months
                    if tax_employment_months is None:
                        raise PolicyValidationError(
                            "employment-month count is required for cumulative withholding"
                        )
                    employment_months = tax_employment_months
                tax_result = calculate_cumulative_tax(
                    policy=policy.tax_policy,
                    input=CumulativeTaxInput(
                        employment_months=employment_months,
                        ytd_taxable_income_before=inp.tax_ytd.taxable_income_before,
                        ytd_employee_contribution_before=inp.tax_ytd.employee_contribution_before,
                        ytd_special_deduction_before=inp.tax_ytd.special_deduction_before,
                        ytd_tax_withheld_before=inp.tax_ytd.tax_withheld_before,
                        current_taxable_income=current_taxable_income,
                        current_employee_contribution=employee_contribution,
                        current_special_deduction=inp.monthly_special_deduction,
                    ),
                )
                tax_withholding = tax_result.current_withholding
                if tax_withholding != ZERO:
                    res._add(
                        "IIT_WITHHOLDING",
                        "累计预扣个税",
                        "累计应纳税额－已预扣税额",
                        -tax_withholding,
                    )
                res.tax_state = TaxWithholdingState(
                    current_taxable_income=current_taxable_income,
                    current_employee_contribution=employee_contribution,
                    current_special_deduction=q(inp.monthly_special_deduction),
                    current_tax_withheld=tax_withholding,
                    cumulative_taxable_income=tax_result.cumulative_taxable_income,
                    cumulative_tax_due=tax_result.cumulative_tax_due,
                    employment_months_to_date=(employment_months if cfg.version == "v4" else None),
                )
            except PolicyValidationError as exc:
                res.exceptions.append(f"Payroll policy calculation is invalid: {exc}")

    # 11. Other deductions + deposit + net pay.
    current_deductions = q(_sum(inp.structure, ComponentType.DEDUCTION))
    prior_deferred_deductions = q(inp.prior_deferred_deductions)
    total_deductions = q(
        current_deductions + prior_deferred_deductions + employee_contribution + tax_withholding
    )
    if current_deductions != ZERO:
        res._add("DEDUCTION", "其他扣款", "扣款", -current_deductions)
    if prior_deferred_deductions != ZERO:
        res._add(
            "CARRY_FORWARD_DEDUCTION",
            "上月延后扣款",
            "上月未执行扣款结转",
            -prior_deferred_deductions,
        )
    payable = res.gross - total_deductions  # 可支配额（应发 − 扣款），据此判断能否扣押金
    deposit_due = q(
        inp.prior_deferred_deposit + (cfg.deposit_amount if inp.is_new_employee else ZERO)
    )

    if deposit_due != ZERO and payable < deposit_due:
        # 规格7.10：工资不足扣押金 → 当月不发放，工资、扣款义务和未收
        # 押金均结转；下一期会作为明确输入再次参与核算。
        res.carry_forward = res.gross
        res.deferred_deductions = total_deductions
        res.deferred_deposit = deposit_due
        res.net = ZERO
        res.warnings.append("新员工工资不足扣押金，当月不发放，结转下月")
        if policy is not None and (
            employee_contribution != ZERO
            or tax_withholding != ZERO
            or prior_deferred_deductions != ZERO
        ):
            # Generic deferred deductions do not carry the identity needed for
            # a later cumulative-tax calculation.  A failed policy payroll
            # must therefore be corrected before it can be locked, rather than
            # silently allowing social insurance or IIT to be counted twice.
            res.exceptions.append(
                "Payroll policy deductions cannot be deferred; complete a manual settlement first."
            )
        if (
            inp.leave_date is not None
            and f"{inp.leave_date.year:04d}-{inp.leave_date.month:02d}" == inp.period
        ):
            # A normal next-period carry would disappear from the cohort after
            # this employee leaves.  Keep the result auditable, but prevent it
            # from becoming a locked, uncollectable obligation.
            res.exceptions.append(
                "Terminating employee has unpaid payroll carry; "
                "final settlement is required before locking."
            )
    else:
        res.deposit = deposit_due
        # 实发 = 应发 − 扣款 − 押金（社保个税 S12 后补）
        res.net = q(payable - res.deposit)
        if res.net < 0:
            res.exceptions.append("实发为负，需人工复核")
    return res


def compute(inp: EmployeeInput, cfg: RuleConfig | None = None) -> PayrollResult:
    """Calculate a payroll result using the immutable rule version requested.

    New calculations use v4 by default.  Recomputing a persisted v2/v3
    snapshot dispatches to its legacy semantics rather than applying later
    rules simply because the application itself has been upgraded.
    """

    config = cfg or RuleConfig()
    if config.version == "v2":
        return _compute_v2(inp, config)
    if config.version not in {"v3", "v4"}:
        raise ValueError(f"Unsupported payroll rule version: {config.version}")
    return _compute_v3(inp, config)
