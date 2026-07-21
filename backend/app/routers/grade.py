from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_global_permission, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.grade import JobGrade, SalaryBand
from app.schemas.grade import (
    JobGradeCreate,
    JobGradeLifecycle,
    JobGradeOut,
    JobGradeUpdate,
    SalaryBandCreate,
    SalaryBandOut,
)

router = APIRouter(prefix="/api/grades", tags=["grades"])
GradeListStatus = Literal["active", "inactive", "all"]
grade_catalog_write_dependency = require_global_permission(Perm.GRADE_WRITE)


def _constraint_name(exc: IntegrityError) -> str | None:
    return getattr(getattr(exc.orig, "diag", None), "constraint_name", None)


def _grade_snapshot(grade: JobGrade) -> dict[str, object]:
    return {
        "code": grade.code,
        "name": grade.name,
        "rank": grade.rank,
        "version": grade.version,
        "is_active": grade.is_active,
        "deactivated_at": (
            grade.deactivated_at.isoformat() if grade.deactivated_at is not None else None
        ),
    }


def _band_snapshot(band: SalaryBand, *, effective_to: date | None) -> dict[str, object]:
    return {
        "job_grade_id": band.job_grade_id,
        "band_min": str(band.band_min),
        "band_mid": str(band.band_mid),
        "band_max": str(band.band_max),
        "effective_from": str(band.effective_from),
        "effective_to": str(effective_to) if effective_to is not None else None,
    }


def _band_out(band: SalaryBand, *, effective_to: date | None) -> SalaryBandOut:
    return SalaryBandOut(
        id=band.id,
        job_grade_id=band.job_grade_id,
        band_min=band.band_min,
        band_mid=band.band_mid,
        band_max=band.band_max,
        effective_from=band.effective_from,
        effective_to=effective_to,
    )


def _grade_for_update(session: Session, grade_id: int) -> JobGrade | None:
    return session.scalars(
        select(JobGrade)
        .where(JobGrade.id == grade_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _next_band_date(session: Session, band: SalaryBand) -> date | None:
    return session.scalar(
        select(func.min(SalaryBand.effective_from)).where(
            SalaryBand.job_grade_id == band.job_grade_id,
            SalaryBand.is_deleted.is_(False),
            SalaryBand.effective_from > band.effective_from,
        )
    )


@router.get("", response_model=list[JobGradeOut])
def list_grades(
    status_filter: GradeListStatus = Query("active", alias="status"),
    _p: Principal = Depends(require_permission(Perm.GRADE_READ)),
    session: Session = Depends(get_session),
) -> list[JobGrade]:
    statement = select(JobGrade)
    if status_filter == "active":
        statement = statement.where(JobGrade.is_deleted.is_(False))
    elif status_filter == "inactive":
        statement = statement.where(JobGrade.is_deleted.is_(True))
    statement = statement.order_by(JobGrade.rank.desc(), JobGrade.code, JobGrade.id)
    return list(session.scalars(statement).all())


@router.post("", response_model=JobGradeOut, status_code=status.HTTP_201_CREATED)
def create_grade(
    body: JobGradeCreate,
    principal: Principal = Depends(grade_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> JobGrade:
    grade = JobGrade(
        code=body.code,
        name=body.name,
        rank=body.rank,
        created_by=principal.user_id,
    )
    session.add(grade)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        if _constraint_name(exc) == "job_grade_code_key":
            raise HTTPException(status_code=409, detail="Job grade code already exists.") from None
        raise
    audit.record(
        session,
        action="grade.create",
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={"before": None, "after": _grade_snapshot(grade)},
    )
    session.commit()
    return grade


@router.patch("/{grade_id}", response_model=JobGradeOut)
def update_grade(
    grade_id: int,
    body: JobGradeUpdate,
    principal: Principal = Depends(grade_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> JobGrade:
    grade = _grade_for_update(session, grade_id)
    if grade is None:
        raise HTTPException(status_code=404, detail="Job grade does not exist.")
    if not grade.is_active:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Inactive job grades must be restored before editing.",
        )
    if grade.version != body.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job grade changed by another user; refresh and retry.",
        )
    before = _grade_snapshot(grade)
    requested = body.model_dump(exclude_unset=True, exclude={"expected_version"})
    data = {field: value for field, value in requested.items() if value != getattr(grade, field)}
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Job grade update must change at least one field.",
        )
    for field, value in data.items():
        setattr(grade, field, value)
    grade.version += 1
    session.flush()
    audit.record(
        session,
        action="grade.update",
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={
            "changed": sorted(data.keys()),
            "from_version": body.expected_version,
            "before": before,
            "after": _grade_snapshot(grade),
        },
    )
    session.commit()
    return grade


def _set_grade_active(
    session: Session,
    *,
    grade_id: int,
    body: JobGradeLifecycle,
    principal: Principal,
    active: bool,
) -> JobGrade:
    grade = _grade_for_update(session, grade_id)
    if grade is None:
        raise HTTPException(status_code=404, detail="Job grade does not exist.")
    # A retried lifecycle request is a successful no-op.  This check precedes
    # optimistic concurrency so a timeout retry carrying the old version does
    # not turn a completed operation into a misleading conflict.
    if grade.is_active is active:
        session.commit()
        return grade
    if body.expected_version is not None and grade.version != body.expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Job grade changed by another user; refresh and retry.",
        )
    before = _grade_snapshot(grade)
    grade.is_deleted = not active
    grade.deleted_at = None if active else datetime.now(UTC)
    grade.version += 1
    session.flush()
    action = "grade.restore" if active else "grade.deactivate"
    audit.record(
        session,
        action=action,
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={
            "reason": body.reason,
            "from_version": before["version"],
            "before": before,
            "after": _grade_snapshot(grade),
        },
    )
    session.commit()
    return grade


@router.post("/{grade_id}/deactivate", response_model=JobGradeOut)
def deactivate_grade(
    grade_id: int,
    body: JobGradeLifecycle,
    principal: Principal = Depends(grade_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> JobGrade:
    return _set_grade_active(
        session,
        grade_id=grade_id,
        body=body,
        principal=principal,
        active=False,
    )


@router.post("/{grade_id}/restore", response_model=JobGradeOut)
def restore_grade(
    grade_id: int,
    body: JobGradeLifecycle,
    principal: Principal = Depends(grade_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> JobGrade:
    return _set_grade_active(
        session,
        grade_id=grade_id,
        body=body,
        principal=principal,
        active=True,
    )


@router.get("/{grade_id}/bands", response_model=list[SalaryBandOut])
def list_bands(
    grade_id: int,
    _p: Principal = Depends(require_permission(Perm.GRADE_READ)),
    session: Session = Depends(get_session),
) -> list[SalaryBandOut]:
    if session.get(JobGrade, grade_id) is None:
        raise HTTPException(status_code=404, detail="Job grade does not exist.")
    statement = (
        select(SalaryBand)
        .where(SalaryBand.job_grade_id == grade_id, SalaryBand.is_deleted.is_(False))
        .order_by(SalaryBand.effective_from.desc(), SalaryBand.id.desc())
    )
    bands = list(session.scalars(statement).all())
    result: list[SalaryBandOut] = []
    next_effective_from: date | None = None
    for band in bands:
        result.append(_band_out(band, effective_to=next_effective_from))
        next_effective_from = band.effective_from
    return result


@router.post(
    "/{grade_id}/bands",
    response_model=SalaryBandOut,
    status_code=status.HTTP_201_CREATED,
)
def create_band(
    grade_id: int,
    body: SalaryBandCreate,
    principal: Principal = Depends(grade_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> SalaryBandOut:
    grade = _grade_for_update(session, grade_id)
    if grade is None:
        raise HTTPException(status_code=404, detail="Job grade does not exist.")
    if not grade.is_active:
        raise HTTPException(status_code=409, detail="Inactive job grades cannot receive new bands.")
    if body.job_grade_id is not None and body.job_grade_id != grade_id:
        raise HTTPException(status_code=422, detail="Body job_grade_id must match the path.")
    if not (body.band_min <= body.band_mid <= body.band_max):
        raise HTTPException(status_code=400, detail="Salary band must satisfy min <= mid <= max.")
    duplicate_id = session.scalar(
        select(SalaryBand.id).where(
            SalaryBand.job_grade_id == grade_id,
            SalaryBand.effective_from == body.effective_from,
            SalaryBand.is_deleted.is_(False),
        )
    )
    if duplicate_id is not None:
        raise HTTPException(
            status_code=409,
            detail="An active salary band already exists for this grade and effective date.",
        )
    band = SalaryBand(
        job_grade_id=grade_id,
        band_min=body.band_min,
        band_mid=body.band_mid,
        band_max=body.band_max,
        effective_from=body.effective_from,
        created_by=principal.user_id,
    )
    session.add(band)
    try:
        session.flush()
    except IntegrityError as exc:
        constraint_name = _constraint_name(exc)
        session.rollback()
        if constraint_name == "uq_salary_band_grade_effective_from_active":
            raise HTTPException(
                status_code=409,
                detail="An active salary band already exists for this grade and effective date.",
            ) from None
        raise
    effective_to = _next_band_date(session, band)
    audit.record(
        session,
        action="band.create",
        actor=(principal.user_id, principal.username),
        target_type="salary_band",
        target_id=band.id,
        detail={"before": None, "after": _band_snapshot(band, effective_to=effective_to)},
    )
    session.commit()
    return _band_out(band, effective_to=effective_to)
