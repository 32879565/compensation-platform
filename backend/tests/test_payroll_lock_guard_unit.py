"""工资已锁定时，源数据写入必须被统一阻断。"""

from datetime import date

import pytest

from app.payroll.guards import (
    PayrollSourceLockedError,
    assert_employee_history_mutable,
    assert_period_mutable,
    assert_structure_effective_date_mutable,
)


class _Session:
    def __init__(self, result: object | None) -> None:
        self.result = result
        self.calls = 0

    def scalar(self, _statement: object) -> object | None:
        self.calls += 1
        return self.result


def test_period_guard_rejects_attendance_or_performance_changes_for_locked_payroll() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_period_mutable(_Session(1), "2026-05")


def test_employee_history_guard_rejects_changes_after_any_locked_result() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_employee_history_mutable(_Session(1), employee_id=7)


def test_structure_guard_allows_future_effective_change_but_blocks_historical_change() -> None:
    with pytest.raises(PayrollSourceLockedError):
        assert_structure_effective_date_mutable(
            _Session(1), employee_id=7, effective_from=date(2026, 5, 1)
        )

    assert_structure_effective_date_mutable(
        _Session(None), employee_id=7, effective_from=date(2027, 1, 1)
    )
