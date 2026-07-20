"""无需数据库的 S13c 批次状态机关键安全规则测试。"""

import pytest

from app.models.employee import Department
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import BatchConfirmation, CompDispute, ConfirmStatus
from app.payroll.batch_service import BatchError, raise_dispute, unlock_batch


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(self, confirmations: list[BatchConfirmation] | None = None) -> None:
        self.confirmations = confirmations or []
        self.added: list[object] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        pass

    def scalars(self, _statement: object) -> _ScalarResult:
        return _ScalarResult(list(self.confirmations))


def test_unlock_resets_all_confirmation_scopes_for_a_new_review_round() -> None:
    batch = PayrollBatch(status=BatchStatus.LOCKED, version=1)
    confirmation = BatchConfirmation(status=ConfirmStatus.CONFIRMED)
    session = _Session([confirmation])

    unlock_batch(session, batch, user_id=7, reason="发现已锁定批次的源数据错误")

    assert batch.status == BatchStatus.PENDING_STORE_CONFIRM
    assert batch.version == 2
    assert confirmation.status == ConfirmStatus.PENDING
    assert confirmation.confirmed_by is None
    assert confirmation.confirmed_at is None


def test_locked_batch_cannot_accept_new_disputes() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.LOCKED)
    employee = type(
        "Employee", (), {"id": 1, "org_unit_id": 1, "department": Department.OTHER}
    )()
    session = _Session()

    with pytest.raises(BatchError, match="已锁定"):
        raise_dispute(session, batch, employee, "ATTEND_WAGE", "锁定后不能新增异议", user_id=9)

    assert session.added == []
