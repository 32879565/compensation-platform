"""薪资计算规则引擎 v2（按业务规格 docs/payroll-batch-spec.md 的真实公式）。

不变量：全程 Decimal、逐项 quantize(0.01, ROUND_HALF_UP)、确定性、逐项可追溯、
缺输入进 exceptions 且 has_error 阻断出账。策略参数集中在 RuleConfig（版本化 v2）。

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

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.models.comp import AllowanceKind, ComponentType
from app.models.employee import Department, EmploymentType

_CENTS = Decimal("0.01")
ZERO = Decimal("0")


def q(value: Decimal) -> Decimal:
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RuleConfig:
    version: str = "v2"
    dining_divisor: Decimal = Decimal("9")  # 厅面：出勤工时÷9
    kitchen_divisor: Decimal = Decimal("9.5")  # 厨房：出勤工时÷9.5
    holiday_base: Decimal = Decimal("3000")  # 法定节假日固定基数
    deposit_amount: Decimal = Decimal("600")  # 新员工押金
    housing_full_threshold_days: Decimal = Decimal("15")  # 房补全额门槛
    overtime_multiplier: Decimal = Decimal("1.5")
    monthly_standard_days: Decimal = Decimal("21.75")
    hours_per_day: Decimal = Decimal("8")


@dataclass(frozen=True)
class StructureComponent:
    code: str
    component_type: ComponentType
    amount: Decimal
    allowance_kind: AllowanceKind | None = None


@dataclass(frozen=True)
class Attendance:
    expected_days: Decimal
    actual_days: Decimal = ZERO
    worked_hours: Decimal = ZERO
    rest_days: Decimal = ZERO
    overtime_hours: Decimal = ZERO
    holiday_worked_days: Decimal = ZERO


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
    performance_coefficient: Decimal | None = None
    is_new_employee: bool = False  # 入职当月
    is_hire_or_leave_month: bool = False  # 入职或离职当月（房补按比例）
    holiday_eligible: bool = True  # 入职晚于法定日则 False
    statutory_holiday_days: Decimal = ZERO  # 当月法定节假日总天数
    prev_makeup: Decimal = ZERO  # 上月补发
    prev_deduct: Decimal = ZERO  # 上月补扣


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
    lines: list[LineItem] = field(default_factory=list)
    gross: Decimal = ZERO  # 应发
    deposit: Decimal = ZERO  # 本月实扣押金
    net: Decimal = ZERO  # 实发（社保个税 S12 后补）
    carry_forward: Decimal = ZERO  # 结转下月金额（工资不足扣押金时）
    exceptions: list[str] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return bool(self.exceptions)

    def _add(self, code: str, category: str, formula: str, amount: Decimal) -> Decimal:
        amt = q(amount)
        self.lines.append(LineItem(code, category, formula, amt))
        return amt


def _sum(structure: list[StructureComponent], ctype: ComponentType) -> Decimal:
    return sum((c.amount for c in structure if c.component_type == ctype), ZERO)


def _actual_attendance_days(inp: EmployeeInput, cfg: RuleConfig) -> Decimal:
    att = inp.attendance
    assert att is not None
    # 特殊岗位 / 其他部门：应出勤 − 休息天数
    if inp.is_special_position or inp.department == Department.OTHER:
        return att.expected_days - att.rest_days
    if inp.department == Department.DINING:
        return att.worked_hours / cfg.dining_divisor
    if inp.department == Department.KITCHEN:
        return att.worked_hours / cfg.kitchen_divisor
    return att.expected_days - att.rest_days  # 兜底


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


def compute(inp: EmployeeInput, cfg: RuleConfig | None = None) -> PayrollResult:
    cfg = cfg or RuleConfig()
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

    # 工时制岗位缺工时无法折算：报异常阻断（区分数据缺失与真实零出勤）
    if (
        not inp.is_special_position
        and inp.department in (Department.DINING, Department.KITCHEN)
        and att.worked_hours <= 0
    ):
        res.exceptions.append("工时制岗位缺出勤工时，无法折算实际出勤")

    # 实际计薪出勤天数（两套标准，上限当月天数）
    actual = _actual_attendance_days(inp, cfg)
    actual = min(actual, inp.days_in_month)
    if actual < 0:
        actual = ZERO
    res.actual_attendance_days = q(actual)

    # 1. 出勤工资 = 综合薪资 / 应出勤 × 实际
    attend_wage = res._add(
        "ATTEND_WAGE",
        "出勤工资",
        "综合薪资÷应出勤×实际出勤",
        comprehensive / att.expected_days * actual,
    )

    # 2. 加班工资 = 综合薪资/21.75/8 × 加班时长 × 倍数
    overtime_wage = ZERO
    if att.overtime_hours > 0:
        hourly = comprehensive / cfg.monthly_standard_days / cfg.hours_per_day
        overtime_wage = res._add(
            "OVERTIME",
            "加班工资",
            "时薪×加班时长×倍数",
            hourly * att.overtime_hours * cfg.overtime_multiplier,
        )

    # 3. 法定节假日工资
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

    # 4. 固定补贴 / 5. 浮动补贴
    fixed_allow = ZERO
    floating_allow = ZERO
    for c in inp.structure:
        if c.component_type != ComponentType.ALLOWANCE:
            continue
        if c.allowance_kind == AllowanceKind.FLOATING:
            floating_allow += res._add(c.code, "浮动补贴", "全额", c.amount)
        else:  # 默认按固定补贴
            fixed_allow += res._add(c.code, "固定补贴", "全额", c.amount)

    # 6. 房补
    housing = _housing(inp, cfg, actual)
    if housing != 0:
        res._add("HOUSING", "房补", "按 15 天/入离职规则", housing)
    housing = q(housing)

    # 7. 上月补发 / 补扣
    prev_makeup = q(inp.prev_makeup)
    prev_deduct = q(inp.prev_deduct)
    if prev_makeup != 0:
        res._add("PREV_MAKEUP", "上月补发", "上月补发", prev_makeup)
    if prev_deduct != 0:
        res._add("PREV_DEDUCT", "上月补扣", "上月补扣", -prev_deduct)

    # 8. 应发 = 出勤+加班+法定+固定+浮动+房补+上月补发−上月补扣
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

    # 9. 其他扣款 + 押金 + 实发
    other_deduct = -_sum(inp.structure, ComponentType.DEDUCTION)  # ≤0
    if other_deduct != 0:
        res._add("DEDUCTION", "其他扣款", "扣款", other_deduct)
    payable = res.gross + other_deduct  # 可支配额（应发 − 扣款），据此判断能否扣押金

    if inp.is_new_employee and payable < cfg.deposit_amount:
        # 规格7.10：工资不足扣押金 → 当月不发放、押金不扣、应发全额结转下月（扣款义务随之延后）
        res.carry_forward = res.gross
        res.net = ZERO
        res.exceptions.append("新员工工资不足扣押金，当月不发放，结转下月")
    else:
        if inp.is_new_employee:
            res.deposit = cfg.deposit_amount  # 新员工足额扣押金
        # 实发 = 应发 − 扣款 − 押金（社保个税 S12 后补）
        res.net = q(payable - res.deposit)
        if res.net < 0:
            res.exceptions.append("实发为负，需人工复核")
    return res
