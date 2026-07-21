from datetime import date
from decimal import Decimal

import pytest

from app.models.comp import AllowanceKind, ComponentType
from app.models.employee import Department, EmploymentType
from app.payroll.engine import (
    Attendance,
    EmployeeInput,
    RuleConfig,
    StatutoryHoliday,
    StructureComponent,
    compute,
)
from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    DerivedIncomeRule,
    PayrollPolicyContext,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
)


def _sc(code, ctype, amount, kind=None, **flags):
    return StructureComponent(
        code,
        ctype,
        Decimal(amount),
        kind,
        taxable=flags.get("taxable", True),
        in_social_base=flags.get("in_social_base", False),
        in_housing_base=flags.get("in_housing_base", False),
        prorate_by_attendance=flags.get("prorate_by_attendance", False),
    )


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
    values = dict(
        employee_id=1,
        period=kw.get("period", "2026-05"),
        days_in_month=Decimal(days),
        employment_type=kw.get("employment_type", EmploymentType.FULL_TIME),
        department=dept,
        is_special_position=special,
        structure=structure,
        attendance=att,
        performance_coefficient=kw.get("performance_coefficient"),
        payroll_policy=kw.get("payroll_policy"),
        tax_employment_months=kw.get("tax_employment_months"),
        monthly_special_deduction=Decimal(kw.get("monthly_special_deduction", "0")),
        statutory_holiday_days=Decimal(kw.get("holidays", "0")),
        statutory_holidays=kw.get("statutory_holidays", ()),
        holiday_eligible=kw.get("holiday_eligible", True),
        holiday_calendar_finalized=kw.get("holiday_calendar_finalized", True),
        is_new_employee=kw.get("new", False),
        is_hire_or_leave_month=kw.get("hire_leave", False),
        hire_date=kw.get("hire_date"),
        leave_date=kw.get("leave_date"),
        prev_makeup=Decimal(kw.get("makeup", "0")),
        prev_deduct=Decimal(kw.get("deduct", "0")),
        prev_makeup_taxable=kw.get("makeup_taxable"),
        prev_makeup_in_social_base=kw.get("makeup_social"),
        prev_makeup_in_housing_base=kw.get("makeup_housing"),
        prev_deduct_taxable=kw.get("deduct_taxable"),
        prev_deduct_in_social_base=kw.get("deduct_social"),
        prev_deduct_in_housing_base=kw.get("deduct_housing"),
        prior_carry_forward=Decimal(kw.get("prior_carry_forward", "0")),
        prior_deferred_deductions=Decimal(kw.get("prior_deferred_deductions", "0")),
        prior_deferred_deposit=Decimal(kw.get("prior_deferred_deposit", "0")),
        source_exceptions=kw.get("source_exceptions", ()),
    )
    if "probation_end" in kw:
        values["probation_end"] = kw["probation_end"]
    return EmployeeInput(**values)


def _line(res, code):
    return next((li for li in res.lines if li.code == code), None)


def _comp(amount="5220"):
    return _sc("COMP", ComponentType.COMPREHENSIVE, amount)


def _performance_policy() -> PayrollPolicyContext:
    rules = []
    for kind in ContributionKind:
        rate = (
            Decimal("0.10")
            if kind in {ContributionKind.PENSION, ContributionKind.HOUSING}
            else Decimal("0")
        )
        rules.append(
            ContributionRule(
                kind=kind,
                employee_rate=rate,
                employer_rate=Decimal("0"),
                base_min=Decimal("0"),
                base_max=None,
            )
        )
    return PayrollPolicyContext(
        policy_id=1,
        city="广州",
        effective_from=date(2026, 1, 1),
        social_policy=SocialInsurancePolicyInput(city="广州", rules=tuple(rules)),
        tax_policy=TaxPolicyInput(
            monthly_basic_deduction=Decimal("0"),
            brackets=(
                TaxBracket(upper_bound=None, rate=Decimal("0"), quick_deduction=Decimal("0")),
            ),
        ),
    )


def _derived_income_policy() -> PayrollPolicyContext:
    """A complete policy whose derived earnings and both contribution sides are active."""
    rules = []
    for kind in ContributionKind:
        employee_rate = (
            Decimal("0.10")
            if kind in {ContributionKind.PENSION, ContributionKind.HOUSING}
            else Decimal("0")
        )
        employer_rate = (
            Decimal("0.20")
            if kind == ContributionKind.PENSION
            else Decimal("0.05") if kind == ContributionKind.HOUSING else Decimal("0")
        )
        rules.append(
            ContributionRule(
                kind=kind,
                employee_rate=employee_rate,
                employer_rate=employer_rate,
                base_min=Decimal("0"),
                base_max=None,
            )
        )
    return PayrollPolicyContext(
        policy_id=2,
        city="广州",
        effective_from=date(2026, 1, 1),
        social_policy=SocialInsurancePolicyInput(city="广州", rules=tuple(rules)),
        tax_policy=TaxPolicyInput(
            monthly_basic_deduction=Decimal("0"),
            brackets=(
                TaxBracket(upper_bound=None, rate=Decimal("0.10"), quick_deduction=Decimal("0")),
            ),
        ),
        derived_income_rules=(
            DerivedIncomeRule(
                code="OVERTIME",
                taxable=True,
                in_social_base=True,
                in_housing_base=True,
            ),
            DerivedIncomeRule(
                code="HOLIDAY",
                taxable=True,
                in_social_base=True,
                in_housing_base=True,
            ),
        ),
    )


# ---------------- 实际出勤天数：两套标准 ----------------
def test_dining_actual_days_by_hours_div_9():
    # 厅面：出勤工时 189 ÷ 9 = 21 天
    res = compute(_inp([_comp()], _att("26", worked="189"), dept=Department.DINING))
    assert res.actual_attendance_days == Decimal("21.00")


def test_kitchen_actual_days_by_hours_div_9_5():
    # 厨房：出勤工时 190 ÷ 9.5 = 20 天
    res = compute(_inp([_comp()], _att("26", worked="190"), dept=Department.KITCHEN))
    assert res.actual_attendance_days == Decimal("20.00")


def test_v4_special_position_uses_approved_actual_days():
    res = compute(
        _inp(
            [_comp()],
            _att("26", actual="17.5", rest="4"),
            dept=Department.KITCHEN,
            special=True,
        )
    )

    assert res.actual_attendance_days == Decimal("17.50")


def test_v4_special_position_approval_overrides_hourly_employment_type():
    res = compute(
        _inp(
            [_comp()],
            _att("26", actual="6.5", worked="80", rest="4"),
            dept=Department.KITCHEN,
            special=True,
            employment_type=EmploymentType.PART_TIME_HOURLY,
        )
    )

    assert res.actual_attendance_days == Decimal("6.50")


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_historical_special_position_attendance_semantics_are_preserved(version):
    res = compute(
        _inp(
            [_comp()],
            _att("26", actual="17.5", rest="4"),
            dept=Department.KITCHEN,
            special=True,
        ),
        RuleConfig(version=version),
    )

    assert res.actual_attendance_days == Decimal("22.00")


def test_other_non_special_uses_recorded_actual_days_not_special_position_formula():
    """OTHER 非特殊岗必须走明确的 actual_days 输入，不能伪装成特殊岗。"""
    res = compute(
        _inp(
            [_comp("5200")],
            _att("26", actual="3", rest="4"),
            dept=Department.OTHER,
            special=False,
        )
    )

    assert not res.has_error
    assert res.actual_attendance_days == Decimal("3.00")
    assert _line(res, "ATTEND_WAGE").amount == Decimal("600.00")


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


@pytest.mark.parametrize("expected_days", [Decimal("0"), Decimal("26")])
def test_part_time_hourly_uses_hourly_rate_and_worked_hours_without_expected_days(expected_days):
    """兼职小时工按时薪×工时计薪，不能被月薪应出勤分母拦截。"""
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=expected_days, worked_hours=Decimal("160")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
        )
    )

    assert res.has_error is False
    assert res.actual_attendance_days == Decimal("20.00")
    assert _line(res, "ATTEND_WAGE").amount == Decimal("4800.00")
    assert _line(res, "ATTEND_WAGE").formula == "时薪×实际工时"


def test_part_time_hourly_requires_worked_hours_not_expected_days():
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=None),
            employment_type=EmploymentType.PART_TIME_HOURLY,
        )
    )

    assert res.has_error
    assert any("兼职小时工" in error for error in res.exceptions)


@pytest.mark.parametrize("expected_days", [Decimal("-1"), Decimal("NaN")])
def test_part_time_hourly_rejects_invalid_expected_days(expected_days):
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=expected_days, worked_hours=Decimal("160")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
        )
    )

    assert res.has_error
    assert any("应出勤天数" in error for error in res.exceptions)
    assert res.gross == Decimal("0.00")


def test_part_time_hourly_rejects_non_finite_worked_hours():
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=Decimal("NaN")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
        )
    )

    assert res.has_error
    assert any("工时" in error for error in res.exceptions)
    assert res.gross == Decimal("0.00")


@pytest.mark.parametrize("hours_per_day", [Decimal("0"), Decimal("NaN")])
def test_part_time_hourly_invalid_standard_hours_blocks_without_dividing_by_zero(
    hours_per_day,
):
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=Decimal("160")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
        ),
        RuleConfig(hours_per_day=hours_per_day),
    )

    assert res.has_error
    assert any("标准工时" in error for error in res.exceptions)


def test_part_time_holiday_without_an_approved_hourly_rule_blocks_safely():
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=Decimal("160")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
            holidays="1",
        )
    )

    assert res.has_error
    assert any("法定节假日" in error for error in res.exceptions)
    assert _line(res, "HOLIDAY") is None


def test_part_time_hourly_ignores_calendar_holidays_before_employment_started():
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=Decimal("160")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
            hire_date=date(2026, 5, 2),
            statutory_holidays=(StatutoryHoliday(day=date(2026, 5, 1), worked=False),),
        )
    )

    assert res.has_error is False
    assert _line(res, "ATTEND_WAGE").amount == Decimal("4800.00")
    assert _line(res, "HOLIDAY") is None


@pytest.mark.parametrize("version", ["v2", "v3"])
def test_historical_part_time_snapshot_keeps_legacy_monthly_expected_days_gate(version):
    """历史 v2/v3 不能因新增时薪路径而改变既有复算结果。"""
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=Decimal("160")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
        ),
        RuleConfig(version=version),
    )

    assert res.has_error
    assert any("应出勤天数" in error for error in res.exceptions)


def test_missing_comprehensive_is_error():
    res = compute(_inp([_sc("ALLOW", ComponentType.ALLOWANCE, "500")], _att("26", worked="180")))
    assert res.has_error
    assert any("综合薪资" in e for e in res.exceptions)


# ---------------- 加班 ----------------
def test_overtime_wage():
    # 5220/21.75/8=30 元/时；10 时 ×1.5 = 450
    res = compute(_inp([_comp("5220")], _att("26", worked="234", ot="10")))
    assert _line(res, "OVERTIME").amount == Decimal("450.00")


@pytest.mark.parametrize(
    "config_overrides",
    [
        {"hours_per_day": Decimal("0")},
        {"hours_per_day": Decimal("NaN")},
        {"monthly_standard_days": Decimal("0")},
        {"monthly_standard_days": Decimal("NaN")},
    ],
)
def test_v4_invalid_overtime_rate_configuration_blocks_without_crashing(config_overrides):
    res = compute(
        _inp([_comp("5220")], _att("26", worked="234", ot="1")),
        RuleConfig(**config_overrides),
    )

    assert res.has_error
    assert any("加班时薪配置" in error for error in res.exceptions)
    assert _line(res, "OVERTIME") is None


def test_v3_policy_snapshot_keeps_derived_overtime_blocked():
    base_policy = _performance_policy()
    policy = PayrollPolicyContext(
        policy_id=base_policy.policy_id,
        city=base_policy.city,
        effective_from=base_policy.effective_from,
        social_policy=base_policy.social_policy,
        tax_policy=base_policy.tax_policy,
        derived_income_rules=(
            DerivedIncomeRule(
                code="OVERTIME",
                taxable=True,
                in_social_base=True,
                in_housing_base=True,
            ),
        ),
    )

    res = compute(
        _inp(
            [_comp("5220")],
            _att("26", worked="234", ot="1"),
            payroll_policy=policy,
        ),
        RuleConfig(version="v3"),
    )

    assert res.has_error
    assert any("cannot classify overtime" in error for error in res.exceptions)
    assert res.tax_state is None


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


@pytest.mark.parametrize(
    ("hire_date", "leave_date", "expected_holiday_wage"),
    [
        (date(2026, 5, 2), None, Decimal("461.54")),
        (None, date(2026, 5, 2), Decimal("461.54")),
        (date(2026, 5, 2), date(2026, 5, 2), Decimal("346.15")),
    ],
    ids=["hired-after-first-holiday", "left-before-last-holiday", "employed-one-day-only"],
)
def test_statutory_holiday_eligibility_is_evaluated_for_each_holiday_date(
    hire_date, leave_date, expected_holiday_wage
):
    """劳动关系只覆盖的法定日才计入；当天出勤三倍、未出勤一倍。"""
    res = compute(
        EmployeeInput(
            employee_id=1,
            period="2026-05",
            days_in_month=Decimal("31"),
            employment_type=EmploymentType.FULL_TIME,
            department=Department.DINING,
            is_special_position=False,
            structure=[_comp()],
            attendance=_att("26", worked="234"),
            # Small D1 input contract: each statutory date carries its own
            # attendance result so aggregate holiday_worked_days cannot apply
            # a triple rate to a date outside the employment relationship.
            statutory_holidays=(
                {"date": date(2026, 5, 1), "worked": False},
                {"date": date(2026, 5, 2), "worked": True},
                {"date": date(2026, 5, 3), "worked": False},
            ),
            hire_date=hire_date,
            leave_date=leave_date,
        )
    )

    assert _line(res, "HOLIDAY").amount == expected_holiday_wage


def test_statutory_holiday_outside_pay_period_blocks_calculation():
    """跨月的法定日数据不能静默混入当前工资月。"""
    res = compute(
        EmployeeInput(
            employee_id=1,
            period="2026-05",
            days_in_month=Decimal("31"),
            employment_type=EmploymentType.FULL_TIME,
            department=Department.DINING,
            is_special_position=False,
            structure=[_comp()],
            attendance=_att("26", worked="234"),
            statutory_holidays=(
                {"date": date(2026, 5, 1), "worked": True},
                {"date": date(2026, 6, 1), "worked": True},
            ),
        )
    )

    assert res.has_error
    assert any("法定" in error for error in res.exceptions)


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


def test_only_configured_allowances_are_prorated_by_attendance():
    res = compute(
        _inp(
            [
                _comp("5200"),
                _sc(
                    "MEAL",
                    ComponentType.ALLOWANCE,
                    "300",
                    AllowanceKind.FIXED,
                    prorate_by_attendance=True,
                ),
                _sc("PHONE", ComponentType.ALLOWANCE, "200", AllowanceKind.FLOATING),
            ],
            _att("26", actual="13"),
            special=True,
        )
    )

    assert _line(res, "MEAL").amount == Decimal("150.00")
    assert "实际计薪出勤天数/应出勤天数" in _line(res, "MEAL").formula
    assert _line(res, "PHONE").amount == Decimal("200.00")
    assert _line(res, "PHONE").formula == "全额"
    assert res.gross == Decimal("2950.00")


def test_prorated_allowance_uses_calculated_amount_in_policy_bases():
    res = compute(
        _inp(
            [
                _sc("COMP", ComponentType.COMPREHENSIVE, "1", taxable=False),
                _sc(
                    "MEAL",
                    ComponentType.ALLOWANCE,
                    "100",
                    AllowanceKind.FIXED,
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                    prorate_by_attendance=True,
                ),
            ],
            _att("26", actual="13"),
            special=True,
            payroll_policy=_performance_policy(),
            tax_employment_months=5,
        )
    )

    assert res.has_error is False
    assert res.tax_state is not None
    assert res.tax_state.current_taxable_income == Decimal("50.00")
    assert _line(res, "SOCIAL_PENSION_EMPLOYEE").amount == Decimal("-5.00")
    assert _line(res, "HOUSING_FUND_EMPLOYEE").amount == Decimal("-5.00")


def test_attendance_proration_is_capped_at_the_configured_allowance_amount():
    res = compute(
        _inp(
            [
                _comp("5200"),
                _sc(
                    "MEAL",
                    ComponentType.ALLOWANCE,
                    "300",
                    AllowanceKind.FIXED,
                    prorate_by_attendance=True,
                ),
            ],
            _att("26", actual="30"),
            special=True,
        )
    )

    assert _line(res, "MEAL").amount == Decimal("300.00")


def test_performance_components_apply_coefficient_individually_and_are_traceable():
    res = compute(
        _inp(
            [
                _comp("5200"),
                _sc("PERF_STORE", ComponentType.PERFORMANCE, "1000"),
                _sc("PERF_PERSON", ComponentType.PERFORMANCE, "500"),
            ],
            _att("26", worked="234"),
            performance_coefficient=Decimal("1.2"),
        )
    )

    assert res.has_error is False
    assert _line(res, "PERF_STORE").amount == Decimal("1200.00")
    assert _line(res, "PERF_PERSON").amount == Decimal("600.00")
    assert "绩效系数(1.2)" in _line(res, "PERF_STORE").formula
    assert res.gross == Decimal("7000.00")


def test_performance_components_default_to_a_neutral_coefficient_when_no_record_exists():
    res = compute(
        _inp(
            [_comp("5200"), _sc("PERF", ComponentType.PERFORMANCE, "1000")],
            _att("26", worked="234"),
        )
    )

    assert res.has_error is False
    assert _line(res, "PERF").amount == Decimal("1000.00")
    assert res.gross == Decimal("6200.00")


def test_performance_component_flags_flow_into_policy_bases():
    res = compute(
        _inp(
            [
                _sc("COMP", ComponentType.COMPREHENSIVE, "1", taxable=False),
                _sc(
                    "PERF",
                    ComponentType.PERFORMANCE,
                    "1000",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
            ],
            _att("26", worked="0"),
            performance_coefficient=Decimal("1.2"),
            payroll_policy=_performance_policy(),
            tax_employment_months=5,
        )
    )

    assert res.has_error is False
    assert res.tax_state is not None
    assert res.tax_state.current_taxable_income == Decimal("1200.00")
    assert _line(res, "SOCIAL_PENSION_EMPLOYEE").amount == Decimal("-120.00")
    assert _line(res, "HOUSING_FUND_EMPLOYEE").amount == Decimal("-120.00")


def test_probation_uses_the_configured_neutral_default_and_records_the_factor():
    res = compute(
        _inp(
            [_comp("5200")],
            _att("26", worked="234"),
            probation_end=date(2026, 5, 31),
        )
    )

    assert res.has_error is False
    assert _line(res, "ATTEND_WAGE").amount == Decimal("5200.00")
    assert "试用期系数(1)" in _line(res, "ATTEND_WAGE").formula


def test_probation_coefficient_is_configurable_without_changing_the_default_policy():
    res = compute(
        _inp(
            [_comp("5200")],
            _att("26", worked="234"),
            probation_end=date(2026, 5, 31),
        ),
        RuleConfig(probation_coefficient=Decimal("0.8")),
    )

    assert _line(res, "ATTEND_WAGE").amount == Decimal("4160.00")
    assert "试用期系数(0.8)" in _line(res, "ATTEND_WAGE").formula


def test_finished_probation_does_not_apply_the_trial_coefficient():
    res = compute(
        _inp(
            [_comp("5200")],
            _att("26", worked="234"),
            probation_end=date(2026, 4, 30),
        ),
        RuleConfig(probation_coefficient=Decimal("0.8")),
    )

    assert _line(res, "ATTEND_WAGE").amount == Decimal("5200.00")
    assert "试用期系数" not in _line(res, "ATTEND_WAGE").formula


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
    assert not res.has_error
    assert any("结转下月" in warning for warning in res.warnings)
    assert not any("结转下月" in error for error in res.exceptions)


def test_terminating_employee_carry_requires_final_settlement_before_lock():
    res = compute(
        _inp(
            [_comp("650")],
            _att("26", worked="45"),
            new=True,
            leave_date=date(2026, 5, 20),
        )
    )

    assert res.carry_forward > 0
    assert res.has_error
    assert any("final settlement" in error.lower() for error in res.exceptions)


def test_future_termination_does_not_block_current_month_carry():
    res = compute(
        _inp(
            [_comp("650")],
            _att("26", worked="45"),
            new=True,
            leave_date=date(2026, 6, 1),
        )
    )

    assert res.carry_forward > 0
    assert not res.has_error


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


def test_v4_policy_uses_explicit_prior_adjustment_classification():
    res = compute(
        _inp(
            [_sc("COMP", ComponentType.COMPREHENSIVE, "1000", taxable=True)],
            _att("26", worked="234"),
            makeup="100",
            deduct="20",
            makeup_taxable=True,
            makeup_social=False,
            makeup_housing=False,
            deduct_taxable=True,
            deduct_social=False,
            deduct_housing=False,
            payroll_policy=_derived_income_policy(),
            tax_employment_months=5,
        )
    )

    assert not any("prior-period adjustments" in error for error in res.exceptions)
    assert res.tax_state is not None
    assert res.tax_state.current_taxable_income == Decimal("1080.00")


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


def test_hourly_department_zero_worked_hours_is_valid_zero_attendance():
    # 真实零出勤不是缺失：厅面/厨房可以合法计为 0 天、0 元。
    res = compute(_inp([_comp()], _att("26", worked="0"), dept=Department.DINING))
    assert not res.has_error
    assert res.actual_attendance_days == Decimal("0.00")
    assert _line(res, "ATTEND_WAGE").amount == Decimal("0.00")


def test_hourly_department_missing_worked_hours_still_blocks_calculation():
    res = compute(
        _inp(
            [_comp()],
            Attendance(expected_days=Decimal("26"), worked_hours=None),
            dept=Department.DINING,
        )
    )

    assert res.has_error
    assert any("工时" in error for error in res.exceptions)


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


# ---------------- 历史版本和政策快照 ----------------
def test_v2_historical_snapshot_calculates_all_legacy_pay_lines():
    """历史重算仍保留 v2 的聚合假日、补贴、房补、押金和扣款语义。"""
    res = compute(
        _inp(
            [
                _comp("5220"),
                _sc("FIX", ComponentType.ALLOWANCE, "300", AllowanceKind.FIXED),
                _sc("FLOAT", ComponentType.ALLOWANCE, "200", AllowanceKind.FLOATING),
                _sc("HOUSING", ComponentType.HOUSING, "800"),
                _sc("FINE", ComponentType.DEDUCTION, "100"),
            ],
            _att("26", worked="234", ot="10", holiday="1"),
            holidays="2",
            makeup="100",
            deduct="50",
            new=True,
        ),
        RuleConfig(version="v2"),
    )

    assert res.has_error is False
    assert res.actual_attendance_days == Decimal("26.00")
    assert _line(res, "OVERTIME").amount == Decimal("450.00")
    assert _line(res, "HOLIDAY").amount == Decimal("461.54")
    assert _line(res, "FIX").amount == Decimal("300.00")
    assert _line(res, "FLOAT").amount == Decimal("200.00")
    assert _line(res, "HOUSING").amount == Decimal("800.00")
    assert _line(res, "PREV_MAKEUP").amount == Decimal("100.00")
    assert _line(res, "PREV_DEDUCT").amount == Decimal("-50.00")
    assert _line(res, "DEDUCTION").amount == Decimal("-100.00")
    assert res.gross == Decimal("7481.54")
    assert res.deposit == Decimal("600.00")
    assert res.net == Decimal("6781.54")


@pytest.mark.parametrize(
    ("department", "attendance", "expected_actual"),
    [
        (Department.KITCHEN, _att("26", worked="190"), Decimal("20.00")),
        (Department.OTHER, _att("26", actual="2", rest="4"), Decimal("22.00")),
    ],
    ids=["kitchen-hours", "other-legacy-rest-days"],
)
def test_v2_historical_attendance_mapping_is_preserved(department, attendance, expected_actual):
    """v2 的 OTHER 岗仍按应出勤减休息日复算，不能套用 v4 的 actual_days 输入。"""
    res = compute(
        _inp([_comp("5200")], attendance, dept=department),
        RuleConfig(version="v2"),
    )

    assert res.has_error is False
    assert res.actual_attendance_days == expected_actual


def test_v2_snapshot_with_missing_attendance_is_an_auditable_error():
    res = compute(_inp([_comp()], None), RuleConfig(version="v2"))

    assert res.has_error
    assert any("缺少考勤" in error for error in res.exceptions)


def test_v4_policy_calculates_derived_earnings_social_contributions_and_tax():
    """完整政策必须让加班/假日、补贴和房补进入各自批准的计税计费基数。"""
    res = compute(
        _inp(
            [
                _sc(
                    "COMP",
                    ComponentType.COMPREHENSIVE,
                    "1000",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
                _sc(
                    "PERF",
                    ComponentType.PERFORMANCE,
                    "100",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
                _sc(
                    "FIX",
                    ComponentType.ALLOWANCE,
                    "50",
                    AllowanceKind.FIXED,
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
                _sc(
                    "FLOAT",
                    ComponentType.ALLOWANCE,
                    "25",
                    AllowanceKind.FLOATING,
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
                _sc(
                    "HOUSING",
                    ComponentType.HOUSING,
                    "100",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                ),
            ],
            _att("26", worked="234", ot="2", holiday="1"),
            holidays="1",
            payroll_policy=_derived_income_policy(),
            tax_employment_months=5,
        )
    )

    assert res.has_error is False
    assert res.tax_state is not None
    assert res.tax_state.current_taxable_income == Decimal("1638.39")
    assert res.tax_state.current_employee_contribution == Decimal("327.68")
    # Cumulative taxable income is reduced by the employee contribution first.
    assert res.tax_state.current_tax_withheld == Decimal("131.07")
    assert res.tax_state.employment_months_to_date == 5
    assert _line(res, "SOCIAL_PENSION_EMPLOYEE").amount == Decimal("-163.84")
    assert _line(res, "SOCIAL_PENSION_EMPLOYER").amount == Decimal("327.68")
    assert _line(res, "HOUSING_FUND_EMPLOYEE").amount == Decimal("-163.84")
    assert _line(res, "HOUSING_FUND_EMPLOYER").amount == Decimal("81.92")
    assert _line(res, "IIT_WITHHOLDING").amount == Decimal("-131.07")


@pytest.mark.parametrize(
    "statutory_holidays",
    [
        ("not-a-calendar-entry",),
        ({"date": "not-a-date", "worked": False},),
        ({"date": "2026-05-01", "worked": "yes"},),
        (
            {"date": "2026-05-01", "worked": False},
            {"date": "2026-05-01", "worked": True},
        ),
    ],
    ids=["not-a-mapping", "invalid-date", "non-boolean-worked", "duplicate-date"],
)
def test_calendar_validation_turns_malformed_holidays_into_auditable_errors(statutory_holidays):
    """Bad calendar rows must block the result instead of silently changing holiday pay."""
    res = compute(
        _inp(
            [_comp()],
            _att("26", worked="234"),
            holidays="1",
            statutory_holidays=statutory_holidays,
        )
    )

    assert res.has_error
    assert any("法定节假日" in error for error in res.exceptions)
    assert _line(res, "HOLIDAY") is None


def test_calendar_accepts_typed_and_iso_date_holidays_in_the_same_snapshot():
    """The immutable input contract supports both service objects and serialized dates."""
    res = compute(
        _inp(
            [_comp()],
            _att("26", worked="234"),
            statutory_holidays=(
                StatutoryHoliday(day=date(2026, 5, 1), worked=False),
                {"date": "2026-05-02", "worked": True},
            ),
        )
    )

    assert res.has_error is False
    assert res.statutory_holiday_days == Decimal("2.00")
    assert res.statutory_holiday_worked_days == Decimal("1.00")
    assert _line(res, "HOLIDAY").amount == Decimal("461.54")


def test_calendar_rejects_a_malformed_period_before_using_holiday_dates():
    res = compute(
        _inp(
            [_comp()],
            _att("26", worked="234"),
            period="2026-13",
            statutory_holidays=(StatutoryHoliday(day=date(2026, 5, 1), worked=True),),
        )
    )

    assert res.has_error
    assert any("计薪周期" in error for error in res.exceptions)


def test_invalid_probation_and_performance_coefficients_are_employee_level_errors():
    probation = compute(
        _inp(
            [_comp()],
            _att("26", worked="234"),
            probation_end=date(2026, 5, 31),
        ),
        RuleConfig(probation_coefficient=Decimal("-0.1")),
    )
    performance = compute(
        _inp(
            [_comp("5200"), _sc("PERF", ComponentType.PERFORMANCE, "100")],
            _att("26", worked="234"),
            performance_coefficient=Decimal("5.1"),
        )
    )

    assert any("试用期系数" in error for error in probation.exceptions)
    assert any("绩效系数" in error for error in performance.exceptions)
    assert _line(performance, "PERF").amount == Decimal("100.00")


def test_hourly_probation_factor_is_visible_in_the_pay_line_formula():
    res = compute(
        _inp(
            [_comp("30")],
            Attendance(expected_days=Decimal("0"), worked_hours=Decimal("8")),
            employment_type=EmploymentType.PART_TIME_HOURLY,
            probation_end=date(2026, 5, 31),
        ),
        RuleConfig(probation_coefficient=Decimal("0.5")),
    )

    assert res.has_error is False
    assert _line(res, "ATTEND_WAGE").amount == Decimal("120.00")
    assert "试用期系数(0.5)" in _line(res, "ATTEND_WAGE").formula


def test_policy_rejects_unclassified_components_and_missing_derived_treatment():
    """Policy payroll may not guess how raw components or overtime enter statutory bases."""
    res = compute(
        _inp(
            [
                _comp("1000"),
                _sc("BASE", ComponentType.BASE, "10", in_social_base=True),
                _sc("DEDUCT_BASE", ComponentType.DEDUCTION, "10", in_social_base=True),
            ],
            _att("26", worked="234", ot="1"),
            payroll_policy=_performance_policy(),
            tax_employment_months=5,
        )
    )

    assert res.has_error
    assert any("uncalculated component BASE" in error for error in res.exceptions)
    assert any("deduction component DEDUCT_BASE" in error for error in res.exceptions)
    assert any("derived-income treatment for OVERTIME" in error for error in res.exceptions)
    assert res.tax_state is None


def test_invalid_derived_income_policy_is_exposed_on_the_payroll_result():
    base_policy = _derived_income_policy()
    duplicate_overtime = DerivedIncomeRule(
        code="OVERTIME",
        taxable=True,
        in_social_base=True,
        in_housing_base=True,
    )
    policy = PayrollPolicyContext(
        policy_id=base_policy.policy_id,
        city=base_policy.city,
        effective_from=base_policy.effective_from,
        social_policy=base_policy.social_policy,
        tax_policy=base_policy.tax_policy,
        derived_income_rules=(duplicate_overtime, duplicate_overtime),
    )

    res = compute(
        _inp(
            [_comp("1000")],
            _att("26", worked="234"),
            payroll_policy=policy,
            tax_employment_months=5,
        )
    )

    assert res.has_error
    assert any("derived-income treatment is invalid" in error for error in res.exceptions)
    assert res.tax_state is None


def test_v3_policy_uses_snapshot_period_month_for_cumulative_tax():
    """v3 uses the historical period month rather than the v4 employment-month input."""
    performance_policy = _performance_policy()
    policy = PayrollPolicyContext(
        policy_id=performance_policy.policy_id,
        city=performance_policy.city,
        effective_from=performance_policy.effective_from,
        social_policy=performance_policy.social_policy,
        tax_policy=TaxPolicyInput(
            monthly_basic_deduction=Decimal("100"),
            brackets=(
                TaxBracket(upper_bound=None, rate=Decimal("0.10"), quick_deduction=Decimal("0")),
            ),
        ),
    )
    res = compute(
        _inp(
            [_sc("COMP", ComponentType.COMPREHENSIVE, "1000", taxable=True)],
            _att("26", worked="234"),
            payroll_policy=policy,
            # A v4-style value conflicts with the May snapshot. v3 must
            # derive five cumulative months from the immutable period instead.
            tax_employment_months=1,
        ),
        RuleConfig(version="v3"),
    )

    assert res.has_error is False
    assert res.tax_state is not None
    assert res.tax_state.employment_months_to_date is None
    assert res.tax_state.cumulative_tax_due == Decimal("50.00")
    assert _line(res, "IIT_WITHHOLDING").amount == Decimal("-50.00")


def test_v4_policy_without_employment_months_reports_an_auditable_calculation_error():
    res = compute(
        _inp(
            [_sc("COMP", ComponentType.COMPREHENSIVE, "1000", taxable=True)],
            _att("26", worked="234"),
            payroll_policy=_performance_policy(),
        )
    )

    assert res.has_error
    assert any("employment-month count" in error for error in res.exceptions)
    assert res.tax_state is None


def test_policy_carry_preserves_all_obligations_and_requires_manual_settlement():
    """A policy result with deferred statutory deductions cannot be silently locked."""
    res = compute(
        _inp(
            [
                _sc(
                    "COMP",
                    ComponentType.COMPREHENSIVE,
                    "100",
                    taxable=True,
                    in_social_base=True,
                    in_housing_base=True,
                )
            ],
            _att("26", worked="234"),
            payroll_policy=_performance_policy(),
            tax_employment_months=1,
            new=True,
            prior_carry_forward="50",
            prior_deferred_deductions="15",
            prior_deferred_deposit="20",
        )
    )

    assert res.carry_forward == Decimal("150.00")
    assert res.deferred_deductions == Decimal("35.00")
    assert res.deferred_deposit == Decimal("620.00")
    assert _line(res, "CARRY_FORWARD_WAGE").amount == Decimal("50.00")
    assert _line(res, "CARRY_FORWARD_DEDUCTION").amount == Decimal("-15.00")
    assert any("cannot be deferred" in error for error in res.exceptions)


def test_negative_net_pay_requires_review_when_no_deposit_carry_rule_applies():
    res = compute(
        _inp(
            [_comp("100"), _sc("FINE", ComponentType.DEDUCTION, "200")],
            _att("26", worked="234"),
        )
    )

    assert res.net == Decimal("-100.00")
    assert any("实发为负" in error for error in res.exceptions)


def test_unknown_rule_version_is_rejected_before_calculation():
    with pytest.raises(ValueError, match="Unsupported payroll rule version"):
        compute(_inp([_comp()], _att("26", worked="234")), RuleConfig(version="v99"))
