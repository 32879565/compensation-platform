"""Audited employee-level tax opening management for a safe mid-year rollout."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.employee import Employee
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_policy import EmployeeTaxYtdOpening
from app.models.payroll_result import PayrollResult
from app.payroll.guards import lock_payroll_input_mutation

router = APIRouter(prefix="/api/employees/{employee_id}/tax-ytd-openings", tags=["employee-tax"])


class TaxOpeningBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tax_year: int = Field(ge=2000, le=9999)
    through_period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    employment_months_to_date: int = Field(ge=0, le=12)
    taxable_income: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    employee_contribution: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    special_deduction: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    tax_withheld: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    evidence_ref: str = Field(min_length=1, max_length=512)

    @field_validator("evidence_ref")
    @classmethod
    def normalize_evidence_ref(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("evidence_ref must not be blank")
        return value

    @model_validator(mode="after")
    def through_period_belongs_to_tax_year(self) -> TaxOpeningBody:
        if int(self.through_period[:4]) != self.tax_year:
            raise ValueError("through_period must belong to tax_year")
        return self


class TaxOpeningOut(BaseModel):
    id: int
    employee_id: int
    tax_year: int
    revision: int
    through_period: str
    employment_months_to_date: int
    taxable_income: Decimal
    employee_contribution: Decimal
    special_deduction: Decimal
    tax_withheld: Decimal
    evidence_ref: str
    is_finalized: bool
    finalized_by: int | None
    finalized_at: datetime | None
    supersedes_id: int | None
    superseded_by: int | None
    superseded_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


def _employee_or_404(session: Session, employee_id: int) -> Employee:
    employee = session.get(Employee, employee_id)
    if employee is None:
        raise HTTPException(status_code=404, detail="Employee does not exist")
    return employee


def _out(opening: EmployeeTaxYtdOpening) -> TaxOpeningOut:
    return TaxOpeningOut.model_validate(opening)


def _period_start(value: str) -> date:
    year, month = (int(part) for part in value.split("-"))
    return date(year, month, 1)


def _next_period(value: str) -> str:
    start = _period_start(value)
    if start.month == 12:
        return f"{start.year + 1:04d}-01"
    return f"{start.year:04d}-{start.month + 1:02d}"


def _validate_opening_values_for_employee(
    *,
    tax_year: int,
    through_period: str,
    employment_months_to_date: int,
    employee: Employee,
) -> None:
    hire_date = employee.hire_date
    if hire_date is None:
        raise HTTPException(
            status_code=422, detail="Employee hire date is required for an audited tax opening"
        )
    period_start = _period_start(through_period)
    period_end = (
        date(period_start.year + 1, 1, 1)
        if period_start.month == 12
        else date(period_start.year, period_start.month + 1, 1)
    )
    if hire_date >= period_end:
        raise HTTPException(
            status_code=422, detail="An audited tax opening cannot predate the employee hire month"
        )
    employment_start = max(hire_date, date(tax_year, 1, 1))
    expected_months = (
        (period_start.year - employment_start.year) * 12
        + period_start.month
        - employment_start.month
        + 1
    )
    if employment_months_to_date != expected_months:
        raise HTTPException(
            status_code=422,
            detail="Opening employment-month count does not match its through period",
        )


def _validate_opening_for_employee(body: TaxOpeningBody, employee: Employee) -> None:
    _validate_opening_values_for_employee(
        tax_year=body.tax_year,
        through_period=body.through_period,
        employment_months_to_date=body.employment_months_to_date,
        employee=employee,
    )


def _history_transition_is_blocked(
    session: Session, *, employee_id: int, through_period: str
) -> bool:
    """Permit a new opening only when all affected history is reopened."""

    tax_year = _period_start(through_period).year
    year_start = f"{tax_year:04d}-01"
    next_year_start = f"{tax_year + 1:04d}-01"

    if (
        session.scalar(
            select(PayrollResult.id)
            .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
            .where(
                PayrollResult.employee_id == employee_id,
                PayrollBatch.period >= year_start,
                PayrollBatch.period <= through_period,
                PayrollBatch.status == BatchStatus.LOCKED,
                PayrollResult.batch_version == PayrollBatch.version,
            )
            .with_for_update()
            .limit(1)
        )
        is not None
    ):
        return True

    first_affected_period = _next_period(through_period)
    if first_affected_period >= next_year_start:
        return False
    batches = session.scalars(
        select(PayrollBatch)
        .where(
            PayrollBatch.period >= first_affected_period,
            PayrollBatch.period < next_year_start,
        )
        .order_by(PayrollBatch.period)
        .with_for_update()
    ).all()
    for batch in batches:
        if batch.status == BatchStatus.DRAFT:
            continue
        return True
    return False


def _opening_or_404(session: Session, employee_id: int, opening_id: int) -> EmployeeTaxYtdOpening:
    opening = session.scalars(
        select(EmployeeTaxYtdOpening)
        .where(
            EmployeeTaxYtdOpening.id == opening_id,
            EmployeeTaxYtdOpening.employee_id == employee_id,
        )
        .with_for_update()
    ).first()
    if opening is None:
        raise HTTPException(status_code=404, detail="Tax opening does not exist")
    return opening


def _apply_opening_body(opening: EmployeeTaxYtdOpening, body: TaxOpeningBody) -> None:
    opening.tax_year = body.tax_year
    opening.through_period = body.through_period
    opening.employment_months_to_date = body.employment_months_to_date
    opening.taxable_income = body.taxable_income
    opening.employee_contribution = body.employee_contribution
    opening.special_deduction = body.special_deduction
    opening.tax_withheld = body.tax_withheld
    opening.evidence_ref = body.evidence_ref


@router.get("", response_model=list[TaxOpeningOut])
def list_tax_openings(
    employee_id: int,
    _principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> list[TaxOpeningOut]:
    _employee_or_404(session, employee_id)
    openings = session.scalars(
        select(EmployeeTaxYtdOpening)
        .where(EmployeeTaxYtdOpening.employee_id == employee_id)
        .order_by(EmployeeTaxYtdOpening.tax_year.desc(), EmployeeTaxYtdOpening.revision.desc())
    ).all()
    return [_out(opening) for opening in openings]


@router.post("", response_model=TaxOpeningOut, status_code=status.HTTP_201_CREATED)
def create_tax_opening(
    employee_id: int,
    body: TaxOpeningBody,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> TaxOpeningOut:
    lock_payroll_input_mutation(session)
    employee = _employee_or_404(session, employee_id)
    _validate_opening_for_employee(body, employee)
    existing = session.scalars(
        select(EmployeeTaxYtdOpening)
        .where(
            EmployeeTaxYtdOpening.employee_id == employee_id,
            EmployeeTaxYtdOpening.tax_year == body.tax_year,
        )
        .with_for_update()
    ).first()
    if existing is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                "A tax opening already exists for this employee and year; "
                "use its correction flow"
            ),
        )
    opening = EmployeeTaxYtdOpening(
        employee_id=employee_id,
        revision=1,
        created_by=principal.user_id,
    )
    _apply_opening_body(opening, body)
    session.add(opening)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="A tax opening already exists") from None
    audit.record(
        session,
        action="employee_tax_opening.create",
        actor=(principal.user_id, principal.username),
        target_type="employee_tax_ytd_opening",
        target_id=opening.id,
        detail={"employee_id": employee_id, "tax_year": opening.tax_year, "revision": 1},
    )
    session.commit()
    return _out(opening)


@router.patch("/{opening_id}", response_model=TaxOpeningOut)
def update_tax_opening(
    employee_id: int,
    opening_id: int,
    body: TaxOpeningBody,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> TaxOpeningOut:
    lock_payroll_input_mutation(session)
    employee = _employee_or_404(session, employee_id)
    opening = _opening_or_404(session, employee_id, opening_id)
    if opening.is_finalized:
        raise HTTPException(status_code=409, detail="Finalized tax openings are immutable")
    if opening.supersedes_id is not None and body.tax_year != opening.tax_year:
        raise HTTPException(
            status_code=422,
            detail="A tax opening correction cannot change the predecessor tax year",
        )
    _validate_opening_for_employee(body, employee)
    before = {
        "through_period": opening.through_period,
        "employment_months_to_date": opening.employment_months_to_date,
        "taxable_income": str(opening.taxable_income),
        "employee_contribution": str(opening.employee_contribution),
        "special_deduction": str(opening.special_deduction),
        "tax_withheld": str(opening.tax_withheld),
        "evidence_ref": opening.evidence_ref,
    }
    _apply_opening_body(opening, body)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="A tax opening already exists") from None
    audit.record(
        session,
        action="employee_tax_opening.update",
        actor=(principal.user_id, principal.username),
        target_type="employee_tax_ytd_opening",
        target_id=opening.id,
        detail={"before": before, "tax_year": opening.tax_year, "revision": opening.revision},
    )
    session.commit()
    return _out(opening)


@router.post("/{opening_id}/finalize", response_model=TaxOpeningOut)
def finalize_tax_opening(
    employee_id: int,
    opening_id: int,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> TaxOpeningOut:
    lock_payroll_input_mutation(session)
    employee = _employee_or_404(session, employee_id)
    opening = _opening_or_404(session, employee_id, opening_id)
    if opening.is_finalized:
        raise HTTPException(status_code=409, detail="Tax opening is already finalized")
    _validate_opening_values_for_employee(
        tax_year=opening.tax_year,
        through_period=opening.through_period,
        employment_months_to_date=opening.employment_months_to_date,
        employee=employee,
    )
    if _history_transition_is_blocked(
        session, employee_id=employee_id, through_period=opening.through_period
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                "Finalize the tax opening before affected payroll history starts "
                "or reopen it for correction"
            ),
        )
    predecessor: EmployeeTaxYtdOpening | None = None
    if opening.supersedes_id is not None:
        predecessor = session.scalars(
            select(EmployeeTaxYtdOpening)
            .where(EmployeeTaxYtdOpening.id == opening.supersedes_id)
            .with_for_update()
        ).first()
        if (
            predecessor is None
            or predecessor.employee_id != employee_id
            or predecessor.tax_year != opening.tax_year
            or not predecessor.is_finalized
            or predecessor.superseded_at is not None
        ):
            raise HTTPException(status_code=409, detail="Tax opening predecessor is not active")
    active = session.scalars(
        select(EmployeeTaxYtdOpening)
        .where(
            EmployeeTaxYtdOpening.employee_id == employee_id,
            EmployeeTaxYtdOpening.tax_year == opening.tax_year,
            EmployeeTaxYtdOpening.is_finalized.is_(True),
            EmployeeTaxYtdOpening.superseded_at.is_(None),
        )
        .with_for_update()
    ).first()
    if active is not None and (predecessor is None or active.id != predecessor.id):
        raise HTTPException(
            status_code=409, detail="An active finalized tax opening already exists"
        )

    now = datetime.now(UTC)
    if predecessor is not None:
        predecessor.superseded_by = principal.user_id
        predecessor.superseded_at = now
        session.flush()
    opening.is_finalized = True
    opening.finalized_by = principal.user_id
    opening.finalized_at = now
    session.flush()
    audit.record(
        session,
        action="employee_tax_opening.finalize",
        actor=(principal.user_id, principal.username),
        target_type="employee_tax_ytd_opening",
        target_id=opening.id,
        detail={
            "employee_id": employee_id,
            "tax_year": opening.tax_year,
            "revision": opening.revision,
            "supersedes_id": opening.supersedes_id,
        },
    )
    session.commit()
    return _out(opening)


@router.post(
    "/{opening_id}/supersede", response_model=TaxOpeningOut, status_code=status.HTTP_201_CREATED
)
def supersede_tax_opening(
    employee_id: int,
    opening_id: int,
    body: TaxOpeningBody,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> TaxOpeningOut:
    lock_payroll_input_mutation(session)
    employee = _employee_or_404(session, employee_id)
    predecessor = _opening_or_404(session, employee_id, opening_id)
    if not predecessor.is_finalized or predecessor.superseded_at is not None:
        raise HTTPException(
            status_code=409, detail="Only an active finalized tax opening can be superseded"
        )
    if body.tax_year != predecessor.tax_year:
        raise HTTPException(
            status_code=422, detail="A tax opening correction cannot change the tax year"
        )
    _validate_opening_for_employee(body, employee)
    if _history_transition_is_blocked(
        session, employee_id=employee_id, through_period=body.through_period
    ):
        raise HTTPException(
            status_code=409,
            detail="Reopen affected payroll history before creating a tax-opening correction",
        )
    draft = session.scalars(
        select(EmployeeTaxYtdOpening)
        .where(
            EmployeeTaxYtdOpening.employee_id == employee_id,
            EmployeeTaxYtdOpening.tax_year == predecessor.tax_year,
            EmployeeTaxYtdOpening.is_finalized.is_(False),
        )
        .with_for_update()
    ).first()
    if draft is not None:
        raise HTTPException(status_code=409, detail="A tax-opening correction draft already exists")
    opening = EmployeeTaxYtdOpening(
        employee_id=employee_id,
        revision=predecessor.revision + 1,
        supersedes_id=predecessor.id,
        created_by=principal.user_id,
    )
    _apply_opening_body(opening, body)
    session.add(opening)
    session.flush()
    audit.record(
        session,
        action="employee_tax_opening.supersede.create",
        actor=(principal.user_id, principal.username),
        target_type="employee_tax_ytd_opening",
        target_id=opening.id,
        detail={"supersedes_id": predecessor.id, "revision": opening.revision},
    )
    session.commit()
    return _out(opening)
