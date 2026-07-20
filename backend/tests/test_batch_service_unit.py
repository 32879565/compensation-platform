"""无需数据库的 S13c 批次状态机关键安全规则测试。"""

from datetime import UTC, date, datetime
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.attendance import AttendanceRecord
from app.models.comp import AllowanceKind, ComponentType
from app.models.employee import Department, Employee, EmploymentType
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import (
    BatchConfirmation,
    ConfirmStatus,
    DisputeStatus,
)
from app.payroll import batch_service
from app.payroll.batch_service import (
    BatchError,
    approve_batch,
    confirm_scope,
    lock_batch,
    raise_dispute,
    recompute_employee,
    reopen_batch,
    resolve_dispute,
    run_batch,
    unlock_batch,
)
from app.payroll.engine import Attendance, EmployeeInput, StructureComponent
from app.payroll.service import _attendance_input


class _ScalarResult:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def all(self) -> list[object]:
        return self._rows

    def first(self) -> object | None:
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(
        self,
        confirmations: list[BatchConfirmation] | None = None,
        *,
        scalar_rows: list[list[object]] | None = None,
        scalar_values: list[object] | None = None,
        objects: dict[tuple[type[object], int], object] | None = None,
    ) -> None:
        self.confirmations = confirmations or []
        self.scalar_rows = list(scalar_rows or [])
        self.scalar_values = list(scalar_values or [])
        self.objects = objects or {}
        self.added: list[object] = []
        self.refreshed: list[tuple[object, object | None]] = []

    def add(self, value: object) -> None:
        self.added.append(value)

    def flush(self) -> None:
        pass

    def refresh(self, value: object, *, with_for_update: object | None = None) -> None:
        self.refreshed.append((value, with_for_update))

    def scalars(self, _statement: object) -> _ScalarResult:
        if self.scalar_rows:
            return _ScalarResult(self.scalar_rows.pop(0))
        return _ScalarResult(list(self.confirmations))

    def scalar(self, statement: object) -> object:
        if "payroll_batch" in str(statement):
            # Most state-machine tests use an isolated batch with no adjacent
            # periods.  Dedicated helpers provide a neighboring batch when a
            # chronological guard is the behavior under test.
            return None
        if not self.scalar_values:
            raise AssertionError("Unexpected scalar query")
        return self.scalar_values.pop(0)

    def get(self, model: type[object], identifier: int) -> object | None:
        return self.objects.get((model, identifier))


class _LockSession(_Session):
    def __init__(self, *, result_count: int, errored_result_count: int) -> None:
        super().__init__()
        self.result_count = result_count
        self.errored_result_count = errored_result_count

    def scalar(self, statement: object) -> object:
        sql = str(statement)
        if "payroll_result" in sql:
            return self.errored_result_count if "has_error" in sql else self.result_count
        if "comp_dispute" in sql or "batch_confirmation" in sql:
            return 0
        if "now" in sql.lower():
            return datetime(2026, 5, 31, tzinfo=UTC)
        raise AssertionError(f"Unexpected scalar query: {sql}")


class _SubsequentBatchSession(_Session):
    """Unit-session double exposing a later batch while keeping disputes closed."""

    def __init__(self, subsequent_batch: PayrollBatch) -> None:
        super().__init__()
        self.subsequent_batch = subsequent_batch

    def scalar(self, statement: object) -> object:
        sql = str(statement)
        if "comp_dispute" in sql:
            return 0
        if "payroll_batch" in sql:
            return self.subsequent_batch.id
        raise AssertionError(f"Unexpected scalar query: {sql}")

    def scalars(self, statement: object) -> _ScalarResult:
        if "payroll_batch" in str(statement):
            return _ScalarResult([self.subsequent_batch])
        return super().scalars(statement)


def test_legacy_expected_days_reason_does_not_bypass_missing_schedule_provenance() -> None:
    record = AttendanceRecord(
        employee_id=1,
        period="2026-05",
        expected_days=Decimal("22"),
        expected_days_adjust_reason="legacy free-text reason",
        actual_days=Decimal("21"),
        worked_hours=Decimal("0"),
        rest_days=Decimal("0"),
        overtime_hours=Decimal("0"),
        holiday_worked_days=Decimal("0"),
    )

    _attendance, generated_days, rule_id, errors = _attendance_input(None, None, "2026-05", record)

    assert generated_days is None
    assert rule_id is None
    assert errors == (
        "Attendance expected days lack schedule provenance; "
        "HR must generate the schedule before payroll.",
    )


def test_unlock_resets_all_confirmation_scopes_for_a_new_review_round() -> None:
    batch = PayrollBatch(status=BatchStatus.LOCKED, version=1)
    confirmation = BatchConfirmation(status=ConfirmStatus.CONFIRMED)
    session = _Session([confirmation], scalar_values=[0])

    unlock_batch(session, batch, user_id=7, reason="发现已锁定批次的源数据错误")

    assert batch.status == BatchStatus.DRAFT
    assert batch.version == 2
    # Previous review-round rows remain immutable history; run_batch creates
    # fresh v2 confirmations from the new calculation scope.
    assert confirmation.status == ConfirmStatus.CONFIRMED


def test_unlock_serializes_history_changes_with_payroll_input_snapshots(monkeypatch) -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.LOCKED, version=1)
    session = _Session(scalar_values=[0])
    calls: list[object] = []
    monkeypatch.setattr(
        batch_service, "lock_payroll_input_mutation", lambda value: calls.append(value)
    )

    unlock_batch(session, batch, user_id=7, reason="Correct the locked policy history")

    assert calls == [session]


@pytest.mark.parametrize(
    "later_status",
    [BatchStatus.PENDING_STORE_CONFIRM, BatchStatus.LOCKED],
    ids=["later_calculated_review", "later_locked"],
)
def test_unlock_rejects_old_month_when_a_later_batch_has_started(
    later_status: BatchStatus,
) -> None:
    old_batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.LOCKED, version=1)
    later_batch = PayrollBatch(id=2, period="2026-06", status=later_status, version=1)
    session = _SubsequentBatchSession(later_batch)

    with pytest.raises(BatchError):
        unlock_batch(session, old_batch, user_id=7, reason="Correct May source data")

    assert old_batch.status == BatchStatus.LOCKED
    assert old_batch.version == 1


@pytest.mark.parametrize(
    "later_status",
    [BatchStatus.PENDING_STORE_CONFIRM, BatchStatus.LOCKED],
    ids=["later_calculated_review", "later_locked"],
)
def test_reopen_rejects_old_month_when_a_later_batch_has_started(
    later_status: BatchStatus,
) -> None:
    old_batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.PENDING_HR, version=1)
    later_batch = PayrollBatch(id=2, period="2026-06", status=later_status, version=1)
    session = _SubsequentBatchSession(later_batch)

    with pytest.raises(BatchError):
        reopen_batch(session, old_batch, user_id=7, reason="Correct May source data")

    assert old_batch.status == BatchStatus.PENDING_HR
    assert old_batch.version == 1


def test_locked_batch_cannot_accept_new_disputes() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.LOCKED)
    employee = type("Employee", (), {"id": 1, "org_unit_id": 1, "department": Department.OTHER})()
    session = _Session()

    with pytest.raises(BatchError, match="已锁定"):
        raise_dispute(session, batch, employee, "ATTEND_WAGE", "锁定后不能新增异议", user_id=9)

    assert session.added == []


def test_confirm_scope_requires_pending_store_confirmation_state() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.DRAFT)
    confirmation = BatchConfirmation(
        batch_id=1,
        org_unit_id=1,
        department=Department.OTHER,
        status=ConfirmStatus.PENDING,
    )
    session = _Session([confirmation])

    session.scalar_values = [datetime(2026, 5, 31, tzinfo=UTC)]

    with pytest.raises(BatchError):
        confirm_scope(session, batch, org_unit_id=1, department=Department.OTHER.value, user_id=7)

    assert confirmation.status == ConfirmStatus.PENDING


def test_confirm_scope_transitions_a_completed_review_round_to_pending_hr() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.PENDING_STORE_CONFIRM)
    confirmation = BatchConfirmation(
        batch_id=1,
        org_unit_id=1,
        department=Department.OTHER,
        status=ConfirmStatus.PENDING,
    )
    session = _Session([confirmation], scalar_values=[datetime(2026, 5, 31, tzinfo=UTC)])

    confirmed = confirm_scope(
        session, batch, org_unit_id=1, department=Department.OTHER.value, user_id=7
    )

    assert confirmed.status == ConfirmStatus.CONFIRMED
    assert batch.status == BatchStatus.PENDING_HR


def test_hr_approval_transitions_a_fully_confirmed_batch_to_confirmed() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.PENDING_HR)
    session = _Session()

    approve_batch(session, batch, user_id=7)

    assert batch.status == BatchStatus.CONFIRMED


def test_resolve_dispute_requires_has_dispute_batch_state() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.PENDING_STORE_CONFIRM)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    session = _Session(
        objects={(PayrollBatch, 1): batch},
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC)],
    )

    with pytest.raises(BatchError):
        resolve_dispute(
            session,
            dispute,
            decision=DisputeStatus.NEED_MORE,
            resolution="Need supporting attendance evidence.",
            approver_id=7,
        )

    assert dispute.status == DisputeStatus.OPEN


def test_resolve_dispute_locks_the_dispute_row_before_transitioning() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.HAS_DISPUTE)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    session = _Session(
        objects={(PayrollBatch, 1): batch},
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC)],
    )

    resolve_dispute(
        session,
        dispute,
        decision=DisputeStatus.NEED_MORE,
        resolution="Need supporting attendance evidence.",
        approver_id=7,
    )

    assert (batch, True) in session.refreshed
    assert (dispute, True) in session.refreshed


def test_approved_dispute_requires_a_nonempty_source_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.HAS_DISPUTE)
    employee = SimpleNamespace(id=2, org_unit_id=1, department=Department.OTHER)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    attendance = SimpleNamespace(
        expected_days=Decimal("22"),
        actual_days=Decimal("22"),
        worked_hours=Decimal("0"),
        rest_days=Decimal("0"),
        overtime_hours=Decimal("0"),
        holiday_worked_days=Decimal("0"),
    )
    session = _Session(
        scalar_rows=[[attendance]],
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC), 0],
        objects={(PayrollBatch, 1): batch, (Employee, 2): employee},
    )
    monkeypatch.setattr(
        batch_service,
        "recompute_employee",
        lambda *_args, **_kwargs: SimpleNamespace(
            version=2, gross=Decimal("5000"), net=Decimal("5000")
        ),
    )

    with pytest.raises(BatchError):
        resolve_dispute(
            session,
            dispute,
            decision=DisputeStatus.APPROVED,
            resolution="The attendance record was verified.",
            approver_id=7,
            attendance_changes={},
        )

    assert attendance.rest_days == Decimal("0")
    assert session.added == []


@pytest.mark.parametrize(
    "attendance_changes",
    [
        {"rest_days": "-1"},
        {"unknown": "1"},
        {"actual_days": "-1"},
        {"rest_days": "0"},
        {"rest_days": "1.234"},
    ],
)
def test_approved_dispute_rejects_invalid_or_noop_attendance_change(
    monkeypatch: pytest.MonkeyPatch,
    attendance_changes: dict[str, str],
) -> None:
    batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.HAS_DISPUTE)
    employee = SimpleNamespace(id=2, org_unit_id=1, department=Department.OTHER)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    attendance = SimpleNamespace(
        expected_days=Decimal("22"),
        actual_days=Decimal("22"),
        worked_hours=Decimal("0"),
        rest_days=Decimal("0"),
        overtime_hours=Decimal("0"),
        holiday_worked_days=Decimal("0"),
    )
    session = _Session(
        scalar_rows=[[attendance]],
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC), 0],
        objects={(PayrollBatch, 1): batch, (Employee, 2): employee},
    )
    monkeypatch.setattr(
        batch_service,
        "recompute_employee",
        lambda *_args, **_kwargs: SimpleNamespace(
            version=2, gross=Decimal("5000"), net=Decimal("5000")
        ),
    )

    with pytest.raises(BatchError):
        resolve_dispute(
            session,
            dispute,
            decision=DisputeStatus.APPROVED,
            resolution="The attendance record was verified.",
            approver_id=7,
            attendance_changes=attendance_changes,
        )

    assert attendance.rest_days == Decimal("0")
    assert session.added == []


def test_approved_dispute_records_applicant_and_resets_affected_confirmation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.HAS_DISPUTE)
    employee = SimpleNamespace(id=2, org_unit_id=1, department=Department.OTHER)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    attendance = SimpleNamespace(
        expected_days=Decimal("22"),
        actual_days=Decimal("22"),
        worked_hours=Decimal("0"),
        rest_days=Decimal("0"),
        overtime_hours=Decimal("0"),
        holiday_worked_days=Decimal("0"),
    )
    confirmation = BatchConfirmation(
        batch_id=1,
        org_unit_id=1,
        department=Department.OTHER,
        status=ConfirmStatus.DISPUTED,
        confirmed_by=4,
        confirmed_at=datetime(2026, 5, 30, tzinfo=UTC),
    )
    session = _Session(
        scalar_rows=[[attendance], [confirmation]],
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC), 0],
        objects={(PayrollBatch, 1): batch, (Employee, 2): employee},
    )
    monkeypatch.setattr(
        batch_service,
        "recompute_employee",
        lambda *_args, **_kwargs: SimpleNamespace(
            version=2,
            batch_version=1,
            gross=Decimal("4545.45"),
            net=Decimal("4545.45"),
            rule_version="v2",
            input_snapshot={"employee_id": 2, "period": "2026-05"},
            org_unit_id=1,
            department=Department.OTHER,
        ),
    )

    resolved = resolve_dispute(
        session,
        dispute,
        decision=DisputeStatus.APPROVED,
        resolution="The attendance record was verified.",
        approver_id=7,
        attendance_changes={"rest_days": "2"},
    )

    adjustments = [item for item in session.added if item.__class__.__name__ == "AdjustmentRecord"]
    adjustment = adjustments[0]
    assert resolved.status == DisputeStatus.APPROVED
    assert adjustment.applicant_id == 9
    assert attendance.rest_days == Decimal("2")
    assert confirmation.status == ConfirmStatus.PENDING
    assert confirmation.confirmed_by is None
    assert confirmation.confirmed_at is None
    assert batch.status == BatchStatus.PENDING_STORE_CONFIRM


def test_rejected_dispute_resets_affected_confirmation_for_a_new_review_round() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.HAS_DISPUTE)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    persisted_result = SimpleNamespace(
        org_unit_id=1,
        department=Department.OTHER,
        lines=[{"code": "ATTEND_WAGE"}],
    )
    confirmation = BatchConfirmation(
        batch_id=1,
        org_unit_id=1,
        department=Department.OTHER,
        status=ConfirmStatus.DISPUTED,
        confirmed_by=4,
        confirmed_at=datetime(2026, 5, 30, tzinfo=UTC),
    )
    session = _Session(
        scalar_rows=[[persisted_result], [confirmation]],
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC), 0],
        objects={(PayrollBatch, 1): batch},
    )

    resolve_dispute(
        session,
        dispute,
        decision=DisputeStatus.REJECTED,
        resolution="The original calculation is correct.",
        approver_id=7,
    )

    assert confirmation.status == ConfirmStatus.PENDING
    assert confirmation.confirmed_by is None
    assert confirmation.confirmed_at is None
    assert batch.status == BatchStatus.PENDING_STORE_CONFIRM


def test_resolving_one_of_multiple_disputes_keeps_scope_disputed() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.HAS_DISPUTE)
    dispute = SimpleNamespace(
        id=3,
        batch_id=1,
        employee_id=2,
        status=DisputeStatus.OPEN,
        salary_item="ATTEND_WAGE",
        raised_by=9,
    )
    persisted_result = SimpleNamespace(org_unit_id=1, department=Department.OTHER)
    confirmation = BatchConfirmation(
        batch_id=1,
        org_unit_id=1,
        department=Department.OTHER,
        status=ConfirmStatus.DISPUTED,
        confirmed_by=4,
        confirmed_at=datetime(2026, 5, 30, tzinfo=UTC),
    )
    session = _Session(
        scalar_rows=[[persisted_result], [confirmation]],
        scalar_values=[datetime(2026, 5, 31, tzinfo=UTC), 1],
        objects={(PayrollBatch, 1): batch},
    )

    resolve_dispute(
        session,
        dispute,
        decision=DisputeStatus.REJECTED,
        resolution="The other dispute remains open.",
        approver_id=7,
    )

    assert confirmation.status == ConfirmStatus.DISPUTED
    assert batch.status == BatchStatus.HAS_DISPUTE


def test_lock_requires_confirmed_batch_state() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.DRAFT)
    session = _LockSession(result_count=1, errored_result_count=0)

    with pytest.raises(BatchError):
        lock_batch(session, batch, user_id=7)

    assert batch.status == BatchStatus.DRAFT


def test_lock_requires_at_least_one_result() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.CONFIRMED)
    session = _LockSession(result_count=0, errored_result_count=0)

    with pytest.raises(BatchError):
        lock_batch(session, batch, user_id=7)

    assert batch.status == BatchStatus.CONFIRMED


def test_dispute_uses_persisted_result_scope_after_employee_transfer() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.PENDING_STORE_CONFIRM)
    employee = SimpleNamespace(id=2, org_unit_id=99, department=Department.DINING)
    persisted_result = SimpleNamespace(
        org_unit_id=1,
        department=Department.OTHER,
        lines=[{"code": "ATTEND_WAGE"}],
    )
    confirmation = BatchConfirmation(
        batch_id=1,
        org_unit_id=1,
        department=Department.OTHER,
        status=ConfirmStatus.PENDING,
    )
    session = _Session(scalar_rows=[[persisted_result], [confirmation]])

    raise_dispute(
        session,
        batch,
        employee,
        "ATTEND_WAGE",
        "Review the original batch result.",
        user_id=9,
    )

    assert confirmation.status == ConfirmStatus.DISPUTED


def test_dispute_rejects_a_line_without_a_source_correction_workflow() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.PENDING_STORE_CONFIRM)
    employee = SimpleNamespace(id=2, org_unit_id=1, department=Department.OTHER)
    persisted_result = SimpleNamespace(
        org_unit_id=1,
        department=Department.OTHER,
        lines=[{"code": "DEDUCTION"}],
    )
    session = _Session(scalar_rows=[[persisted_result]])

    with pytest.raises(BatchError, match="source-data correction workflow"):
        raise_dispute(
            session,
            batch,
            employee,
            "DEDUCTION",
            "The deduction needs review.",
            user_id=9,
        )

    assert session.added == []


def test_result_persists_rule_version_input_snapshot_and_batch_version() -> None:
    batch = PayrollBatch(id=1, version=3)
    employee = SimpleNamespace(id=2, org_unit_id=1, department=Department.OTHER)
    engine_result = SimpleNamespace(
        rule_version="v2",
        actual_attendance_days=Decimal("22"),
        gross=Decimal("5000"),
        deposit=Decimal("0"),
        net=Decimal("5000"),
        carry_forward=Decimal("0"),
        lines=[],
        exceptions=[],
        has_error=False,
    )
    snapshot = {"employee_id": 2, "period": "2026-05", "attendance": {"rest_days": "0"}}
    session = _Session()

    result = batch_service._write_result(
        session,
        batch,
        employee,
        engine_result,
        version=4,
        input_snapshot=snapshot,
    )

    assert result.batch_version == 3
    assert result.rule_version == "v2"
    assert result.input_snapshot == snapshot


def test_run_batch_persists_results_and_creates_snapshot_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.DRAFT, version=3)
    employee = SimpleNamespace(id=2, org_unit_id=1, department=Department.OTHER)
    engine_result = SimpleNamespace(
        rule_version="v2",
        actual_attendance_days=Decimal("22"),
        gross=Decimal("5000"),
        deposit=Decimal("0"),
        net=Decimal("5000"),
        carry_forward=Decimal("0"),
        lines=[],
        exceptions=[],
        has_error=False,
    )
    snapshot = {"employee_id": 2, "period": "2026-05"}
    session = _Session(scalar_values=[datetime(2026, 5, 31, tzinfo=UTC)])
    monkeypatch.setattr(
        batch_service, "_calculate_result", lambda *_args: (engine_result, snapshot)
    )

    count = run_batch(session, batch, [employee])

    persisted = next(item for item in session.added if item.__class__.__name__ == "PayrollResult")
    confirmation = next(
        item for item in session.added if item.__class__.__name__ == "BatchConfirmation"
    )
    assert count == 1
    assert persisted.batch_version == 3
    assert persisted.input_snapshot == snapshot
    assert confirmation.org_unit_id == 1
    assert confirmation.department == Department.OTHER
    assert batch.status == BatchStatus.PENDING_STORE_CONFIRM


def test_run_batch_rejects_an_empty_cohort_without_leaving_draft() -> None:
    batch = PayrollBatch(id=1, period="2026-05", status=BatchStatus.DRAFT)
    session = _Session()

    with pytest.raises(BatchError, match="no eligible employees"):
        run_batch(session, batch, [])

    assert batch.status == BatchStatus.DRAFT


def test_calculation_snapshot_captures_json_safe_engine_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine_input = EmployeeInput(
        employee_id=2,
        period="2026-05",
        days_in_month=Decimal("31"),
        employment_type=EmploymentType.FULL_TIME,
        department=Department.OTHER,
        is_special_position=False,
        structure=[
            StructureComponent(
                code="COMP",
                component_type=ComponentType.COMPREHENSIVE,
                amount=Decimal("5000"),
            ),
            StructureComponent(
                code="MEAL",
                component_type=ComponentType.ALLOWANCE,
                amount=Decimal("100"),
                allowance_kind=AllowanceKind.FIXED,
            ),
        ],
        attendance=Attendance(
            expected_days=Decimal("22"),
            actual_days=Decimal("22"),
            worked_hours=Decimal("176"),
            rest_days=Decimal("0"),
            overtime_hours=Decimal("0"),
            holiday_worked_days=Decimal("0"),
        ),
        performance_coefficient=Decimal("1"),
        statutory_holiday_days=Decimal("0"),
        prev_makeup=Decimal("10"),
        prev_deduct=Decimal("5"),
        prior_carry_forward=Decimal("125"),
        prior_deferred_deductions=Decimal("25"),
        prior_deferred_deposit=Decimal("600"),
    )
    monkeypatch.setattr(
        batch_service,
        "build_input",
        lambda *_args: (engine_input, [99]),
    )

    result, snapshot = batch_service._calculate_result(object(), object(), "2026-05")

    assert result.rule_version == "v4"
    assert snapshot == {
        "employee_id": 2,
        "period": "2026-05",
        "days_in_month": "31",
        "employment_type": "FULL_TIME",
        "department": "OTHER",
        "is_special_position": False,
        "hire_date": None,
        "leave_date": None,
        "generated_expected_days": None,
        "expected_days_rule_id": None,
        "attendance": {
            "expected_days": "22",
            "actual_days": "22",
            "worked_hours": "176",
            "rest_days": "0",
            "overtime_hours": "0",
            "holiday_worked_days": "0",
        },
        "performance_coefficient": "1",
        "is_new_employee": False,
        "is_hire_or_leave_month": False,
        "holiday_eligible": True,
        "statutory_holiday_days": "0",
        "statutory_holidays": [],
        "holiday_calendar_finalized": True,
        "source_exceptions": [],
        "prev_makeup": "10",
        "prev_deduct": "5",
        "prior_carry_forward": "125",
        "prior_deferred_deductions": "25",
        "prior_deferred_deposit": "600",
        "payroll_tax": None,
        "tax_withholding": None,
        "structure": [
            {
                "code": "COMP",
                "component_type": "COMPREHENSIVE",
                "amount": "5000",
                "allowance_kind": None,
                "taxable": True,
                "in_social_base": False,
                "in_housing_base": False,
            },
            {
                "code": "MEAL",
                "component_type": "ALLOWANCE",
                "amount": "100",
                "allowance_kind": "FIXED",
                "taxable": True,
                "in_social_base": False,
                "in_housing_base": False,
            },
        ],
        "missing_component_ids": [99],
    }
    assert result.has_error is True


def test_calculation_snapshot_preserves_daily_holiday_eligibility_inputs() -> None:
    """逐日法定假日的资格判断必须随结果快照保存，才能日后复算与审计。"""
    engine_input = EmployeeInput(
        employee_id=2,
        period="2026-05",
        days_in_month=Decimal("31"),
        employment_type=EmploymentType.FULL_TIME,
        department=Department.DINING,
        is_special_position=False,
        structure=[
            StructureComponent(
                code="COMP",
                component_type=ComponentType.COMPREHENSIVE,
                amount=Decimal("5000"),
            )
        ],
        attendance=Attendance(expected_days=Decimal("26"), worked_hours=Decimal("234")),
        statutory_holidays=(
            {"date": date(2026, 5, 1), "worked": False},
            {"date": date(2026, 5, 2), "worked": True},
        ),
        hire_date=date(2026, 5, 2),
        leave_date=date(2026, 5, 2),
    )

    snapshot = batch_service._input_snapshot(engine_input, [])

    assert snapshot["hire_date"] == "2026-05-02"
    assert snapshot["leave_date"] == "2026-05-02"
    assert snapshot["statutory_holidays"] == [
        {"date": "2026-05-01", "worked": False},
        {"date": "2026-05-02", "worked": True},
    ]


def test_recompute_uses_original_input_snapshot_after_employee_transfer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_input = EmployeeInput(
        employee_id=2,
        period="2026-05",
        days_in_month=Decimal("31"),
        employment_type=EmploymentType.FULL_TIME,
        department=Department.OTHER,
        is_special_position=False,
        structure=[
            StructureComponent(
                code="COMP",
                component_type=ComponentType.COMPREHENSIVE,
                amount=Decimal("5000"),
            )
        ],
        attendance=Attendance(
            expected_days=Decimal("22"), actual_days=Decimal("22"), rest_days=Decimal("0")
        ),
    )
    prior_result = SimpleNamespace(
        version=1,
        rule_version="v2",
        input_snapshot=batch_service._input_snapshot(original_input, []),
        org_unit_id=1,
        department=Department.OTHER,
    )
    batch = PayrollBatch(id=1, period="2026-05", version=1)
    transferred_employee = SimpleNamespace(id=2, org_unit_id=99, department=Department.DINING)
    session = _Session(scalar_rows=[[prior_result]])
    monkeypatch.setattr(
        batch_service,
        "_calculate_result",
        lambda *_args: (_ for _ in ()).throw(
            AssertionError("live employee input must not be used")
        ),
    )

    recomputed = recompute_employee(
        session,
        batch,
        transferred_employee,
        attendance_changes={"actual_days": Decimal("20")},
    )

    assert recomputed.version == 2
    assert recomputed.org_unit_id == 1
    assert recomputed.department == Department.OTHER
    # v2 OTHER employees used expected days minus rest days; changing the
    # later v3-only actual_days field must not rewrite the historical result.
    assert recomputed.gross == Decimal("5000.00")
    assert recomputed.input_snapshot["department"] == "OTHER"
    assert recomputed.input_snapshot["attendance"]["actual_days"] == "20"


def test_lock_rejects_batches_with_errored_results() -> None:
    batch = PayrollBatch(id=1, status=BatchStatus.CONFIRMED)
    session = _LockSession(result_count=1, errored_result_count=1)

    with pytest.raises(BatchError):
        lock_batch(session, batch, user_id=7)

    assert batch.status == BatchStatus.CONFIRMED
