from __future__ import annotations

from datetime import date
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_global_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.core.decimal import decimal_text
from app.db.session import get_session
from app.importing.source_lock import lock_legacy_salary_dataset
from app.legacy_catalog import service as legacy
from app.models.comp import SalaryComponentDef
from app.models.grade import JobGrade, SalaryBand
from app.routers.comp import ComponentCreate
from app.schemas.grade import JobGradeCreate, SalaryBandCreate

router = APIRouter(prefix="/api/legacy-catalog", tags=["legacy-catalog"])
global_import_dependency = require_global_permission(Perm.IMPORT_RUN)
global_structure_write_dependency = require_global_permission(Perm.STRUCTURE_WRITE)
global_grade_write_dependency = require_global_permission(Perm.GRADE_WRITE)


def _global_legacy_importer(
    principal: Principal = Depends(global_import_dependency),
) -> Principal:
    return principal


class LegacySourceOut(BaseModel):
    record_count: int
    period_from: str | None
    period_to: str | None
    snapshot_id: str


class LegacyComponentCandidateOut(BaseModel):
    source_field: str
    record_count: int
    nonzero_count: int
    period_from: str
    period_to: str
    suggested_component_type: str | None
    suggested_allowance_kind: None = None
    classification: str
    importable: bool
    applied: bool
    applied_target_id: int | None
    note: str


class LegacyGradeCandidateOut(BaseModel):
    position: str
    record_count: int
    contributor_count: int
    salary_sample_count: int
    period_from: str
    period_to: str
    observed_p25: str | None
    observed_median: str | None
    observed_p75: str | None
    suppressed_for_privacy: bool
    applied: bool
    applied_target_id: int | None
    is_official_grade: Literal[False] = False


class LegacyCatalogPreviewOut(BaseModel):
    source: LegacySourceOut
    component_candidates: list[LegacyComponentCandidateOut]
    grade_source_status: Literal["OFFICIAL_MASTER_NOT_PRESENT"]
    grade_candidates: list[LegacyGradeCandidateOut]
    warnings: list[str]


@router.get("/preview", response_model=LegacyCatalogPreviewOut)
def preview_legacy_catalog(
    principal: Principal = Depends(_global_legacy_importer),
    session: Session = Depends(get_session),
) -> LegacyCatalogPreviewOut:
    lock_legacy_salary_dataset(session)
    summary = legacy.source_summary(session)
    component_observations, grade_observations = legacy.catalog_observations(session)
    specs = {spec.source_field: spec for spec in legacy.COMPONENT_FIELD_SPECS}
    applied_components = legacy.applied_source_targets(session, kind="component")
    applied_grades = legacy.applied_source_targets(session, kind="grade")
    response = LegacyCatalogPreviewOut(
        source=LegacySourceOut(
            record_count=summary.record_count,
            period_from=summary.period_from,
            period_to=summary.period_to,
            snapshot_id=summary.snapshot_id,
        ),
        component_candidates=[
            LegacyComponentCandidateOut(
                source_field=observation.source_field,
                record_count=observation.record_count,
                nonzero_count=observation.nonzero_count,
                period_from=observation.period_from,
                period_to=observation.period_to,
                suggested_component_type=specs[observation.source_field].suggested_component_type,
                classification=specs[observation.source_field].classification,
                importable=(
                    specs[observation.source_field].importable
                    and observation.source_field not in applied_components
                ),
                applied=observation.source_field in applied_components,
                applied_target_id=applied_components.get(observation.source_field),
                note=specs[observation.source_field].note,
            )
            for observation in component_observations
        ],
        grade_source_status="OFFICIAL_MASTER_NOT_PRESENT",
        grade_candidates=[
            LegacyGradeCandidateOut(
                **observation.__dict__,
                applied=observation.position in applied_grades,
                applied_target_id=applied_grades.get(observation.position),
            )
            for observation in grade_observations
        ],
        warnings=[
            "旧工资明细不包含官方职级或薪档主数据。",
            "历史薪资分位仅供核对，不能自动成为正式薪档。",
            "组件类型、固定/浮动性质及计税口径必须由人事确认。",
        ],
    )
    audit.record(
        session,
        action="legacy_catalog.preview",
        actor=(principal.user_id, principal.username),
        target_type="salary_record",
        detail={
            "source_record_count": summary.record_count,
            "source_snapshot_id": summary.snapshot_id,
            "period_from": summary.period_from,
            "period_to": summary.period_to,
            "component_candidate_count": len(response.component_candidates),
            "grade_candidate_count": len(response.grade_candidates),
            "component_candidates": [
                {
                    "source_field": item.source_field,
                    "record_count": item.record_count,
                    "nonzero_count": item.nonzero_count,
                    "period_from": item.period_from,
                    "period_to": item.period_to,
                    "classification": item.classification,
                    "applied": item.applied,
                }
                for item in response.component_candidates
            ],
            "grade_candidates": [
                {
                    "position": item.position,
                    "record_count": item.record_count,
                    "contributor_count": item.contributor_count,
                    "salary_sample_count": item.salary_sample_count,
                    "period_from": item.period_from,
                    "period_to": item.period_to,
                    "observed_p25": item.observed_p25,
                    "observed_median": item.observed_median,
                    "observed_p75": item.observed_p75,
                    "applied": item.applied,
                }
                for item in response.grade_candidates
            ],
        },
    )
    session.commit()
    return response


class ConfirmedLegacyComponentApply(BaseModel):
    source_field: str = Field(min_length=1, max_length=128)
    expected_record_count: int = Field(gt=0)
    expected_source_snapshot_id: str = Field(min_length=32, max_length=32)
    confirmed_by_hr: Literal[True]
    reason: str = Field(min_length=1, max_length=1000)
    component: ComponentCreate

    @field_validator("source_field", "reason")
    @classmethod
    def trim_nonblank(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class LegacyComponentApplyOut(BaseModel):
    id: int
    code: str
    name: str
    component_type: str
    taxable: bool
    in_social_base: bool
    in_housing_base: bool
    prorate_by_attendance: bool
    allowance_kind: str | None
    sort_order: int
    created_by: int

    model_config = {"from_attributes": True}


@router.post(
    "/components/apply",
    response_model=LegacyComponentApplyOut,
    status_code=status.HTTP_201_CREATED,
)
def apply_legacy_component(
    body: ConfirmedLegacyComponentApply,
    principal: Principal = Depends(_global_legacy_importer),
    _catalog_writer: Principal = Depends(global_structure_write_dependency),
    session: Session = Depends(get_session),
) -> SalaryComponentDef:
    spec = next(
        (item for item in legacy.COMPONENT_FIELD_SPECS if item.source_field == body.source_field),
        None,
    )
    if spec is None or not spec.importable:
        raise HTTPException(status_code=404, detail="Importable legacy source field not found.")
    lock_legacy_salary_dataset(session)
    summary = legacy.source_summary(session)
    if summary.snapshot_id != body.expected_source_snapshot_id:
        raise HTTPException(
            status_code=409,
            detail="Legacy source data changed; refresh the preview before importing.",
        )
    if legacy.lock_and_check_source_applied(
        session,
        kind="component",
        source_value=body.source_field,
    ):
        raise HTTPException(
            status_code=409,
            detail="This legacy source field has already been assigned to a salary component.",
        )
    observation = legacy.component_observation(session, body.source_field)
    if observation is None:
        raise HTTPException(status_code=404, detail="Legacy source field has no imported records.")
    if observation.record_count != body.expected_record_count:
        raise HTTPException(
            status_code=409,
            detail="Legacy source data changed; refresh the preview before importing.",
        )
    existing = session.scalars(
        select(SalaryComponentDef).where(SalaryComponentDef.code == body.component.code)
    ).first()
    if existing is not None:
        detail = (
            "Salary component code belongs to an inactive component; restore it instead."
            if existing.is_deleted
            else "Salary component code already exists."
        )
        raise HTTPException(status_code=409, detail=detail)
    component = SalaryComponentDef(
        **body.component.model_dump(),
        created_by=principal.user_id,
    )
    session.add(component)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409, detail="Salary component code already exists."
        ) from None
    audit.record(
        session,
        action="legacy_catalog.component.apply",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=component.id,
        detail={
            "source_field": body.source_field,
            "source_snapshot_id": summary.snapshot_id,
            "source_record_count": observation.record_count,
            "source_nonzero_count": observation.nonzero_count,
            "period_from": observation.period_from,
            "period_to": observation.period_to,
            "reason": body.reason,
            "confirmed_by_hr": True,
            "component_code": component.code,
            "component_type": component.component_type.value,
            "component": {
                "id": component.id,
                "code": component.code,
                "name": component.name,
                "component_type": component.component_type.value,
                "taxable": component.taxable,
                "in_social_base": component.in_social_base,
                "in_housing_base": component.in_housing_base,
                "prorate_by_attendance": component.prorate_by_attendance,
                "allowance_kind": (
                    component.allowance_kind.value if component.allowance_kind else None
                ),
                "sort_order": component.sort_order,
            },
        },
    )
    session.commit()
    return component


class ConfirmedLegacyGradeApply(BaseModel):
    source_position: str = Field(min_length=1, max_length=128)
    expected_record_count: int = Field(gt=0)
    expected_source_snapshot_id: str = Field(min_length=32, max_length=32)
    policy_confirmation: Literal["HR_CONFIRMED"]
    reason: str = Field(min_length=1, max_length=1000)
    grade: JobGradeCreate
    band: SalaryBandCreate

    @field_validator("source_position", "reason")
    @classmethod
    def normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class LegacyAppliedGradeOut(BaseModel):
    id: int
    code: str
    name: str
    rank: int
    version: int

    model_config = {"from_attributes": True}


class LegacyAppliedBandOut(BaseModel):
    id: int
    job_grade_id: int
    band_min: str
    band_mid: str
    band_max: str
    effective_from: date


class LegacyObservedHistoryOut(BaseModel):
    record_count: int
    contributor_count: int
    salary_sample_count: int
    observed_median: str


class LegacyGradeApplyOut(BaseModel):
    grade: LegacyAppliedGradeOut
    band: LegacyAppliedBandOut
    observed_history: LegacyObservedHistoryOut


@router.post(
    "/grades/apply",
    response_model=LegacyGradeApplyOut,
    status_code=status.HTTP_201_CREATED,
)
def apply_legacy_grade(
    body: ConfirmedLegacyGradeApply,
    principal: Principal = Depends(_global_legacy_importer),
    _catalog_writer: Principal = Depends(global_grade_write_dependency),
    session: Session = Depends(get_session),
) -> LegacyGradeApplyOut:
    lock_legacy_salary_dataset(session)
    summary = legacy.source_summary(session)
    if summary.snapshot_id != body.expected_source_snapshot_id:
        raise HTTPException(
            status_code=409,
            detail="Legacy source data changed; refresh the preview before importing.",
        )
    if legacy.lock_and_check_source_applied(
        session,
        kind="grade",
        source_value=body.source_position,
    ):
        raise HTTPException(
            status_code=409,
            detail="This legacy position has already been assigned to a job grade.",
        )
    observation = legacy.grade_observation(session, body.source_position)
    if (
        observation is None
        or observation.suppressed_for_privacy
        or observation.observed_median is None
    ):
        raise HTTPException(status_code=404, detail="Legacy position was not found.")
    if observation.record_count != body.expected_record_count:
        raise HTTPException(
            status_code=409,
            detail="Legacy source data changed; refresh the preview before importing.",
        )
    if body.band.job_grade_id is not None:
        raise HTTPException(
            status_code=422, detail="Grade import band must use the new grade path."
        )
    if not (body.band.band_min <= body.band.band_mid <= body.band.band_max):
        raise HTTPException(status_code=422, detail="Salary band must satisfy min <= mid <= max.")
    if session.scalar(select(JobGrade.id).where(JobGrade.code == body.grade.code)) is not None:
        raise HTTPException(status_code=409, detail="Job grade code already exists.")
    grade = JobGrade(
        code=body.grade.code,
        name=body.grade.name,
        rank=body.grade.rank,
        created_by=principal.user_id,
    )
    session.add(grade)
    try:
        session.flush()
        band = SalaryBand(
            job_grade_id=grade.id,
            band_min=body.band.band_min,
            band_mid=body.band.band_mid,
            band_max=body.band.band_max,
            effective_from=body.band.effective_from,
            created_by=principal.user_id,
        )
        session.add(band)
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=409,
            detail="The confirmed grade or salary band conflicts with existing data.",
        ) from None
    policy_band = {
        "band_min": decimal_text(band.band_min),
        "band_mid": decimal_text(band.band_mid),
        "band_max": decimal_text(band.band_max),
        "effective_from": str(band.effective_from),
    }
    audit.record(
        session,
        action="grade.create",
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={"source": "legacy_catalog_hr_confirmation", "code": grade.code},
    )
    audit.record(
        session,
        action="band.create",
        actor=(principal.user_id, principal.username),
        target_type="salary_band",
        target_id=band.id,
        detail={"source": "legacy_catalog_hr_confirmation", **policy_band},
    )
    audit.record(
        session,
        action="legacy_catalog.grade.apply",
        actor=(principal.user_id, principal.username),
        target_type="job_grade",
        target_id=grade.id,
        detail={
            "source_position": body.source_position,
            "source_snapshot_id": summary.snapshot_id,
            "source_record_count": observation.record_count,
            "period_from": observation.period_from,
            "period_to": observation.period_to,
            "contributor_count": observation.contributor_count,
            "salary_sample_count": observation.salary_sample_count,
            "observed_median": observation.observed_median,
            "policy_band": policy_band,
            "grade": {
                "id": grade.id,
                "code": grade.code,
                "name": grade.name,
                "rank": grade.rank,
                "version": grade.version,
            },
            "reason": body.reason,
            "policy_confirmation": body.policy_confirmation,
        },
    )
    session.commit()
    return LegacyGradeApplyOut(
        grade=LegacyAppliedGradeOut.model_validate(grade),
        band=LegacyAppliedBandOut(
            id=band.id,
            job_grade_id=band.job_grade_id,
            band_min=decimal_text(band.band_min),
            band_mid=decimal_text(band.band_mid),
            band_max=decimal_text(band.band_max),
            effective_from=band.effective_from,
        ),
        observed_history=LegacyObservedHistoryOut(
            record_count=observation.record_count,
            contributor_count=observation.contributor_count,
            salary_sample_count=observation.salary_sample_count,
            observed_median=observation.observed_median,
        ),
    )
