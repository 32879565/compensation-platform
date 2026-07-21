"""HR-maintained statutory-holiday calendar and auditable day-level attendance."""

from __future__ import annotations

from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import (
    Principal,
    permission_org_scope_allows,
    resolve_permission_org_scope,
)
from app.core.urls import optional_http_url
from app.db.session import get_session
from app.models.employee import EmploymentType
from app.models.holiday import HolidayCalendarPeriod, HolidayWorkRecord, StatutoryHolidayDate
from app.models.payroll_batch import PayrollBatch
from app.models.payroll_result import AdjustmentRecord, PayrollResult
from app.payroll.guards import PayrollSourceLockedError, assert_period_mutable
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api/holiday-calendar", tags=["holiday-calendar"])

_PERIOD = r"^\d{4}-\d{2}$"


def _period_for_day(day: date) -> str:
    return f"{day.year:04d}-{day.month:02d}"


def _period_bounds_or_error(period: str) -> tuple[date, date]:
    """Validate calendar months as well as the route's YYYY-MM pattern."""
    try:
        year, month = (int(value) for value in period.split("-"))
        start = date(year, month, 1)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=422, detail="Period must be a valid calendar month."
        ) from exc
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _require_mutable_period(session: Session, period: str) -> bool:
    try:
        correction_round = assert_period_mutable(session, period)
    except PayrollSourceLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    return correction_round


def _require_calendar_definition_mutable_period(session: Session, period: str) -> None:
    """Reject reopened-round calendar edits that cannot be tied to one employee.

    Day-level holiday-work corrections have a reason, evidence, an adjustment
    record, and a rerun result.  Calendar-wide date and finalization changes do
    not yet have that audit workflow, so allowing them in a reopened batch
    would silently alter the calculated cohort.
    """
    if _require_mutable_period(session, period):
        raise HTTPException(
            status_code=409,
            detail=(
                "Holiday calendar definitions cannot be changed during a reopened "
                "payroll correction round; use the auditable employee holiday-work correction."
            ),
        )


def _reopened_batch_employee_or_error(
    session: Session, period: str, employee_id: int
) -> tuple[PayrollBatch, PayrollResult]:
    """Keep a direct holiday correction inside the original calculated cohort."""
    batch = session.scalars(
        select(PayrollBatch).where(PayrollBatch.period == period).with_for_update()
    ).one()
    prior_result = session.scalars(
        select(PayrollResult)
        .where(
            PayrollResult.batch_id == batch.id,
            PayrollResult.employee_id == employee_id,
        )
        .order_by(PayrollResult.batch_version.desc(), PayrollResult.version.desc())
        .limit(1)
    ).first()
    if prior_result is None:
        raise HTTPException(
            status_code=422,
            detail="The employee is outside the original reopened payroll cohort.",
        )
    return batch, prior_result


def _historical_result_uses_holiday(result: PayrollResult, holiday_date: date) -> bool:
    """Use the immutable payroll snapshot to reject corrections with no effect."""
    snapshot = result.input_snapshot
    if not isinstance(snapshot, dict):
        return False
    holidays = snapshot.get("statutory_holidays")
    if not isinstance(holidays, list):
        return False
    target = holiday_date.isoformat()
    return any(
        isinstance(holiday, dict) and str(holiday.get("date")) == target for holiday in holidays
    )


def _historical_employee_org_unit(
    session: Session, employee_id: int, period: str, current_org_unit_id: int | None
) -> int | None:
    """Resolve period scope from an immutable payroll result before current master data."""
    historical = session.execute(
        select(PayrollResult.org_unit_id)
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .where(
            PayrollBatch.period == period,
            PayrollResult.employee_id == employee_id,
        )
        .order_by(PayrollResult.batch_version.desc(), PayrollResult.version.desc())
        .limit(1)
    ).first()
    if historical is not None:
        return historical[0]
    return current_org_unit_id


def _org_scope_allows(org_scope: frozenset[int] | None, org_unit_id: int | None) -> bool:
    if org_scope is None:
        return True
    return org_unit_id is not None and org_unit_id in org_scope


class HolidayDateBody(BaseModel):
    holiday_date: date
    name: str = Field(min_length=1, max_length=64)
    eligible_employment_types: list[EmploymentType] = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def strip_name(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("法定节假日名称不能为空")
        return value

    @model_validator(mode="after")
    def no_duplicate_types(self) -> HolidayDateBody:
        if len(set(self.eligible_employment_types)) != len(self.eligible_employment_types):
            raise ValueError("适用用工类型不能重复")
        return self


class HolidayDateOut(BaseModel):
    holiday_date: date
    name: str
    eligible_employment_types: list[EmploymentType]


class CalendarPeriodOut(BaseModel):
    period: str
    is_finalized: bool
    finalized_by: int | None
    finalized_at: datetime | None

    model_config = {"from_attributes": True}


class HolidayWorkBody(BaseModel):
    worked: bool
    reason: str | None = Field(default=None, max_length=1000)
    evidence_url: str | None = Field(default=None, max_length=512)
    correction_reason: str | None = Field(default=None, max_length=1000)

    @field_validator("reason", "correction_reason", mode="before")
    @classmethod
    def strip_optional_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("evidence_url", mode="before")
    @classmethod
    def validate_evidence_url(cls, value: object) -> object:
        return optional_http_url(value)


class HolidayWorkOut(BaseModel):
    employee_id: int
    holiday_date: date
    worked: bool
    reason: str | None
    evidence_url: str | None

    model_config = {"from_attributes": True}


@router.get(
    "/employees/{employee_id}/work",
    response_model=list[HolidayWorkOut],
)
def list_holiday_work(
    employee_id: int,
    period: str = Query(..., pattern=_PERIOD),
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_READ)),
    session: Session = Depends(get_session),
) -> list[HolidayWorkRecord]:
    """List auditable day-level holiday attendance for one visible employee."""

    start, end = _period_bounds_or_error(period)
    org_scope = resolve_permission_org_scope(session, principal, Perm.ATTENDANCE_READ)
    statement = select(HolidayWorkRecord).where(
        HolidayWorkRecord.employee_id == employee_id,
        HolidayWorkRecord.holiday_date >= start,
        HolidayWorkRecord.holiday_date < end,
    )
    if org_scope is not None:
        statement = statement.where(HolidayWorkRecord.org_unit_id.in_(list(org_scope)))
    records = list(session.scalars(statement.order_by(HolidayWorkRecord.holiday_date)).all())
    if records:
        return records
    # An employee currently in scope may legitimately have no records. A
    # transferred employee with historical records outside the caller's scope
    # remains indistinguishable from a nonexistent employee.
    employee = EmployeeRepository(session, org_scope=org_scope).get(employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    return []


@router.get("/dates", response_model=list[HolidayDateOut])
def list_holidays(
    period: str = Query(..., pattern=_PERIOD),
    _principal: Principal = Depends(require_permission(Perm.HOLIDAY_CALENDAR_READ)),
    session: Session = Depends(get_session),
) -> list[HolidayDateOut]:
    start, end = _period_bounds_or_error(period)
    holidays = list(
        session.scalars(
            select(StatutoryHolidayDate)
            .where(
                StatutoryHolidayDate.holiday_date >= start,
                StatutoryHolidayDate.holiday_date < end,
            )
            .order_by(StatutoryHolidayDate.holiday_date)
        ).all()
    )
    return [
        HolidayDateOut(
            holiday_date=holiday.holiday_date,
            name=holiday.name,
            eligible_employment_types=[
                EmploymentType(value) for value in holiday.eligible_employment_types
            ],
        )
        for holiday in holidays
    ]


@router.put("/dates/{holiday_date}", response_model=HolidayDateOut)
def upsert_holiday(
    holiday_date: date,
    body: HolidayDateBody,
    principal: Principal = Depends(require_permission(Perm.HOLIDAY_CALENDAR_WRITE)),
    session: Session = Depends(get_session),
) -> HolidayDateOut:
    if holiday_date != body.holiday_date:
        raise HTTPException(status_code=422, detail="路径日期必须与请求体日期一致")
    period = _period_for_day(holiday_date)
    _require_calendar_definition_mutable_period(session, period)
    period_row = session.scalars(
        select(HolidayCalendarPeriod)
        .where(HolidayCalendarPeriod.period == period)
        .with_for_update()
    ).first()
    if period_row is not None and period_row.is_finalized:
        raise HTTPException(status_code=409, detail="已确认的法定日历不可直接修改，请先撤销确认")
    holiday = session.scalars(
        select(StatutoryHolidayDate)
        .where(StatutoryHolidayDate.holiday_date == holiday_date)
        .with_for_update()
    ).first()
    before = None
    if holiday is None:
        holiday = StatutoryHolidayDate(holiday_date=holiday_date, name=body.name)
        session.add(holiday)
    else:
        before = {
            "name": holiday.name,
            "eligible_employment_types": list(holiday.eligible_employment_types),
        }
        holiday.name = body.name
    holiday.eligible_employment_types = [value.value for value in body.eligible_employment_types]
    session.flush()
    audit.record(
        session,
        action="holiday_calendar.date.set",
        actor=(principal.user_id, principal.username),
        target_type="statutory_holiday_date",
        target_id=holiday.id,
        detail={
            "date": holiday_date.isoformat(),
            "before": before,
            "after": {
                "name": holiday.name,
                "eligible_employment_types": holiday.eligible_employment_types,
            },
        },
    )
    session.commit()
    return HolidayDateOut(
        holiday_date=holiday.holiday_date,
        name=holiday.name,
        eligible_employment_types=[
            EmploymentType(value) for value in holiday.eligible_employment_types
        ],
    )


@router.get("/periods/{period}", response_model=CalendarPeriodOut)
def get_calendar_period(
    period: str = Path(pattern=_PERIOD),
    _principal: Principal = Depends(require_permission(Perm.HOLIDAY_CALENDAR_READ)),
    session: Session = Depends(get_session),
) -> CalendarPeriodOut:
    _period_bounds_or_error(period)
    row = session.scalars(
        select(HolidayCalendarPeriod).where(HolidayCalendarPeriod.period == period)
    ).first()
    if row is None:
        return CalendarPeriodOut(
            period=period,
            is_finalized=False,
            finalized_by=None,
            finalized_at=None,
        )
    return CalendarPeriodOut.model_validate(row)


@router.post("/periods/{period}/finalize", response_model=CalendarPeriodOut)
def finalize_calendar_period(
    period: str = Path(pattern=_PERIOD),
    principal: Principal = Depends(require_permission(Perm.HOLIDAY_CALENDAR_WRITE)),
    session: Session = Depends(get_session),
) -> HolidayCalendarPeriod:
    _period_bounds_or_error(period)
    _require_calendar_definition_mutable_period(session, period)
    row = session.scalars(
        select(HolidayCalendarPeriod)
        .where(HolidayCalendarPeriod.period == period)
        .with_for_update()
    ).first()
    if row is None:
        row = HolidayCalendarPeriod(period=period)
        session.add(row)
    row.is_finalized = True
    row.finalized_by = principal.user_id
    row.finalized_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        action="holiday_calendar.period.finalize",
        actor=(principal.user_id, principal.username),
        target_type="holiday_calendar_period",
        target_id=row.id,
        detail={"period": period},
    )
    session.commit()
    return row


@router.post("/periods/{period}/unfinalize", response_model=CalendarPeriodOut)
def unfinalize_calendar_period(
    period: str = Path(pattern=_PERIOD),
    principal: Principal = Depends(require_permission(Perm.HOLIDAY_CALENDAR_WRITE)),
    session: Session = Depends(get_session),
) -> HolidayCalendarPeriod:
    _period_bounds_or_error(period)
    _require_calendar_definition_mutable_period(session, period)
    row = session.scalars(
        select(HolidayCalendarPeriod)
        .where(HolidayCalendarPeriod.period == period)
        .with_for_update()
    ).first()
    if row is None or not row.is_finalized:
        raise HTTPException(status_code=409, detail="该周期法定日历尚未确认")
    row.is_finalized = False
    row.finalized_by = None
    row.finalized_at = None
    audit.record(
        session,
        action="holiday_calendar.period.unfinalize",
        actor=(principal.user_id, principal.username),
        target_type="holiday_calendar_period",
        target_id=row.id,
        detail={"period": period},
    )
    session.commit()
    return row


@router.put("/employees/{employee_id}/work/{holiday_date}", response_model=HolidayWorkOut)
def set_holiday_work(
    employee_id: int,
    holiday_date: date,
    body: HolidayWorkBody,
    principal: Principal = Depends(require_permission(Perm.ATTENDANCE_WRITE)),
    session: Session = Depends(get_session),
) -> HolidayWorkRecord:
    period = _period_for_day(holiday_date)
    correction_round = _require_mutable_period(session, period)
    if correction_round and not body.correction_reason:
        raise HTTPException(status_code=422, detail="更正已解锁批次的法定日出勤必须填写更正原因")
    if correction_round and not body.evidence_url:
        raise HTTPException(status_code=422, detail="更正已解锁批次的法定日出勤必须上传证明附件")
    org_scope = resolve_permission_org_scope(session, principal, Perm.ATTENDANCE_WRITE)
    holiday = session.scalars(
        select(StatutoryHolidayDate).where(StatutoryHolidayDate.holiday_date == holiday_date)
    ).first()
    if holiday is None:
        raise HTTPException(status_code=422, detail="日期不在法定节假日日历中")
    record = session.scalars(
        select(HolidayWorkRecord)
        .where(
            HolidayWorkRecord.employee_id == employee_id,
            HolidayWorkRecord.holiday_date == holiday_date,
        )
        .with_for_update()
    ).first()
    record_org_unit_id: int | None
    if record is None:
        employee = EmployeeRepository(session, org_scope=None).get(employee_id)
        if employee is None:
            raise HTTPException(status_code=404, detail="员工不存在或不可见")
        record_org_unit_id = _historical_employee_org_unit(
            session, employee_id, period, employee.org_unit_id
        )
    else:
        record_org_unit_id = record.org_unit_id
    if not _org_scope_allows(org_scope, record_org_unit_id):
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    if correction_round and not permission_org_scope_allows(
        session,
        principal,
        Perm.PAYROLL_CORRECT,
        record_org_unit_id,
    ):
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    correction_batch: PayrollBatch | None = None
    correction_prior_result: PayrollResult | None = None
    if correction_round:
        correction_batch, correction_prior_result = _reopened_batch_employee_or_error(
            session, period, employee_id
        )
        if not _historical_result_uses_holiday(correction_prior_result, holiday_date):
            raise HTTPException(
                status_code=422,
                detail=(
                    "The holiday is not eligible on the employee's historical payroll path; "
                    "this correction cannot affect payroll"
                ),
            )
    before = {"record_exists": record is not None, "holiday_date": holiday_date.isoformat()}
    if record is not None:
        before.update(
            {
                "worked": record.worked,
                "reason": record.reason,
                "evidence_url": record.evidence_url,
            }
        )
    if correction_round and bool(before.get("worked", False)) == body.worked:
        raise HTTPException(
            status_code=422,
            detail="A reopened holiday correction must change whether the employee worked.",
        )
    if record is None:
        record = HolidayWorkRecord(
            employee_id=employee_id,
            org_unit_id=record_org_unit_id,
            holiday_date=holiday_date,
        )
        session.add(record)
    record.worked = body.worked
    record.reason = body.reason
    record.evidence_url = body.evidence_url
    record.recorded_by = principal.user_id
    record.recorded_at = datetime.now(UTC)
    session.flush()
    after = {
        "record_exists": True,
        "holiday_date": holiday_date.isoformat(),
        "org_unit_id": record.org_unit_id,
        "worked": record.worked,
        "reason": record.reason,
        "evidence_url": record.evidence_url,
    }
    if correction_round:
        if correction_batch is None:
            raise HTTPException(status_code=409, detail="已解锁薪资批次不包含该员工")
        session.add(
            AdjustmentRecord(
                batch_id=correction_batch.id,
                batch_version=correction_batch.version,
                employee_id=employee_id,
                dispute_id=None,
                item="HOLIDAY_WORK_SOURCE",
                before_value=before,
                after_value=after,
                reason=body.correction_reason or "",
                applicant_id=principal.user_id,
                approver_id=principal.user_id,
                attachment_url=body.evidence_url,
                recompute_result={
                    "status": "PENDING_RERUN",
                    "batch_version": correction_batch.version,
                },
            )
        )
    audit.record(
        session,
        action="holiday_work.set",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={
            "date": holiday_date.isoformat(),
            "before": before,
            "after": after,
            "correction": correction_round,
            "correction_reason": body.correction_reason if correction_round else None,
        },
    )
    session.commit()
    return record
