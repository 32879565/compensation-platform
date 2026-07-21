"""Cross-router guards that preserve immutable payroll calculation inputs."""

from __future__ import annotations

from datetime import date
from typing import cast

from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session

from app.models.employee import Employee
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import PayrollResult


class PayrollSourceLockedError(Exception):
    """Raised when a write would alter an already-calculated payroll source."""


_PAYROLL_INPUT_LOCK_NAME = "compensation-payroll-input-v1"


def lock_payroll_input_mutation(session: Session) -> None:
    """Serialize payroll source writes with the calculation snapshot boundary.

    A batch row does not exist before a draft is created, so row locks alone
    cannot prevent a concurrent attendance/employee/structure write from
    slipping between cohort selection and input snapshotting.  PostgreSQL's
    transaction-scoped advisory lock is shared by all source writers and batch
    calculations.  Lightweight unit-test sessions intentionally no-op.
    """
    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return
    bind = get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return
    session.scalar(select(func.pg_advisory_xact_lock(func.hashtext(_PAYROLL_INPUT_LOCK_NAME))))


def first_affected_structure_period(effective_from: date) -> str:
    """Return the first payroll period whose calculation reads this change.

    The current engine selects an employee structure at the first calendar day
    of ``PayrollBatch.period``.  A mid-month effective-date record therefore
    applies from the following period; treating it as a current-period source
    correction would produce an auditable-but-no-op rerun.
    """
    year, month = effective_from.year, effective_from.month
    if effective_from.day != 1:
        if month == 12:
            year, month = year + 1, 1
        else:
            month += 1
    return f"{year:04d}-{month:02d}"


def first_affected_employee_structure_period(
    session: Session,
    *,
    employee_id: int,
    effective_from: date,
) -> str:
    """Account for a new hire whose first terms start during the hire month."""
    if effective_from.day == 1:
        return first_affected_structure_period(effective_from)
    hire_date = session.scalar(select(Employee.hire_date).where(Employee.id == employee_id))
    if (
        isinstance(hire_date, date)
        and hire_date.year == effective_from.year
        and hire_date.month == effective_from.month
        and effective_from <= hire_date
    ):
        return f"{effective_from.year:04d}-{effective_from.month:02d}"
    return first_affected_structure_period(effective_from)


def _status_and_version(row: object | None) -> tuple[BatchStatus, int] | None:
    if row is None:
        return None
    status, version = cast(tuple[BatchStatus | str, int], row)
    return BatchStatus(status), int(version)


def assert_period_mutable(session: Session, period: str) -> bool:
    """Return whether a permitted write is a reopened HR-correction write.

    Direct source writes are rejected once the batch has started calculation.
    A reopened batch returns to ``DRAFT`` to permit a controlled correction and
    recalculation; callers must then require ``payroll:correct`` when this
    function returns ``True``.
    """
    lock_payroll_input_mutation(session)
    row = session.execute(
        select(PayrollBatch.status, PayrollBatch.version)
        .where(PayrollBatch.period == period)
        .with_for_update()
    ).first()
    state = _status_and_version(row)
    if state is not None and state[0] != BatchStatus.DRAFT:
        raise PayrollSourceLockedError(
            "该薪资周期已开始核算，源数据只能通过薪资异议审批后调整并重新核算"
        )
    return state is not None and state[1] > 1


def assert_employee_history_mutable(
    session: Session,
    employee_id: int,
    *,
    hire_date: date | None = None,
    leave_date: date | None = None,
) -> bool:
    """Keep payroll-input master fields immutable once any result exists.

    An unlocked full rerun rebuilds its cohort from live employee records.  If
    a post-lock master-data edit were allowed, an unrelated attendance
    correction could silently rewrite the historical payroll basis or remove
    the employee from the next review round.  Future-effective employee
    changes therefore belong to the audited adjustment workflow instead.
    """
    lock_payroll_input_mutation(session)
    # Employee writes lack a payroll period.  Lock all batch rows before
    # inspecting result history so they share the run-batch ordering.
    session.scalars(select(PayrollBatch.id).with_for_update()).all()
    blocked = session.scalar(
        select(PayrollBatch.id)
        .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
        .where(
            PayrollResult.employee_id == employee_id,
        )
        .limit(1)
    )
    if blocked is not None:
        raise PayrollSourceLockedError("员工存在已核算工资结果，不能直接修改其历史计薪口径")
    if (
        _unrepresented_eligible_batch(
            session,
            employee_id=employee_id,
            hire_date=hire_date,
            leave_date=leave_date,
        )
        is not None
    ):
        raise PayrollSourceLockedError(
            "该变更会让员工进入缺少核算结果的历史薪资批次；请通过受审计的更正流程处理"
        )
    return False


def _unrepresented_eligible_batch(
    session: Session,
    *,
    employee_id: int | None,
    hire_date: date | None,
    leave_date: date | None,
) -> int | None:
    """Find a historical payroll cohort the employee was eligible for but lacks.

    A result snapshot makes later master-data edits safe for a locked batch.  It
    does not protect an employee who was omitted from that batch altogether:
    allowing a backdated hire/leave change would silently create an uncalculated
    payroll obligation.  Reopened drafts are included because they require an
    audited correction path, not a new untracked cohort member.
    """
    conditions = [
        or_(
            PayrollBatch.status != BatchStatus.DRAFT,
            PayrollBatch.version > 1,
        )
    ]
    if hire_date is not None:
        conditions.append(PayrollBatch.attendance_end >= hire_date)
    if leave_date is not None:
        conditions.append(PayrollBatch.attendance_start <= leave_date)
    if employee_id is not None:
        conditions.append(
            ~exists(
                select(PayrollResult.id).where(
                    PayrollResult.batch_id == PayrollBatch.id,
                    PayrollResult.employee_id == employee_id,
                )
            )
        )
    return session.scalar(select(PayrollBatch.id).where(*conditions).limit(1))


def assert_new_employee_cohort_mutable(session: Session, hire_date: date | None) -> None:
    """Reject a backdated new employee that would alter an already-run cohort."""
    lock_payroll_input_mutation(session)
    session.scalars(select(PayrollBatch.id).with_for_update()).all()
    if (
        _unrepresented_eligible_batch(
            session,
            employee_id=None,
            hire_date=hire_date,
            leave_date=None,
        )
        is not None
    ):
        if hire_date is None:
            raise PayrollSourceLockedError(
                "未填写入职日期会被视为可进入所有历史薪资批次；请填写不早于未结批次的入职日期"
            )
        raise PayrollSourceLockedError(
            "该入职日期会进入已开始或已锁定的薪资批次；请通过受审计的更正流程处理"
        )


def assert_structure_effective_date_mutable(
    session: Session, employee_id: int, effective_from: date
) -> bool:
    """Protect effective-dated compensation changes and identify correction rounds."""
    affected_period = first_affected_employee_structure_period(
        session,
        employee_id=employee_id,
        effective_from=effective_from,
    )
    lock_payroll_input_mutation(session)
    session.scalars(
        select(PayrollBatch.id).where(PayrollBatch.period >= affected_period).with_for_update()
    ).all()
    blocked = session.scalar(
        select(PayrollBatch.id)
        .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
        .where(
            PayrollResult.employee_id == employee_id,
            PayrollBatch.period >= affected_period,
            PayrollBatch.status != BatchStatus.DRAFT,
        )
        .limit(1)
    )
    if blocked is not None:
        raise PayrollSourceLockedError("该生效日期会影响已核算工资，只能走调整与重新核算流程")
    return (
        session.scalar(
            select(PayrollBatch.id)
            .join(PayrollResult, PayrollResult.batch_id == PayrollBatch.id)
            .where(
                PayrollResult.employee_id == employee_id,
                PayrollBatch.period >= affected_period,
                PayrollBatch.status == BatchStatus.DRAFT,
                PayrollBatch.version > 1,
            )
            .limit(1)
        )
        is not None
    )
