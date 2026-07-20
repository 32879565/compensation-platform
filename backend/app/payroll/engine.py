"""薪资计算规则引擎（纯函数，确定性，逐项可追溯）。

设计不变量：
- 全程 Decimal，逐项 quantize(0.01, ROUND_HALF_UP)，禁 float。
- 同输入 + 同规则版本 → 同输出（确定性）。
- 每个组件产出一条 LineItem（组件→输入→公式→结果），供工资条与复核展开。
- 缺输入/异常不静默：进 exceptions 并 has_error=True，S13 据此阻断出账。

策略参数集中在 RuleConfig（版本化、可审计）。默认规则 v1 的口径（出勤折算除数、
加班倍数、每日工时、兼职工时来源、试用系数）均为可调策略，业务确认后调整（见蓝图开放问题）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal

from app.models.comp import ComponentType
from app.models.employee import EmploymentType

_CENTS = Decimal("0.01")
ZERO = Decimal("0")


def q(value: Decimal) -> Decimal:
    """量化到分，四舍五入（财务口径 ROUND_HALF_UP，非银行家舍入）。"""
    return value.quantize(_CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class RuleConfig:
    version: str = "v1"
    overtime_multiplier: Decimal = Decimal("1.5")  # 工作日加班倍数
    hours_per_day: Decimal = Decimal("8")
    monthly_standard_days: Decimal = Decimal("21.75")  # 月计薪标准天数（时薪折算用）
    # 按出勤比例折算的组件类型（其余按全额）
    prorate_types: frozenset[ComponentType] = frozenset(
        {ComponentType.BASE, ComponentType.POSITION}
    )
    # 加班计算基数所用的组件类型
    overtime_base_types: frozenset[ComponentType] = frozenset(
        {ComponentType.BASE, ComponentType.POSITION}
    )


@dataclass(frozen=True)
class StructureComponent:
    code: str
    component_type: ComponentType
    amount: Decimal


@dataclass(frozen=True)
class Attendance:
    expected_days: Decimal
    actual_days: Decimal
    overtime_hours: Decimal = ZERO
    leave_days: Decimal = ZERO


@dataclass(frozen=True)
class EmployeeInput:
    employee_id: int
    period: str
    employment_type: EmploymentType
    structure: list[StructureComponent]
    attendance: Attendance | None = None
    performance_coefficient: Decimal | None = None
    probation_coefficient: Decimal = Decimal("1")  # 试用期系数，1 表示不打折


@dataclass
class LineItem:
    code: str
    component_type: ComponentType
    input_amount: Decimal
    formula: str
    amount: Decimal  # 已量化


@dataclass
class PayrollResult:
    employee_id: int
    period: str
    rule_version: str
    lines: list[LineItem] = field(default_factory=list)
    gross: Decimal = ZERO
    exceptions: list[str] = field(default_factory=list)

    @property
    def has_error(self) -> bool:
        return bool(self.exceptions)


def _attendance_ratio(att: Attendance) -> Decimal:
    if att.expected_days <= 0:
        return ZERO
    ratio = att.actual_days / att.expected_days
    return min(ratio, Decimal("1"))  # 超出部分按加班另算，不重复计入


def _overtime_base_monthly(cfg: RuleConfig, structure: list[StructureComponent]) -> Decimal:
    return sum((c.amount for c in structure if c.component_type in cfg.overtime_base_types), ZERO)


def _has_perf(structure: list[StructureComponent]) -> bool:
    return any(c.component_type == ComponentType.PERFORMANCE for c in structure)


def _append_other(res: PayrollResult, c: StructureComponent, perf_coeff: Decimal | None) -> None:
    """非折算组件的统一处理（绩效/补贴/扣款），全职与兼职共用，避免口径分叉。"""
    if c.component_type == ComponentType.PERFORMANCE:
        if perf_coeff is None:
            return  # 缺绩效系数异常已在上层记录
        res.lines.append(
            LineItem(
                c.code, c.component_type, c.amount, "绩效基数×绩效系数", q(c.amount * perf_coeff)
            )
        )
    elif c.component_type == ComponentType.DEDUCTION:
        # -abs 保证扣款恒为负：即便误录成负数也不会二次取负变成加发
        res.lines.append(LineItem(c.code, c.component_type, c.amount, "扣款", q(-abs(c.amount))))
    else:
        res.lines.append(LineItem(c.code, c.component_type, c.amount, "全额", q(c.amount)))


def _compute_full_time(inp: EmployeeInput, cfg: RuleConfig, res: PayrollResult) -> None:
    if inp.attendance is None:
        res.exceptions.append("缺少考勤数据，无法核算")
        return
    att = inp.attendance
    if att.expected_days <= 0:
        res.exceptions.append("应出勤天数为 0，无法折算")
        return
    ratio = _attendance_ratio(att)

    has_performance_component = any(
        c.component_type == ComponentType.PERFORMANCE for c in inp.structure
    )
    if has_performance_component and inp.performance_coefficient is None:
        res.exceptions.append("缺少绩效系数，无法核算绩效")

    for c in inp.structure:
        if c.component_type in cfg.prorate_types:
            amount = q(c.amount * ratio * inp.probation_coefficient)
            # 试用系数=1 时不在公式里标注，避免"已按试用折算"的误导
            label = "月额×出勤比×试用系数" if inp.probation_coefficient != 1 else "月额×出勤比"
            res.lines.append(LineItem(c.code, c.component_type, c.amount, label, amount))
        else:
            _append_other(res, c, inp.performance_coefficient)

    # 加班费：基数月额 / 标准天数 / 每日工时 × 加班时长 × 倍数
    if att.overtime_hours > 0:
        base_monthly = _overtime_base_monthly(cfg, inp.structure)
        hourly = base_monthly / cfg.monthly_standard_days / cfg.hours_per_day
        ot = q(hourly * att.overtime_hours * cfg.overtime_multiplier)
        res.lines.append(
            LineItem("OVERTIME", ComponentType.OVERTIME, base_monthly, "时薪×加班时长×倍数", ot)
        )


def _compute_hourly(inp: EmployeeInput, cfg: RuleConfig, res: PayrollResult) -> None:
    if inp.attendance is None:
        res.exceptions.append("缺少考勤数据，无法核算")
        return
    base = next((c for c in inp.structure if c.component_type == ComponentType.BASE), None)
    if base is None:
        res.exceptions.append("兼职缺少时薪（BASE 组件）")
        return
    if _has_perf(inp.structure) and inp.performance_coefficient is None:
        res.exceptions.append("缺少绩效系数，无法核算绩效")
    hourly_rate = base.amount
    worked_hours = inp.attendance.actual_days * cfg.hours_per_day  # v1：按出勤天数×每日工时
    res.lines.append(
        LineItem(
            base.code, ComponentType.BASE, hourly_rate, "时薪×工时", q(hourly_rate * worked_hours)
        )
    )
    if inp.attendance.overtime_hours > 0:
        ot = q(hourly_rate * inp.attendance.overtime_hours * cfg.overtime_multiplier)
        res.lines.append(
            LineItem("OVERTIME", ComponentType.OVERTIME, hourly_rate, "时薪×加班时长×倍数", ot)
        )
    for c in inp.structure:
        if c is base:
            continue
        _append_other(res, c, inp.performance_coefficient)


def _compute_labor(inp: EmployeeInput, cfg: RuleConfig, res: PayrollResult) -> None:
    # 劳务：结构组件按全额求和（扣款为负），不做出勤折算
    for c in inp.structure:
        _append_other(res, c, inp.performance_coefficient)


def compute(inp: EmployeeInput, cfg: RuleConfig | None = None) -> PayrollResult:
    cfg = cfg or RuleConfig()
    res = PayrollResult(inp.employee_id, inp.period, cfg.version)
    if not inp.structure:
        res.exceptions.append("缺少薪资结构，无法核算")

    if inp.employment_type == EmploymentType.FULL_TIME:
        _compute_full_time(inp, cfg, res)
    elif inp.employment_type == EmploymentType.PART_TIME_HOURLY:
        _compute_hourly(inp, cfg, res)
    else:  # LABOR
        _compute_labor(inp, cfg, res)

    res.gross = q(sum((li.amount for li in res.lines), ZERO))
    # 防御性守卫：净额为负（扣款超应发）不静默出账，标记待人工复核（v1；封顶/结转口径待业务定）
    if res.gross < 0:
        res.exceptions.append("核算净额为负（扣款超过应发），需人工复核")
    return res
