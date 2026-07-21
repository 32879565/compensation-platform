"""Deterministic expected-attendance generation from HR-maintained rules."""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.attendance import ExpectedAttendanceRule
from app.models.employee import Employee, requires_approved_attendance_days

_DAYS_QUANTUM = Decimal("0.01")


class ExpectedDaysError(ValueError):
    """A payroll-safe explanation for an absent or ambiguous schedule rule."""


@dataclass(frozen=True)
class GeneratedExpectedDays:
    rule_id: int
    days: Decimal


def period_bounds(period: str) -> tuple[date, date]:
    try:
        year, month = (int(value) for value in period.split("-"))
        start = date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise ExpectedDaysError("计薪周期格式无效") from exc
    return start, date(year, month, calendar.monthrange(year, month)[1])


def load_active_rules(session: Session, period: str) -> list[ExpectedAttendanceRule]:
    start, _ = period_bounds(period)
    return list(
        session.scalars(
            select(ExpectedAttendanceRule)
            .where(
                ExpectedAttendanceRule.is_active.is_(True),
                ExpectedAttendanceRule.effective_from <= start,
                (ExpectedAttendanceRule.effective_to.is_(None))
                | (ExpectedAttendanceRule.effective_to > start),
            )
            .order_by(ExpectedAttendanceRule.priority.desc(), ExpectedAttendanceRule.id)
        ).all()
    )


def _matches(rule: ExpectedAttendanceRule, employee: Employee) -> bool:
    effective_special_position = employee.is_special_position or requires_approved_attendance_days(
        employee.position_title
    )
    return (
        (rule.org_unit_id is None or rule.org_unit_id == employee.org_unit_id)
        and (rule.employment_type is None or rule.employment_type == employee.employment_type)
        and (rule.department is None or rule.department == employee.department)
        and (rule.position_title is None or rule.position_title == employee.position_title)
        and (
            rule.is_special_position is None
            or rule.is_special_position == effective_special_position
        )
    )


def _specificity(rule: ExpectedAttendanceRule) -> int:
    return sum(
        value is not None
        for value in (
            rule.org_unit_id,
            rule.employment_type,
            rule.department,
            rule.position_title,
            rule.is_special_position,
        )
    )


def _validated_rest_days(rule: ExpectedAttendanceRule) -> set[int]:
    rest_days = rule.weekly_rest_days
    if (
        not isinstance(rest_days, list)
        or any(not isinstance(value, int) or value < 0 or value > 6 for value in rest_days)
        or len(set(rest_days)) != len(rest_days)
    ):
        raise ExpectedDaysError(f"应出勤规则 {rule.id} 的每周休息日配置无效")
    if rule.monthly_expected_days is None and not rest_days:
        raise ExpectedDaysError(f"应出勤规则 {rule.id} 必须配置每周休息日或固定月应出勤")
    if rule.monthly_expected_days is not None and (
        rule.monthly_expected_days <= 0 or rule.monthly_expected_days > 31
    ):
        raise ExpectedDaysError(f"应出勤规则 {rule.id} 的固定月应出勤天数无效")
    return set(rest_days)


def _active_bounds(employee: Employee, period: str) -> tuple[date, date]:
    period_start, period_end = period_bounds(period)
    start = max(period_start, employee.hire_date or period_start)
    end = min(period_end, employee.leave_date or period_end)
    return start, end


def _generated_days(rule: ExpectedAttendanceRule, employee: Employee, period: str) -> Decimal:
    rest_days = _validated_rest_days(rule)
    period_start, period_end = period_bounds(period)
    active_start, active_end = _active_bounds(employee, period)
    if active_end < active_start:
        return Decimal("0")
    if rule.monthly_expected_days is not None:
        active_days = Decimal((active_end - active_start).days + 1)
        days_in_period = Decimal((period_end - period_start).days + 1)
        return (rule.monthly_expected_days * active_days / days_in_period).quantize(
            _DAYS_QUANTUM, rounding=ROUND_HALF_UP
        )
    workdays = 0
    current = active_start
    while current <= active_end:
        if current.weekday() not in rest_days:
            workdays += 1
        current += timedelta(days=1)
    return Decimal(workdays)


def resolve_expected_days(
    session: Session,
    employee: Employee,
    period: str,
    *,
    rules: list[ExpectedAttendanceRule] | None = None,
) -> GeneratedExpectedDays:
    rules_to_match = rules if rules is not None else load_active_rules(session, period)
    candidates = [rule for rule in rules_to_match if _matches(rule, employee)]
    if not candidates:
        raise ExpectedDaysError("未找到匹配员工岗位和用工类型的应出勤规则")
    candidates.sort(
        key=lambda rule: (rule.priority, _specificity(rule), rule.effective_from), reverse=True
    )
    best = candidates[0]
    best_rank = (best.priority, _specificity(best), best.effective_from)
    if (
        sum(
            (rule.priority, _specificity(rule), rule.effective_from) == best_rank
            for rule in candidates
        )
        > 1
    ):
        raise ExpectedDaysError("存在多个同优先级的应出勤规则，无法安全生成")
    return GeneratedExpectedDays(rule_id=best.id, days=_generated_days(best, employee, period))
