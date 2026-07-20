from decimal import Decimal

from app.models.comp import ComponentType
from app.models.employee import EmploymentType
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    RuleConfig,
    StructureComponent,
    compute,
)


def _sc(code, ctype, amount):
    return StructureComponent(code, ctype, Decimal(amount))


def _full(structure, att=None, perf=None, probation=Decimal("1")):
    return EmployeeInput(
        employee_id=1,
        period="2026-05",
        employment_type=EmploymentType.FULL_TIME,
        structure=structure,
        attendance=att,
        performance_coefficient=perf,
        probation_coefficient=probation,
    )


def _att(expected="22", actual="22", ot="0"):
    return Attendance(Decimal(expected), Decimal(actual), Decimal(ot))


def _line(res, code):
    return next(li for li in res.lines if li.code == code)


def test_full_attendance_full_pay():
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], _att()))
    assert not res.has_error
    assert _line(res, "BASE").amount == Decimal("5000.00")
    assert res.gross == Decimal("5000.00")


def test_partial_attendance_prorated():
    # 出勤 11/22 → 基本减半
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], _att("22", "11")))
    assert _line(res, "BASE").amount == Decimal("2500.00")


def test_allowance_flat_not_prorated():
    res = compute(
        _full(
            [
                _sc("BASE", ComponentType.BASE, "4000"),
                _sc("ALLOW", ComponentType.ALLOWANCE, "600"),
            ],
            _att("22", "11"),
        )
    )
    assert _line(res, "BASE").amount == Decimal("2000.00")  # 折算
    assert _line(res, "ALLOW").amount == Decimal("600.00")  # 全额


def test_performance_by_coefficient():
    res = compute(
        _full(
            [_sc("PERF", ComponentType.PERFORMANCE, "2000")],
            _att(),
            perf=Decimal("1.2"),
        )
    )
    assert _line(res, "PERF").amount == Decimal("2400.00")


def test_deduction_is_negative():
    res = compute(
        _full(
            [
                _sc("BASE", ComponentType.BASE, "5000"),
                _sc("FINE", ComponentType.DEDUCTION, "200"),
            ],
            _att(),
        )
    )
    assert _line(res, "FINE").amount == Decimal("-200.00")
    assert res.gross == Decimal("4800.00")


def test_overtime_pay():
    # 基数 5220 / 21.75 / 8 = 30 元/时；10 时 × 1.5 = 450
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5220")], _att("22", "22", "10")))
    assert _line(res, "OVERTIME").amount == Decimal("450.00")


def test_rounding_half_up():
    # 5000 × (7/22) = 1590.909... → 1590.91（四舍五入，非银行家）
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], _att("22", "7")))
    assert _line(res, "BASE").amount == Decimal("1590.91")


def test_missing_attendance_is_error():
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], att=None))
    assert res.has_error
    assert any("考勤" in e for e in res.exceptions)


def test_zero_expected_days_is_error():
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], _att("0", "0")))
    assert res.has_error


def test_missing_performance_coefficient_is_error():
    res = compute(_full([_sc("PERF", ComponentType.PERFORMANCE, "2000")], _att()))
    assert res.has_error
    assert any("绩效" in e for e in res.exceptions)


def test_no_structure_is_error():
    res = compute(_full([], _att()))
    assert res.has_error


def test_determinism_same_input_same_output():
    inp = _full([_sc("BASE", ComponentType.BASE, "5000")], _att("22", "13"))
    a = compute(inp)
    b = compute(inp)
    assert a.gross == b.gross
    assert [li.amount for li in a.lines] == [li.amount for li in b.lines]


def test_attendance_ratio_capped_at_one():
    # 实出勤 > 应出勤 → 基本不超发（超出走加班）
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], _att("22", "25")))
    assert _line(res, "BASE").amount == Decimal("5000.00")


def test_hourly_worker_pay():
    inp = EmployeeInput(
        employee_id=2,
        period="2026-05",
        employment_type=EmploymentType.PART_TIME_HOURLY,
        structure=[_sc("HOUR", ComponentType.BASE, "25")],  # 时薪 25
        attendance=_att("22", "10", "4"),  # 10 天 × 8 时 = 80 时；加班 4 时
    )
    res = compute(inp)
    assert _line(res, "HOUR").amount == Decimal("2000.00")  # 25 × 80
    assert _line(res, "OVERTIME").amount == Decimal("150.00")  # 25 × 4 × 1.5
    assert res.gross == Decimal("2150.00")


def test_hourly_missing_base_is_error():
    inp = EmployeeInput(
        employee_id=2,
        period="2026-05",
        employment_type=EmploymentType.PART_TIME_HOURLY,
        structure=[_sc("ALLOW", ComponentType.ALLOWANCE, "100")],
        attendance=_att(),
    )
    res = compute(inp)
    assert res.has_error


def test_probation_coefficient_applied():
    res = compute(
        _full([_sc("BASE", ComponentType.BASE, "5000")], _att(), probation=Decimal("0.8"))
    )
    assert _line(res, "BASE").amount == Decimal("4000.00")


def test_custom_overtime_multiplier():
    cfg = RuleConfig(overtime_multiplier=Decimal("2"))
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5220")], _att("22", "22", "10")), cfg)
    assert _line(res, "OVERTIME").amount == Decimal("600.00")  # 30 × 10 × 2


# ---------------- 复核修复 ----------------
def test_deduction_entered_negative_still_deducts():
    # 复核修复：扣款误录成负数，用 -abs 仍应扣减而非加发
    res = compute(
        _full(
            [
                _sc("BASE", ComponentType.BASE, "5000"),
                _sc("FINE", ComponentType.DEDUCTION, "-200"),  # 误录负数
            ],
            _att(),
        )
    )
    assert _line(res, "FINE").amount == Decimal("-200.00")
    assert res.gross == Decimal("4800.00")


def test_negative_gross_flagged_as_error():
    # 复核修复：扣款超应发→净额为负→标记异常阻断出账
    res = compute(
        _full(
            [
                _sc("BASE", ComponentType.BASE, "2000"),
                _sc("FINE", ComponentType.DEDUCTION, "5000"),
            ],
            _att(),
        )
    )
    assert res.gross == Decimal("-3000.00")
    assert res.has_error
    assert any("为负" in e for e in res.exceptions)


def test_hourly_performance_applies_coefficient():
    # 复核修复：兼职绩效也按系数，不再全额发放
    inp = EmployeeInput(
        employee_id=2,
        period="2026-05",
        employment_type=EmploymentType.PART_TIME_HOURLY,
        structure=[
            _sc("HOUR", ComponentType.BASE, "25"),
            _sc("PERF", ComponentType.PERFORMANCE, "500"),
        ],
        attendance=_att("22", "10"),
        performance_coefficient=Decimal("0.6"),
    )
    res = compute(inp)
    assert _line(res, "PERF").amount == Decimal("300.00")  # 500 × 0.6


def test_probation_label_omitted_when_coefficient_one():
    res = compute(_full([_sc("BASE", ComponentType.BASE, "5000")], _att()))
    assert _line(res, "BASE").formula == "月额×出勤比"  # 系数为 1 不标注试用
    res2 = compute(
        _full([_sc("BASE", ComponentType.BASE, "5000")], _att(), probation=Decimal("0.8"))
    )
    assert "试用系数" in _line(res2, "BASE").formula
