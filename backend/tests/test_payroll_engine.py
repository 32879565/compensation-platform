from decimal import Decimal

from app.models.comp import AllowanceKind, ComponentType
from app.models.employee import Department, EmploymentType
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    StructureComponent,
    compute,
)


def _sc(code, ctype, amount, kind=None):
    return StructureComponent(code, ctype, Decimal(amount), kind)


def _att(expected="26", **kw):
    return Attendance(
        expected_days=Decimal(expected),
        actual_days=Decimal(kw.get("actual", "0")),
        worked_hours=Decimal(kw.get("worked", "0")),
        rest_days=Decimal(kw.get("rest", "0")),
        overtime_hours=Decimal(kw.get("ot", "0")),
        holiday_worked_days=Decimal(kw.get("holiday", "0")),
    )


def _inp(structure, att, *, dept=Department.DINING, special=False, days="31", **kw):
    return EmployeeInput(
        employee_id=1,
        period="2026-05",
        days_in_month=Decimal(days),
        employment_type=EmploymentType.FULL_TIME,
        department=dept,
        is_special_position=special,
        structure=structure,
        attendance=att,
        statutory_holiday_days=Decimal(kw.get("holidays", "0")),
        holiday_eligible=kw.get("holiday_eligible", True),
        is_new_employee=kw.get("new", False),
        is_hire_or_leave_month=kw.get("hire_leave", False),
        prev_makeup=Decimal(kw.get("makeup", "0")),
        prev_deduct=Decimal(kw.get("deduct", "0")),
    )


def _line(res, code):
    return next((li for li in res.lines if li.code == code), None)


def _comp(amount="5220"):
    return _sc("COMP", ComponentType.COMPREHENSIVE, amount)


# ---------------- 实际出勤天数：两套标准 ----------------
def test_dining_actual_days_by_hours_div_9():
    # 厅面：出勤工时 189 ÷ 9 = 21 天
    res = compute(_inp([_comp()], _att("26", worked="189"), dept=Department.DINING))
    assert res.actual_attendance_days == Decimal("21.00")


def test_kitchen_actual_days_by_hours_div_9_5():
    # 厨房：出勤工时 190 ÷ 9.5 = 20 天
    res = compute(_inp([_comp()], _att("26", worked="190"), dept=Department.KITCHEN))
    assert res.actual_attendance_days == Decimal("20.00")


def test_special_position_actual_days_by_expected_minus_rest():
    # 特殊岗位：应出勤 26 − 休息 4 = 22 天
    res = compute(_inp([_comp()], _att("26", rest="4"), dept=Department.KITCHEN, special=True))
    assert res.actual_attendance_days == Decimal("22.00")


def test_actual_days_capped_at_days_in_month():
    # 工时极大 → 实际出勤上限当月天数 31
    res = compute(_inp([_comp()], _att("26", worked="900"), days="31"))
    assert res.actual_attendance_days == Decimal("31.00")


# ---------------- 出勤工资 ----------------
def test_attendance_wage_formula():
    # 综合薪资 5200 / 应出勤 26 × 实际 20（工时180÷9） = 4000
    res = compute(
        _inp([_sc("COMP", ComponentType.COMPREHENSIVE, "5200")], _att("26", worked="180"))
    )
    assert _line(res, "ATTEND_WAGE").amount == Decimal("4000.00")
    assert res.gross == Decimal("4000.00")


def test_attendance_wage_rounding():
    # 5000/26×21 = 4038.4615… → 4038.46
    res = compute(
        _inp([_sc("COMP", ComponentType.COMPREHENSIVE, "5000")], _att("26", worked="189"))
    )
    assert _line(res, "ATTEND_WAGE").amount == Decimal("4038.46")


def test_missing_comprehensive_is_error():
    res = compute(_inp([_sc("ALLOW", ComponentType.ALLOWANCE, "500")], _att("26", worked="180")))
    assert res.has_error
    assert any("综合薪资" in e for e in res.exceptions)


# ---------------- 加班 ----------------
def test_overtime_wage():
    # 5220/21.75/8=30 元/时；10 时 ×1.5 = 450
    res = compute(_inp([_comp("5220")], _att("26", worked="234", ot="10")))
    assert _line(res, "OVERTIME").amount == Decimal("450.00")


# ---------------- 法定节假日 ----------------
def test_holiday_worked_triple():
    # 3000/25 × (1×3) = 360（出勤 1 天，共 1 个法定日）
    res = compute(_inp([_comp()], _att("25", worked="225", holiday="1"), holidays="1"))
    assert _line(res, "HOLIDAY").amount == Decimal("360.00")


def test_holiday_not_worked_single():
    # 共 1 个法定日、出勤 0 → 3000/25 × (0×3 + 1×1) = 120
    res = compute(_inp([_comp()], _att("25", worked="225", holiday="0"), holidays="1"))
    assert _line(res, "HOLIDAY").amount == Decimal("120.00")


def test_holiday_not_eligible_when_hired_late():
    res = compute(
        _inp([_comp()], _att("25", worked="225", holiday="1"), holidays="1", holiday_eligible=False)
    )
    assert _line(res, "HOLIDAY") is None


# ---------------- 补贴 / 房补 ----------------
def test_fixed_and_floating_allowance():
    res = compute(
        _inp(
            [
                _comp(),
                _sc("FIX", ComponentType.ALLOWANCE, "300", AllowanceKind.FIXED),
                _sc("FLT", ComponentType.ALLOWANCE, "200", AllowanceKind.FLOATING),
            ],
            _att("26", worked="234"),
        )
    )
    assert _line(res, "FIX").amount == Decimal("300.00")
    assert _line(res, "FLT").amount == Decimal("200.00")


def test_housing_full_when_over_15_days():
    # 实际出勤 20>15 → 房补全额 800
    res = compute(
        _inp([_comp(), _sc("HB", ComponentType.HOUSING, "800")], _att("26", worked="180"))
    )
    assert _line(res, "HOUSING").amount == Decimal("800.00")


def test_housing_prorated_when_15_or_less():
    # 实际出勤 10≤15 → 房补 800×10/26 = 307.69
    res = compute(_inp([_comp(), _sc("HB", ComponentType.HOUSING, "800")], _att("26", worked="90")))
    assert _line(res, "HOUSING").amount == Decimal("307.69")


def test_housing_prorated_on_hire_leave_month():
    # 入离职当月按比例：800×20/26（即便>15）
    res = compute(
        _inp(
            [_comp(), _sc("HB", ComponentType.HOUSING, "800")],
            _att("26", worked="180"),
            hire_leave=True,
        )
    )
    assert _line(res, "HOUSING").amount == Decimal("615.38")  # 800×20/26


# ---------------- 押金 ----------------
def test_new_employee_deposit_deducted():
    res = compute(_inp([_comp("5200")], _att("26", worked="234"), new=True))  # 应发5200
    assert res.deposit == Decimal("600.00")
    assert res.net == Decimal("4600.00")


def test_new_employee_insufficient_wage_carried_forward():
    # 应发不足 600 → 当月不发、结转下月、押金不扣
    res = compute(_inp([_comp("650")], _att("26", worked="45"), new=True))  # 650/26×5=125
    assert res.deposit == Decimal("0.00")
    assert res.carry_forward > 0
    assert res.net == Decimal("0.00")
    assert any("结转下月" in e for e in res.exceptions)


# ---------------- 应发汇总 / 上月补发补扣 ----------------
def test_gross_sum_with_prev_makeup_and_deduct():
    res = compute(
        _inp(
            [_comp("5200"), _sc("FIX", ComponentType.ALLOWANCE, "300", AllowanceKind.FIXED)],
            _att("26", worked="234"),
            makeup="100",
            deduct="50",
        )
    )
    # 出勤 5200 + 固定 300 + 补发 100 − 补扣 50 = 5550
    assert res.gross == Decimal("5550.00")


def test_determinism():
    inp = _inp([_comp("5200")], _att("26", worked="180"))
    a, b = compute(inp), compute(inp)
    assert a.gross == b.gross and a.net == b.net


def test_missing_attendance_error():
    res = compute(_inp([_comp()], None))
    assert res.has_error


# ---------------- 复核修复 ----------------
def test_carry_forward_with_deduction_net_is_zero_not_negative():
    # 复核修复：结转月即便有扣款，实发=0 而非负数，不误报'实发为负'
    res = compute(
        _inp(
            [_comp("650"), _sc("FINE", ComponentType.DEDUCTION, "100")],
            _att("26", worked="45"),  # 650/26×5=125 < 600
            new=True,
        )
    )
    assert res.net == Decimal("0.00")
    assert res.carry_forward > 0
    assert not any("实发为负" in e for e in res.exceptions)


def test_deposit_affordability_uses_payable_not_gross():
    # 复核修复：应发620但扣款100→可支配520<600→不扣押金、结转，不产生负实发
    res = compute(
        _inp(
            [_comp("650"), _sc("FINE", ComponentType.DEDUCTION, "100")],
            _att("26", worked="234"),  # 650/26×26=650 应发
            new=True,
        )
    )
    # 应发650，扣款100，可支配550<600 → 结转，net=0
    assert res.deposit == Decimal("0.00")
    assert res.net == Decimal("0.00")


def test_hourly_department_missing_worked_hours_is_error():
    # 复核修复：厅面/厨房缺工时→报异常阻断（区分数据缺失与真实零出勤）
    res = compute(_inp([_comp()], _att("26", worked="0"), dept=Department.DINING))
    assert res.has_error
    assert any("工时" in e for e in res.exceptions)


def test_housing_hire_leave_capped_at_full():
    # 复核修复：入离职当月房补比例封顶全额（工时超月致 actual>expected 也不超发）
    res = compute(
        _inp(
            [_comp(), _sc("HB", ComponentType.HOUSING, "800")],
            _att("26", worked="280"),  # 280/9=31.11→cap31 > expected26
            hire_leave=True,
        )
    )
    assert _line(res, "HOUSING").amount == Decimal("800.00")  # 封顶全额
