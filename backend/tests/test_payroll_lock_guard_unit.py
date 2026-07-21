"""工资已锁定时，源数据写入必须被统一阻断。"""

from datetime import date

import pytest

from app.payroll.guards import (
    PayrollSourceLockedError,
    assert_employee_history_mutable,
    assert_new_employee_cohort_mutable,
    assert_period_mutable,
    assert_structure_effective_date_mutable,
    first_affected_structure_period,
)


class _Scalars:
    def all(self) -> list[object]:
        return []


class _Result:
    def __init__(self, value: object | None) -> None:
        self.value = value

    def first(self) -> object | None:
        return self.value


class _Session:
    def __init__(self, result: object | None) -> None:
        self.result = result
        self.calls = 0
        self.statements: list[object] = []
        self.scalars_calls = 0

    def scalar(self, statement: object) -> object | None:
        self.calls += 1
        self.statements.append(statement)
        return self.result

    def execute(self, statement: object) -> _Result:
        self.statements.append(statement)
        if self.result is None:
            return _Result(None)
        # The guards now select the pair needed to distinguish an initial draft
        # from a reopened HR-correction round.  Existing non-null test fixtures
        # represent a non-draft calculation state.
        return _Result(("PENDING_STORE_CONFIRM", 1))

    def scalars(self, statement: object) -> _Scalars:
        self.scalars_calls += 1
        self.statements.append(statement)
        return _Scalars()


def test_period_guard_rejects_attendance_or_performance_changes_for_calculated_payroll() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_period_mutable(_Session(1), "2026-05")


def test_employee_history_guard_rejects_changes_after_any_calculated_result() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_employee_history_mutable(_Session(1), employee_id=7)


def test_new_employee_guard_rejects_backdated_or_unknown_hires_in_started_cohorts() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_new_employee_cohort_mutable(_Session(1), date(2026, 5, 1))

    # The payroll cohort treats a missing hire date as eligible for every
    # period, so it must be rejected once a payroll batch has started too.
    with pytest.raises(PayrollSourceLockedError):
        assert_new_employee_cohort_mutable(_Session(1), None)

    future = _Session(None)
    assert_new_employee_cohort_mutable(future, date(2027, 1, 1))
    assert "attendance_end" in str(future.statements[-1])


def test_structure_effective_date_uses_the_engine_period_start() -> None:
    assert first_affected_structure_period(date(2026, 5, 1)) == "2026-05"
    assert first_affected_structure_period(date(2026, 5, 20)) == "2026-06"
    assert first_affected_structure_period(date(2026, 12, 2)) == "2027-01"


def test_structure_guard_allows_future_effective_change_but_blocks_historical_change() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_structure_effective_date_mutable(
            _Session(1), employee_id=7, effective_from=date(2026, 5, 1)
        )

    future = _Session(None)
    assert_structure_effective_date_mutable(future, employee_id=7, effective_from=date(2027, 1, 1))
    assert "payroll_batch.period" in str(future.statements[-1])


def test_guards_freeze_direct_source_writes_after_any_calculated_batch() -> None:
    period_session = _Session(None)
    employee_session = _Session(None)
    structure_session = _Session(None)

    assert_period_mutable(period_session, "2026-05")
    assert_employee_history_mutable(employee_session, employee_id=7)
    assert_structure_effective_date_mutable(
        structure_session, employee_id=7, effective_from=date(2026, 5, 1)
    )

    for session in (period_session, employee_session, structure_session):
        assert any("FOR UPDATE" in str(statement) for statement in session.statements)

    # Employee and structure updates have no period parameter, so they must lock
    # candidate draft batches before examining calculated result history.
    assert employee_session.scalars_calls == 1
    assert structure_session.scalars_calls == 1
