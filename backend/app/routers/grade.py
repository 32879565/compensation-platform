from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.grade import JobGrade, SalaryBand
from app.repositories.grade import JobGradeRepository, SalaryBandRepository
from app.schemas.grade import (
    JobGradeCreate,
    JobGradeOut,
    JobGradeUpdate,
    SalaryBandCreate,
    SalaryBandOut,
)

router = APIRouter(prefix="/api/grades", tags=["grades"])


@router.get("", response_model=list[JobGradeOut])
def list_grades(
    _p: Principal = Depends(require_permission(Perm.GRADE_READ)),
    session: Session = Depends(get_session),
) -> list[JobGrade]:
    return list(JobGradeRepository(session).list(page=1, page_size=500).items)


@router.post("", response_model=JobGradeOut, status_code=status.HTTP_201_CREATED)
def create_grade(
    body: JobGradeCreate,
    principal: Principal = Depends(require_permission(Perm.GRADE_WRITE)),
    session: Session = Depends(get_session),
) -> JobGrade:
    grade = JobGrade(code=body.code, name=body.name, rank=body.rank)
    session.add(grade)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="职级编码已存在") from None
    audit.record(
        session,
        action="grade.create",
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={"code": grade.code},
    )
    session.commit()
    return grade


@router.patch("/{grade_id}", response_model=JobGradeOut)
def update_grade(
    grade_id: int,
    body: JobGradeUpdate,
    principal: Principal = Depends(require_permission(Perm.GRADE_WRITE)),
    session: Session = Depends(get_session),
) -> JobGrade:
    repo = JobGradeRepository(session)
    grade = repo.get(grade_id)
    if grade is None:
        raise HTTPException(status_code=404, detail="职级不存在")
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(grade, field, value)
    session.flush()
    audit.record(
        session,
        action="grade.update",
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={"changed": sorted(data.keys())},
    )
    session.commit()
    return grade


@router.get("/{grade_id}/bands", response_model=list[SalaryBandOut])
def list_bands(
    grade_id: int,
    _p: Principal = Depends(require_permission(Perm.GRADE_READ)),
    session: Session = Depends(get_session),
) -> list[SalaryBand]:
    from sqlalchemy import select

    stmt = (
        select(SalaryBand)
        .where(SalaryBand.job_grade_id == grade_id, SalaryBand.is_deleted.is_(False))
        .order_by(SalaryBand.effective_from.desc())
    )
    return list(session.scalars(stmt).all())


@router.post("/{grade_id}/bands", response_model=SalaryBandOut, status_code=status.HTTP_201_CREATED)
def create_band(
    grade_id: int,
    body: SalaryBandCreate,
    principal: Principal = Depends(require_permission(Perm.GRADE_WRITE)),
    session: Session = Depends(get_session),
) -> SalaryBand:
    if JobGradeRepository(session).get(grade_id) is None:
        raise HTTPException(status_code=404, detail="职级不存在")
    if not (body.band_min <= body.band_mid <= body.band_max):
        raise HTTPException(status_code=400, detail="带宽需满足 min ≤ mid ≤ max")
    band = SalaryBand(
        job_grade_id=grade_id,
        band_min=body.band_min,
        band_mid=body.band_mid,
        band_max=body.band_max,
        effective_from=body.effective_from,
    )
    SalaryBandRepository(session).add(band)
    audit.record(
        session,
        action="band.create",
        actor=(principal.user_id, principal.username),
        target_type="salary_band",
        target_id=band.id,
        detail={"job_grade_id": grade_id},
    )
    session.commit()
    return band
