from __future__ import annotations

import enum
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationInfo, field_validator, model_validator
from sqlalchemy import case, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_any_permission, require_global_permission, require_permission
from app.auth.permissions import Perm
from app.auth.service import (
    Principal,
    permission_org_scope_allows,
    resolve_permission_org_scope,
)
from app.comp.service import (
    StructureError,
    compa_ratio,
    current_structure,
    lock_employee_salary_structure,
    set_component_amount,
)
from app.core.decimal import decimal_text
from app.core.urls import optional_http_url
from app.db.session import get_session
from app.models.approval import SalaryAdjustment, SalaryAdjustmentStatus
from app.models.comp import (
    AllowanceKind,
    ComponentType,
    EmployeeSalaryStructure,
    SalaryComponentDef,
)
from app.models.employee import Employee
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import AdjustmentRecord, PayrollResult
from app.payroll.guards import (
    PayrollSourceLockedError,
    assert_structure_effective_date_mutable,
    first_affected_employee_structure_period,
    lock_payroll_input_mutation,
)
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api/salary-components", tags=["comp"])
component_read_dependency = require_any_permission(Perm.STRUCTURE_READ, Perm.ADJUSTMENT_CREATE)
component_catalog_write_dependency = require_global_permission(Perm.STRUCTURE_WRITE)


class ComponentCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    component_type: ComponentType
    taxable: bool = True
    in_social_base: bool = False
    in_housing_base: bool = False
    prorate_by_attendance: bool = False
    allowance_kind: AllowanceKind | None = None  # 仅补贴类需区分固定/浮动
    sort_order: int = 0

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @model_validator(mode="after")
    def validate_allowance_kind(self) -> ComponentCreate:
        if self.component_type is ComponentType.ALLOWANCE:
            if self.allowance_kind is None:
                raise ValueError("ALLOWANCE components require allowance_kind")
        elif self.allowance_kind is not None:
            raise ValueError("Only ALLOWANCE components may define allowance_kind")
        if self.component_type is not ComponentType.ALLOWANCE and self.prorate_by_attendance:
            raise ValueError("Only ALLOWANCE components may be prorated by attendance")
        return self


class ComponentUpdate(BaseModel):
    expected_updated_at: datetime
    reason: str | None = Field(default=None, max_length=1000)
    name: str | None = Field(default=None, max_length=64)
    taxable: bool | None = None
    in_social_base: bool | None = None
    in_housing_base: bool | None = None
    prorate_by_attendance: bool | None = None
    allowance_kind: AllowanceKind | None = None
    sort_order: int | None = None

    model_config = {"extra": "forbid"}

    @field_validator(
        "name",
        "taxable",
        "in_social_base",
        "in_housing_base",
        "prorate_by_attendance",
        "sort_order",
        mode="before",
    )
    @classmethod
    def reject_explicit_null(cls, value: object, info: ValidationInfo) -> object:
        if value is None:
            raise ValueError(f"{info.field_name} cannot be null")
        return value

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return value
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("reason", mode="before")
    @classmethod
    def normalize_optional_reason(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("expected_updated_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("expected_updated_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_a_component_change(self) -> ComponentUpdate:
        if self.model_fields_set == {"expected_updated_at"}:
            raise ValueError("at least one component field must be changed")
        return self


class ComponentLifecycleBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)
    expected_updated_at: datetime | None = None

    model_config = {"extra": "forbid"}

    @field_validator("reason")
    @classmethod
    def normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("expected_updated_at")
    @classmethod
    def lifecycle_timestamp_requires_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise ValueError("expected_updated_at must include a timezone")
        return value


class ComponentCatalogStatus(enum.StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"
    ALL = "all"


class ComponentOut(BaseModel):
    id: int
    code: str
    name: str
    component_type: ComponentType
    taxable: bool
    in_social_base: bool
    in_housing_base: bool
    prorate_by_attendance: bool
    allowance_kind: AllowanceKind | None
    sort_order: int
    updated_at: datetime
    is_active: bool
    deactivated_at: datetime | None
    calculation_locked: bool
    calculation_lock_reason: str | None


def _component_timestamp_matches(actual: datetime, expected: datetime) -> bool:
    return actual.astimezone(UTC) == expected.astimezone(UTC)


def _next_component_timestamp(previous: datetime) -> datetime:
    now = datetime.now(UTC)
    previous_utc = previous.astimezone(UTC)
    return now if now > previous_utc else previous_utc + timedelta(microseconds=1)


def _component_catalog_snapshot(component: SalaryComponentDef) -> dict[str, object]:
    return {
        "id": component.id,
        "code": component.code,
        "name": component.name,
        "component_type": component.component_type.value,
        "taxable": component.taxable,
        "in_social_base": component.in_social_base,
        "in_housing_base": component.in_housing_base,
        "prorate_by_attendance": component.prorate_by_attendance,
        "allowance_kind": component.allowance_kind.value if component.allowance_kind else None,
        "sort_order": component.sort_order,
        "updated_at": component.updated_at.isoformat(),
        "is_active": not component.is_deleted,
        "deactivated_at": component.deleted_at.isoformat() if component.deleted_at else None,
    }


def _calculation_locked_component_ids(session: Session, component_ids: set[int]) -> set[int]:
    if not component_ids:
        return set()
    # PayrollResult.input_snapshot is the immutable calculation evidence.  A
    # component must be locked only when its stable code actually appears in a
    # persisted snapshot; merely sharing an employee with an older result would
    # incorrectly freeze components added for a later effective date.
    used_in_snapshot = (
        select(PayrollResult.id)
        .where(
            PayrollResult.input_snapshot["structure"].contains(
                func.jsonb_build_array(func.jsonb_build_object("code", SalaryComponentDef.code))
            )
        )
        .exists()
    )
    locked_ids = set(
        session.scalars(
            select(SalaryComponentDef.id).where(
                SalaryComponentDef.id.in_(component_ids),
                used_in_snapshot,
            )
        ).all()
    )
    remaining_ids = component_ids - locked_ids
    if not remaining_ids:
        return locked_ids

    # S13f repaired pre-snapshot payroll rows with ``{}``. Those rows remain
    # immutable payroll evidence but cannot name the components they used.
    # Fall back only for a missing/non-array structure and reconstruct the
    # structure at the exact date the engine selected for that employee.
    period_start = func.to_date(PayrollBatch.period + "-01", "YYYY-MM-DD")
    selection_date = case(
        (
            func.to_char(Employee.hire_date, "YYYY-MM") == PayrollBatch.period,
            Employee.hire_date,
        ),
        else_=period_start,
    )
    legacy_structure_type = func.jsonb_typeof(PayrollResult.input_snapshot["structure"])
    legacy_locked_ids = session.scalars(
        select(EmployeeSalaryStructure.component_id)
        .join(
            PayrollResult,
            PayrollResult.employee_id == EmployeeSalaryStructure.employee_id,
        )
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .join(Employee, Employee.id == PayrollResult.employee_id)
        .where(
            EmployeeSalaryStructure.component_id.in_(remaining_ids),
            func.coalesce(legacy_structure_type, "") != "array",
            # A later backdated row was not an input to the persisted result.
            EmployeeSalaryStructure.created_at <= PayrollResult.created_at,
            EmployeeSalaryStructure.effective_from <= selection_date,
            (EmployeeSalaryStructure.effective_to.is_(None))
            | (EmployeeSalaryStructure.effective_to > selection_date),
        )
        .distinct()
    ).all()
    return locked_ids | set(legacy_locked_ids)


def _component_out(component: SalaryComponentDef, *, calculation_locked: bool) -> ComponentOut:
    return ComponentOut(
        id=component.id,
        code=component.code,
        name=component.name,
        component_type=component.component_type,
        taxable=component.taxable,
        in_social_base=component.in_social_base,
        in_housing_base=component.in_housing_base,
        prorate_by_attendance=component.prorate_by_attendance,
        allowance_kind=component.allowance_kind,
        sort_order=component.sort_order,
        updated_at=component.updated_at,
        is_active=not component.is_deleted,
        deactivated_at=component.deleted_at,
        calculation_locked=calculation_locked,
        calculation_lock_reason=(
            "该组件已参与历史工资计算，计算属性已锁定" if calculation_locked else None
        ),
    )


def _lock_component_for_catalog(session: Session, component_id: int) -> SalaryComponentDef | None:
    lock_payroll_input_mutation(session)
    return session.scalars(
        select(SalaryComponentDef)
        .where(SalaryComponentDef.id == component_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).first()


def _assert_expected_component_timestamp(
    component: SalaryComponentDef, expected_updated_at: datetime | None
) -> None:
    if expected_updated_at is not None and not _component_timestamp_matches(
        component.updated_at, expected_updated_at
    ):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Salary component changed by another user; refresh and retry",
        )


@router.get("", response_model=list[ComponentOut])
def list_components(
    catalog_status: ComponentCatalogStatus = Query(ComponentCatalogStatus.ACTIVE, alias="status"),
    principal: Principal = Depends(component_read_dependency),
    session: Session = Depends(get_session),
) -> list[ComponentOut]:
    if catalog_status is not ComponentCatalogStatus.ACTIVE and not principal.has_permission(
        Perm.STRUCTURE_READ
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive salary-component history requires structure read permission",
        )
    statement = select(SalaryComponentDef)
    if catalog_status is ComponentCatalogStatus.ACTIVE:
        statement = statement.where(SalaryComponentDef.is_deleted.is_(False))
    elif catalog_status is ComponentCatalogStatus.INACTIVE:
        statement = statement.where(SalaryComponentDef.is_deleted.is_(True))
    components = list(
        session.scalars(
            statement.order_by(SalaryComponentDef.sort_order, SalaryComponentDef.code)
        ).all()
    )
    locked_ids = _calculation_locked_component_ids(
        session, {component.id for component in components}
    )
    return [
        _component_out(component, calculation_locked=component.id in locked_ids)
        for component in components
    ]


@router.post("", response_model=ComponentOut, status_code=status.HTTP_201_CREATED)
def create_component(
    body: ComponentCreate,
    principal: Principal = Depends(component_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> ComponentOut:
    existing = session.scalars(
        select(SalaryComponentDef).where(SalaryComponentDef.code == body.code).with_for_update()
    ).first()
    if existing is not None:
        detail = (
            "Salary component code belongs to an inactive component; restore it instead"
            if existing.is_deleted
            else "Salary component code already exists"
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)
    comp = SalaryComponentDef(**body.model_dump(), created_by=principal.user_id)
    session.add(comp)
    try:
        session.flush()
    except IntegrityError as exc:
        session.rollback()
        constraint_name = getattr(getattr(exc.orig, "diag", None), "constraint_name", None)
        if constraint_name == "salary_component_def_code_key":
            raise HTTPException(status_code=409, detail="组件编码已存在") from None
        raise
    audit.record(
        session,
        action="component.create",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=comp.id,
        detail={"before": None, "after": _component_catalog_snapshot(comp)},
    )
    response = _component_out(comp, calculation_locked=False)
    session.commit()
    return response


@router.patch("/{component_id}", response_model=ComponentOut)
def update_component(
    component_id: int,
    body: ComponentUpdate,
    principal: Principal = Depends(component_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> ComponentOut:
    comp = _lock_component_for_catalog(session, component_id)
    if comp is None:
        raise HTTPException(status_code=404, detail="Salary component not found")
    _assert_expected_component_timestamp(comp, body.expected_updated_at)
    requested = body.model_dump(
        exclude_unset=True,
        exclude={"expected_updated_at", "reason"},
    )
    data = {field: value for field, value in requested.items() if value != getattr(comp, field)}
    if not data:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="Salary component update must change at least one field",
        )
    if "allowance_kind" in data:
        allowance_kind = data["allowance_kind"]
        if comp.component_type is ComponentType.ALLOWANCE and allowance_kind is None:
            raise HTTPException(
                status_code=422,
                detail="ALLOWANCE components require allowance_kind",
            )
        if comp.component_type is not ComponentType.ALLOWANCE and allowance_kind is not None:
            raise HTTPException(
                status_code=422,
                detail="Only ALLOWANCE components may define allowance_kind",
            )
    if data.get("prorate_by_attendance") and comp.component_type is not ComponentType.ALLOWANCE:
        raise HTTPException(
            status_code=422,
            detail="Only ALLOWANCE components may be prorated by attendance",
        )
    calculation_fields = {
        "taxable",
        "in_social_base",
        "in_housing_base",
        "prorate_by_attendance",
        "allowance_kind",
    }
    calculation_changes = calculation_fields & data.keys()
    calculation_locked = bool(_calculation_locked_component_ids(session, {comp.id}))
    legacy_allowance_classification = (
        comp.component_type is ComponentType.ALLOWANCE
        and comp.allowance_kind is None
        and data.get("allowance_kind") is not None
    )
    if comp.is_deleted and not legacy_allowance_classification:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Salary component is inactive; restore it before editing",
        )
    if legacy_allowance_classification and not body.reason:
        raise HTTPException(
            status_code=422,
            detail="Classifying a legacy allowance requires a reason",
        )
    blocked_calculation_changes = calculation_changes - (
        {"allowance_kind"} if legacy_allowance_classification else set()
    )
    if calculation_changes:
        pending_adjustment = session.scalar(
            select(SalaryAdjustment.id)
            .where(
                SalaryAdjustment.component_id == comp.id,
                SalaryAdjustment.status == SalaryAdjustmentStatus.PENDING,
            )
            .limit(1)
        )
        if pending_adjustment is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "Calculation metadata cannot change while the component has a pending "
                    "salary adjustment"
                ),
            )
    if blocked_calculation_changes and calculation_locked:
        raise HTTPException(
            status_code=409,
            detail=(
                "Calculation metadata is immutable after a component has been used "
                "in payroll; create a new effective-dated component instead"
            ),
        )
    before = _component_catalog_snapshot(comp)
    for field, value in data.items():
        setattr(comp, field, value)
    comp.updated_at = _next_component_timestamp(comp.updated_at)
    session.flush()
    audit.record(
        session,
        action="component.update",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=comp.id,
        detail={
            "changed": sorted(data.keys()),
            "before": before,
            "after": _component_catalog_snapshot(comp),
            "reason": body.reason,
            "legacy_allowance_classification": legacy_allowance_classification,
        },
    )
    response = _component_out(comp, calculation_locked=calculation_locked)
    session.commit()
    return response


@router.post("/{component_id}/deactivate", response_model=ComponentOut)
def deactivate_component(
    component_id: int,
    body: ComponentLifecycleBody,
    principal: Principal = Depends(component_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> ComponentOut:
    component = _lock_component_for_catalog(session, component_id)
    if component is None:
        raise HTTPException(status_code=404, detail="Salary component not found")
    calculation_locked = bool(_calculation_locked_component_ids(session, {component.id}))
    if component.is_deleted:
        response = _component_out(component, calculation_locked=calculation_locked)
        session.commit()
        return response
    _assert_expected_component_timestamp(component, body.expected_updated_at)
    if component.component_type is ComponentType.ALLOWANCE and component.allowance_kind is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Classify the legacy allowance as fixed or floating before deactivation",
        )
    pending_adjustment = session.scalar(
        select(SalaryAdjustment.id)
        .where(
            SalaryAdjustment.component_id == component.id,
            SalaryAdjustment.status == SalaryAdjustmentStatus.PENDING,
        )
        .limit(1)
    )
    if pending_adjustment is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Salary component has a pending salary adjustment",
        )
    before = _component_catalog_snapshot(component)
    changed_at = _next_component_timestamp(component.updated_at)
    component.is_deleted = True
    component.deleted_at = changed_at
    component.updated_at = changed_at
    session.flush()
    audit.record(
        session,
        action="component.deactivate",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=component.id,
        detail={
            "reason": body.reason,
            "before": before,
            "after": _component_catalog_snapshot(component),
        },
    )
    response = _component_out(component, calculation_locked=calculation_locked)
    session.commit()
    return response


@router.post("/{component_id}/restore", response_model=ComponentOut)
def restore_component(
    component_id: int,
    body: ComponentLifecycleBody,
    principal: Principal = Depends(component_catalog_write_dependency),
    session: Session = Depends(get_session),
) -> ComponentOut:
    component = _lock_component_for_catalog(session, component_id)
    if component is None:
        raise HTTPException(status_code=404, detail="Salary component not found")
    calculation_locked = bool(_calculation_locked_component_ids(session, {component.id}))
    if not component.is_deleted:
        response = _component_out(component, calculation_locked=calculation_locked)
        session.commit()
        return response
    _assert_expected_component_timestamp(component, body.expected_updated_at)
    if component.component_type is ComponentType.ALLOWANCE and component.allowance_kind is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Classify the legacy allowance as fixed or floating before restoration",
        )
    before = _component_catalog_snapshot(component)
    component.is_deleted = False
    component.deleted_at = None
    component.updated_at = _next_component_timestamp(component.updated_at)
    session.flush()
    audit.record(
        session,
        action="component.restore",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=component.id,
        detail={
            "reason": body.reason,
            "before": before,
            "after": _component_catalog_snapshot(component),
        },
    )
    response = _component_out(component, calculation_locked=calculation_locked)
    session.commit()
    return response


# ------------------- 员工薪资结构 -------------------
structure_router = APIRouter(prefix="/api/employees", tags=["comp"])


class StructureItem(BaseModel):
    component_id: int
    amount: Decimal
    effective_from: date
    effective_to: date | None
    source_adjustment_id: int | None
    source_reason: str | None
    source_attachment_url: str | None

    model_config = {"from_attributes": True}


class StructureHistoryItem(StructureItem):
    id: int
    revision: int
    component_code: str
    component_name: str
    component_type: ComponentType
    component_is_active: bool


class CompaOut(BaseModel):
    total: Decimal
    band_status: str
    compa_ratio: Decimal | None
    band_min: Decimal | None
    band_mid: Decimal | None
    band_max: Decimal | None


class StructureResponse(BaseModel):
    items: list[StructureItem]
    compa: CompaOut


class SetComponentBody(BaseModel):
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    effective_from: date
    correction_reason: str | None = Field(default=None, max_length=1000)
    attachment_url: str | None = Field(default=None, max_length=512)

    @field_validator("correction_reason", mode="before")
    @classmethod
    def strip_optional_audit_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("attachment_url", mode="before")
    @classmethod
    def validate_attachment_url(cls, value: object) -> object:
        return optional_http_url(value)


class InitialStructureComponentBody(BaseModel):
    component_id: int = Field(gt=0)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    reason: str | None = Field(default=None, max_length=1000)
    attachment_url: str | None = Field(default=None, max_length=512)

    @field_validator("reason", mode="before")
    @classmethod
    def strip_optional_audit_text(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @field_validator("attachment_url", mode="before")
    @classmethod
    def validate_attachment_url(cls, value: object) -> object:
        return optional_http_url(value)


class InitialStructureBody(BaseModel):
    effective_from: date
    items: list[InitialStructureComponentBody] = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def require_unique_components(self) -> InitialStructureBody:
        component_ids = [item.component_id for item in self.items]
        if len(component_ids) != len(set(component_ids)):
            raise ValueError("initial salary structure component_id values must be unique")
        return self


def _component_snapshot(record: EmployeeSalaryStructure | None) -> dict[str, object]:
    if record is None:
        return {"record_exists": False}
    return {
        "record_exists": True,
        "structure_id": record.id,
        "component_id": record.component_id,
        "amount": decimal_text(record.amount),
        "effective_from": str(record.effective_from),
        "effective_to": str(record.effective_to) if record.effective_to else None,
    }


def _component_effective_on(
    session: Session, employee_id: int, component_id: int, on_date: date
) -> EmployeeSalaryStructure | None:
    return session.scalars(
        select(EmployeeSalaryStructure)
        .where(
            EmployeeSalaryStructure.employee_id == employee_id,
            EmployeeSalaryStructure.component_id == component_id,
            EmployeeSalaryStructure.effective_from <= on_date,
            (EmployeeSalaryStructure.effective_to.is_(None))
            | (EmployeeSalaryStructure.effective_to > on_date),
        )
        .order_by(EmployeeSalaryStructure.effective_from.desc())
        .limit(1)
    ).first()


def _affected_reopened_structure_batches(
    session: Session, employee_id: int, effective_from: date
) -> list[PayrollBatch]:
    """Return the reopened result rounds affected by a structure source change.

    ``assert_structure_effective_date_mutable`` has already taken row locks for
    every potentially affected batch.  We intentionally look for any persisted
    result for the employee, rather than active-round results: a reopened draft
    has incremented its batch version before the replacement results exist.
    """
    has_prior_result = (
        select(PayrollResult.id)
        .where(
            PayrollResult.batch_id == PayrollBatch.id,
            PayrollResult.employee_id == employee_id,
        )
        .exists()
    )
    affected_period = first_affected_employee_structure_period(
        session,
        employee_id=employee_id,
        effective_from=effective_from,
    )
    return list(
        session.scalars(
            select(PayrollBatch)
            .where(
                PayrollBatch.status == BatchStatus.DRAFT,
                PayrollBatch.version > 1,
                PayrollBatch.period >= affected_period,
                has_prior_result,
            )
            .order_by(PayrollBatch.id)
        ).all()
    )


def _require_reopened_structure_correction_scope(
    session: Session,
    *,
    principal: Principal,
    employee_id: int,
    effective_from: date,
) -> None:
    """Require the correction grant for every historical batch organization."""
    batches = _affected_reopened_structure_batches(session, employee_id, effective_from)
    if not batches:
        raise HTTPException(status_code=409, detail="未找到需要重算的已解锁薪资批次")
    for batch in batches:
        historical_org = session.scalar(
            select(PayrollResult.org_unit_id)
            .where(
                PayrollResult.batch_id == batch.id,
                PayrollResult.employee_id == employee_id,
            )
            .order_by(PayrollResult.batch_version.desc(), PayrollResult.version.desc())
            .limit(1)
        )
        if not permission_org_scope_allows(
            session,
            principal,
            Perm.PAYROLL_CORRECT,
            historical_org,
        ):
            raise HTTPException(status_code=404, detail="员工不存在或不可见")


def _record_reopened_structure_correction(
    session: Session,
    *,
    employee_id: int,
    effective_from: date,
    before: dict[str, object],
    after: dict[str, object],
    reason: str,
    attachment_url: str | None,
    principal: Principal,
) -> None:
    batches = _affected_reopened_structure_batches(session, employee_id, effective_from)
    if not batches:
        # The guard identified the correction round, so failing closed here is
        # preferable to modifying a payroll input without its required audit
        # and rerun link.
        raise HTTPException(status_code=409, detail="未找到需要重算的已解锁薪资批次")
    for batch in batches:
        session.add(
            AdjustmentRecord(
                batch_id=batch.id,
                batch_version=batch.version,
                employee_id=employee_id,
                dispute_id=None,
                item="SALARY_STRUCTURE_SOURCE",
                before_value=before,
                after_value=after,
                reason=reason,
                applicant_id=principal.user_id,
                approver_id=principal.user_id,
                attachment_url=attachment_url,
                recompute_result={
                    "status": "PENDING_RERUN",
                    "batch_version": batch.version,
                },
            )
        )


def _employee_or_404(session: Session, principal: Principal, employee_id: int, permission: str):
    emp = EmployeeRepository(
        session,
        org_scope=resolve_permission_org_scope(session, principal, permission),
    ).get(employee_id)
    if emp is None:
        raise HTTPException(status_code=404, detail="员工不存在或不可见")
    return emp


@structure_router.get("/{employee_id}/structure", response_model=StructureResponse)
def get_structure(
    employee_id: int,
    on_date: date | None = Query(None),
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_READ)),
    session: Session = Depends(get_session),
) -> StructureResponse:
    emp = _employee_or_404(session, principal, employee_id, Perm.STRUCTURE_READ)
    day = on_date or date.today()
    items = current_structure(session, employee_id, day)
    compa = compa_ratio(
        session, employee_id=employee_id, job_grade_id=emp.job_grade_id, on_date=day
    )
    response = StructureResponse(
        items=[StructureItem.model_validate(i) for i in items],
        compa=CompaOut(
            total=compa.total,
            band_status=compa.band_status.value,
            compa_ratio=compa.compa_ratio,
            band_min=compa.band_min,
            band_mid=compa.band_mid,
            band_max=compa.band_max,
        ),
    )
    audit.record(
        session,
        action="structure.view",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={"on_date": str(day), "returned_count": len(items)},
    )
    session.commit()
    return response


@structure_router.put(
    "/{employee_id}/initial-structure",
    response_model=list[StructureItem],
    status_code=status.HTTP_201_CREATED,
)
def set_initial_structure(
    employee_id: int,
    body: InitialStructureBody,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_WRITE)),
    session: Session = Depends(get_session),
) -> list[EmployeeSalaryStructure]:
    """Create an employee's complete first salary structure in one transaction.

    The per-component legacy route intentionally cannot be used to add a
    second component after setup: doing so would be an unapproved salary
    change.  This endpoint preserves legitimate multi-component onboarding
    without leaving a time window for piecemeal bypasses.
    """

    _employee_or_404(session, principal, employee_id, Perm.STRUCTURE_WRITE)
    locked_employee = lock_employee_salary_structure(session, employee_id=employee_id)
    scope = resolve_permission_org_scope(session, principal, Perm.STRUCTURE_WRITE)
    if locked_employee.is_deleted or (
        scope is not None and locked_employee.org_unit_id not in scope
    ):
        raise HTTPException(
            status_code=404, detail="Employee not found or outside organization scope"
        )
    has_any_structure = (
        session.scalar(
            select(EmployeeSalaryStructure.id)
            .where(EmployeeSalaryStructure.employee_id == employee_id)
            .with_for_update()
            .limit(1)
        )
        is not None
    )
    if has_any_structure:
        raise HTTPException(
            status_code=409,
            detail="Initial salary structure already exists; use a salary adjustment approval",
        )
    component_ids = {item.component_id for item in body.items}
    components = {
        component.id: component
        for component in session.scalars(
            select(SalaryComponentDef)
            .where(SalaryComponentDef.id.in_(component_ids))
            .with_for_update(read=True)
        ).all()
    }
    missing_components = sorted(component_ids - components.keys())
    if missing_components:
        missing_component_text = ", ".join(str(value) for value in missing_components)
        raise HTTPException(
            status_code=404,
            detail=f"Salary component not found: {missing_component_text}",
        )
    inactive_components = sorted(
        component_id for component_id, component in components.items() if component.is_deleted
    )
    if inactive_components:
        inactive_component_text = ", ".join(str(value) for value in inactive_components)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Salary component is inactive: {inactive_component_text}",
        )
    for item in body.items:
        component = components[item.component_id]
        if component.component_type in (ComponentType.ALLOWANCE, ComponentType.HOUSING) and (
            not item.reason or not item.attachment_url
        ):
            raise HTTPException(
                status_code=422,
                detail=(
                    "Manual allowance or housing initial setup requires "
                    "reason and evidence attachment"
                ),
            )
    try:
        correction_round = assert_structure_effective_date_mutable(
            session, employee_id=employee_id, effective_from=body.effective_from
        )
    except PayrollSourceLockedError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    if correction_round:
        raise HTTPException(
            status_code=409,
            detail="A reopened payroll source requires the dedicated correction workflow",
        )
    records = [
        set_component_amount(
            session,
            employee_id=employee_id,
            component_id=item.component_id,
            amount=item.amount,
            effective_from=body.effective_from,
            source_reason=item.reason,
            source_attachment_url=item.attachment_url,
        )
        for item in body.items
    ]
    audit.record(
        session,
        action="structure.initial_set",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={
            "effective_from": str(body.effective_from),
            "component_ids": sorted(component_ids),
            "component_count": len(records),
            "manual_allowance_component_ids": sorted(
                item.component_id
                for item in body.items
                if components[item.component_id].component_type is ComponentType.ALLOWANCE
            ),
        },
    )
    session.commit()
    return records


@structure_router.put("/{employee_id}/structure/{component_id}", response_model=StructureItem)
def set_structure(
    employee_id: int,
    component_id: int,
    body: SetComponentBody,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_WRITE)),
    session: Session = Depends(get_session),
) -> EmployeeSalaryStructure:
    _employee_or_404(session, principal, employee_id, Perm.STRUCTURE_WRITE)
    # Lock the stable employee parent before inspecting the absence/presence of
    # a structure row.  This closes the empty-row race that could otherwise
    # turn two concurrent initial writes into an unapproved salary revision.
    locked_employee = lock_employee_salary_structure(session, employee_id=employee_id)
    scope = resolve_permission_org_scope(session, principal, Perm.STRUCTURE_WRITE)
    if locked_employee.is_deleted or (
        scope is not None and locked_employee.org_unit_id not in scope
    ):
        raise HTTPException(
            status_code=404, detail="Employee not found or outside organization scope"
        )
    component = session.scalars(
        select(SalaryComponentDef)
        .where(SalaryComponentDef.id == component_id)
        .with_for_update(read=True)
    ).first()
    if component is None:
        raise HTTPException(status_code=404, detail="Salary component not found")
    has_any_structure = (
        session.scalar(
            select(EmployeeSalaryStructure.id)
            .where(EmployeeSalaryStructure.employee_id == employee_id)
            .with_for_update()
            .limit(1)
        )
        is not None
    )
    has_component_history = (
        session.scalar(
            select(EmployeeSalaryStructure.id)
            .where(
                EmployeeSalaryStructure.employee_id == employee_id,
                EmployeeSalaryStructure.component_id == component_id,
            )
            .with_for_update()
            .limit(1)
        )
        is not None
    )
    if component.is_deleted and not has_component_history:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Salary component is inactive",
        )
    if component.component_type in (ComponentType.ALLOWANCE, ComponentType.HOUSING) and (
        not body.correction_reason or not body.attachment_url
    ):
        raise HTTPException(
            status_code=422,
            detail="手工补贴或房补必须记录原因和依据附件",
        )
    try:
        correction_round = assert_structure_effective_date_mutable(
            session, employee_id=employee_id, effective_from=body.effective_from
        )
        if correction_round:
            _require_reopened_structure_correction_scope(
                session,
                principal=principal,
                employee_id=employee_id,
                effective_from=body.effective_from,
            )
            if not body.correction_reason:
                raise HTTPException(
                    status_code=422,
                    detail="更正已解锁批次的薪资结构必须填写更正原因",
                )
            if not body.attachment_url:
                raise HTTPException(
                    status_code=422,
                    detail="更正已解锁批次的薪资结构必须上传证明附件",
                )
        elif has_any_structure:
            # S8 maker-checker boundary: this legacy endpoint may establish
            # one first component for backwards-compatible onboarding only.
            # Every later row -- including a previously unused component --
            # must go through the approval route.  Multi-component onboarding
            # uses the atomic initial-structure endpoint above.
            raise HTTPException(
                status_code=409,
                detail=(
                    "Existing salary structures must be changed or extended through "
                    "a salary adjustment approval"
                ),
            )
        before = _component_snapshot(
            _component_effective_on(session, employee_id, component_id, body.effective_from)
        )
        rec = set_component_amount(
            session,
            employee_id=employee_id,
            component_id=component_id,
            amount=body.amount,
            effective_from=body.effective_from,
            source_reason=body.correction_reason,
            source_attachment_url=body.attachment_url,
        )
        after = _component_snapshot(
            _component_effective_on(session, employee_id, component_id, body.effective_from)
        )
        if correction_round:
            if before == after:
                raise HTTPException(status_code=422, detail="更正必须实际改变参与计薪的薪资结构")
            _record_reopened_structure_correction(
                session,
                employee_id=employee_id,
                effective_from=body.effective_from,
                before=before,
                after=after,
                reason=body.correction_reason or "",
                attachment_url=body.attachment_url,
                principal=principal,
            )
    except (PayrollSourceLockedError, StructureError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="structure.set",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={
            "component_id": component_id,
            "effective_from": str(body.effective_from),
            "correction": correction_round,
            "before": before if correction_round else None,
            "after": after if correction_round else None,
            # The complete business evidence belongs to the protected salary
            # structure record.  Audit logs are globally readable by the
            # audit role, so retain only evidence-presence metadata here.
            "has_reason": body.correction_reason is not None,
            "evidence_attached": body.attachment_url is not None,
            "manual_allowance": component.component_type is ComponentType.ALLOWANCE,
        },
    )
    session.commit()
    return rec


@structure_router.get("/{employee_id}/structure/history", response_model=list[StructureHistoryItem])
def structure_history(
    employee_id: int,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_READ)),
    session: Session = Depends(get_session),
) -> list[StructureHistoryItem]:
    _employee_or_404(session, principal, employee_id, Perm.STRUCTURE_READ)
    statement = (
        select(EmployeeSalaryStructure, SalaryComponentDef)
        .join(SalaryComponentDef, SalaryComponentDef.id == EmployeeSalaryStructure.component_id)
        .where(EmployeeSalaryStructure.employee_id == employee_id)
        .order_by(
            SalaryComponentDef.sort_order,
            SalaryComponentDef.code,
            EmployeeSalaryStructure.effective_from,
            EmployeeSalaryStructure.revision,
            EmployeeSalaryStructure.id,
        )
    )
    rows = list(session.execute(statement).all())
    records = [
        StructureHistoryItem(
            id=record.id,
            revision=record.revision,
            component_id=record.component_id,
            amount=record.amount,
            effective_from=record.effective_from,
            effective_to=record.effective_to,
            source_adjustment_id=record.source_adjustment_id,
            source_reason=record.source_reason,
            source_attachment_url=record.source_attachment_url,
            component_code=component.code,
            component_name=component.name,
            component_type=component.component_type,
            component_is_active=not component.is_deleted,
        )
        for record, component in rows
    ]
    audit.record(
        session,
        action="structure.history.view",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={"returned_count": len(records)},
    )
    session.commit()
    return records
