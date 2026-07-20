from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import principal_scope, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.comp.service import (
    StructureError,
    compa_ratio,
    current_structure,
    set_component_amount,
)
from app.db.session import get_session
from app.models.comp import ComponentType, EmployeeSalaryStructure, SalaryComponentDef
from app.repositories.employee import EmployeeRepository

router = APIRouter(prefix="/api/salary-components", tags=["comp"])


class ComponentCreate(BaseModel):
    code: str = Field(min_length=1, max_length=32)
    name: str = Field(min_length=1, max_length=64)
    component_type: ComponentType
    taxable: bool = True
    in_social_base: bool = False
    in_housing_base: bool = False
    sort_order: int = 0


class ComponentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=64)
    taxable: bool | None = None
    in_social_base: bool | None = None
    in_housing_base: bool | None = None
    sort_order: int | None = None


class ComponentOut(BaseModel):
    id: int
    code: str
    name: str
    component_type: ComponentType
    taxable: bool
    in_social_base: bool
    in_housing_base: bool
    sort_order: int

    model_config = {"from_attributes": True}


@router.get("", response_model=list[ComponentOut])
def list_components(
    _p: Principal = Depends(require_permission(Perm.STRUCTURE_READ)),
    session: Session = Depends(get_session),
) -> list[SalaryComponentDef]:
    stmt = (
        select(SalaryComponentDef)
        .where(SalaryComponentDef.is_deleted.is_(False))
        .order_by(SalaryComponentDef.sort_order, SalaryComponentDef.code)
    )
    return list(session.scalars(stmt).all())


@router.post("", response_model=ComponentOut, status_code=status.HTTP_201_CREATED)
def create_component(
    body: ComponentCreate,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_WRITE)),
    session: Session = Depends(get_session),
) -> SalaryComponentDef:
    comp = SalaryComponentDef(**body.model_dump())
    session.add(comp)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="组件编码已存在") from None
    audit.record(
        session,
        action="component.create",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=comp.id,
        detail={"code": comp.code},
    )
    session.commit()
    return comp


@router.patch("/{component_id}", response_model=ComponentOut)
def update_component(
    component_id: int,
    body: ComponentUpdate,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_WRITE)),
    session: Session = Depends(get_session),
) -> SalaryComponentDef:
    comp = session.get(SalaryComponentDef, component_id)
    if comp is None or comp.is_deleted:
        raise HTTPException(status_code=404, detail="组件不存在")
    data = body.model_dump(exclude_unset=True)
    for field, value in data.items():
        setattr(comp, field, value)
    session.flush()
    audit.record(
        session,
        action="component.update",
        actor=(principal.user_id, principal.username),
        target_type="salary_component_def",
        target_id=comp.id,
        detail={"changed": sorted(data.keys())},
    )
    session.commit()
    return comp


# ------------------- 员工薪资结构 -------------------
structure_router = APIRouter(prefix="/api/employees", tags=["comp"])


class StructureItem(BaseModel):
    component_id: int
    amount: Decimal
    effective_from: date
    effective_to: date | None

    model_config = {"from_attributes": True}


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
    amount: Decimal = Field(ge=0)
    effective_from: date


def _employee_or_404(session: Session, principal: Principal, employee_id: int):
    emp = EmployeeRepository(session, org_scope=principal_scope(principal)).get(employee_id)
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
    emp = _employee_or_404(session, principal, employee_id)
    day = on_date or date.today()
    items = current_structure(session, employee_id, day)
    compa = compa_ratio(
        session, employee_id=employee_id, job_grade_id=emp.job_grade_id, on_date=day
    )
    return StructureResponse(
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


@structure_router.put("/{employee_id}/structure/{component_id}", response_model=StructureItem)
def set_structure(
    employee_id: int,
    component_id: int,
    body: SetComponentBody,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_WRITE)),
    session: Session = Depends(get_session),
) -> EmployeeSalaryStructure:
    _employee_or_404(session, principal, employee_id)
    if session.get(SalaryComponentDef, component_id) is None:
        raise HTTPException(status_code=404, detail="组件不存在")
    try:
        rec = set_component_amount(
            session,
            employee_id=employee_id,
            component_id=component_id,
            amount=body.amount,
            effective_from=body.effective_from,
        )
    except StructureError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="structure.set",
        actor=(principal.user_id, principal.username),
        target_type="employee",
        target_id=employee_id,
        detail={"component_id": component_id, "effective_from": str(body.effective_from)},
    )
    session.commit()
    return rec


@structure_router.get("/{employee_id}/structure/history", response_model=list[StructureItem])
def structure_history(
    employee_id: int,
    principal: Principal = Depends(require_permission(Perm.STRUCTURE_READ)),
    session: Session = Depends(get_session),
) -> list[EmployeeSalaryStructure]:
    _employee_or_404(session, principal, employee_id)
    stmt = (
        select(EmployeeSalaryStructure)
        .where(EmployeeSalaryStructure.employee_id == employee_id)
        .order_by(
            EmployeeSalaryStructure.component_id,
            EmployeeSalaryStructure.effective_from,
        )
    )
    return list(session.scalars(stmt).all())
