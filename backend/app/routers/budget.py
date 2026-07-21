"""Organization-scoped labor-budget maintenance."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.db.session import get_session
from app.models.budget import LaborBudget
from app.models.org import OrgType
from app.repositories.budget import LaborBudgetRepository
from app.repositories.org import OrgUnitRepository

router = APIRouter(prefix="/api/budgets", tags=["budget"])


def _month_start_or_422(period: date) -> date:
    if period.day != 1:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="period must be the first day of its accounting month",
        )
    return period


class BudgetWrite(BaseModel):
    org_unit_id: int = Field(gt=0)
    period: date
    headcount_budget: int = Field(ge=0, le=1_000_000)
    labor_cost_budget: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("period")
    @classmethod
    def period_is_month_start(cls, value: date) -> date:
        return _month_start_or_422(value)

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class BudgetUpdate(BaseModel):
    version: int = Field(gt=0)
    org_unit_id: int | None = Field(default=None, gt=0)
    period: date | None = None
    headcount_budget: int | None = Field(default=None, ge=0, le=1_000_000)
    labor_cost_budget: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    note: str | None = Field(default=None, max_length=500)

    @field_validator("period")
    @classmethod
    def updated_period_is_month_start(cls, value: date | None) -> date | None:
        return _month_start_or_422(value) if value is not None else value

    @field_validator("note", mode="before")
    @classmethod
    def normalize_note(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value

    @model_validator(mode="after")
    def includes_change(self) -> BudgetUpdate:
        if self.model_fields_set == {"version"}:
            raise ValueError("at least one budget field must be changed")
        return self


class BudgetOut(BaseModel):
    id: int
    org_unit_id: int
    period: date
    headcount_budget: int
    labor_cost_budget: Decimal
    note: str | None
    version: int

    model_config = {"from_attributes": True}


class BudgetPage(BaseModel):
    items: list[BudgetOut]
    total: int
    page: int
    page_size: int


def _repo(session: Session, principal: Principal, permission: str) -> LaborBudgetRepository:
    return LaborBudgetRepository(
        session,
        org_scope=resolve_permission_org_scope(session, principal, permission),
    )


def _visible_store_or_404(
    session: Session, org_scope: frozenset[int] | None, org_unit_id: int
) -> None:
    org_unit = OrgUnitRepository(session, org_scope=org_scope).get(org_unit_id)
    if org_unit is None:
        raise HTTPException(status_code=404, detail="organization does not exist or is not visible")
    # Payroll results are generated at the store.  Restricting budget entry to
    # the same leaf level makes region/group budget comparisons an unambiguous
    # sum, rather than double-counting parent and child planning envelopes.
    if org_unit.type != OrgType.STORE:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="labor budgets can only be maintained for active stores",
        )


def _snapshot(budget: LaborBudget) -> dict[str, object]:
    """Return the non-sensitive business state needed to reconstruct an edit."""
    return {
        "org_unit_id": budget.org_unit_id,
        "period": str(budget.period),
        "headcount_budget": budget.headcount_budget,
        "labor_cost_budget": str(budget.labor_cost_budget),
        "note": budget.note,
        "version": budget.version,
    }


@router.get("", response_model=BudgetPage)
def list_budgets(
    org_unit_id: int | None = Query(None, gt=0),
    period: date | None = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=500),
    principal: Principal = Depends(require_permission(Perm.BUDGET_READ)),
    session: Session = Depends(get_session),
) -> BudgetPage:
    if period is not None:
        _month_start_or_422(period)
    result = _repo(session, principal, Perm.BUDGET_READ).list_filtered(
        org_unit_id=org_unit_id,
        period=period,
        page=page,
        page_size=page_size,
    )
    return BudgetPage(
        items=[BudgetOut.model_validate(item) for item in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )


@router.post("", response_model=BudgetOut, status_code=status.HTTP_201_CREATED)
def create_budget(
    body: BudgetWrite,
    principal: Principal = Depends(require_permission(Perm.BUDGET_WRITE)),
    session: Session = Depends(get_session),
) -> LaborBudget:
    org_scope = resolve_permission_org_scope(session, principal, Perm.BUDGET_WRITE)
    _visible_store_or_404(session, org_scope, body.org_unit_id)
    budget = LaborBudget(**body.model_dump())
    session.add(budget)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a budget already exists for this organization and period",
        ) from None
    audit.record(
        session,
        action="budget.create",
        actor=(principal.user_id, principal.username),
        target_type="labor_budget",
        target_id=budget.id,
        detail={"before": None, "after": _snapshot(budget)},
    )
    session.commit()
    return budget


@router.patch("/{budget_id}", response_model=BudgetOut)
def update_budget(
    budget_id: int,
    body: BudgetUpdate,
    principal: Principal = Depends(require_permission(Perm.BUDGET_WRITE)),
    session: Session = Depends(get_session),
) -> LaborBudget:
    org_scope = resolve_permission_org_scope(session, principal, Perm.BUDGET_WRITE)
    budget = LaborBudgetRepository(session, org_scope=org_scope).get_for_update(budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget does not exist or is not visible")
    expected_version = body.version
    if budget.version != expected_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="budget changed by another user; refresh and retry",
        )
    before = _snapshot(budget)
    data = body.model_dump(exclude_unset=True, exclude={"version"})
    if "org_unit_id" in data:
        _visible_store_or_404(session, org_scope, data["org_unit_id"])
    for field, value in data.items():
        setattr(budget, field, value)
    budget.version += 1
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a budget already exists for this organization and period",
        ) from None
    audit.record(
        session,
        action="budget.update",
        actor=(principal.user_id, principal.username),
        target_type="labor_budget",
        target_id=budget.id,
        detail={
            "changed": sorted(data.keys()),
            "from_version": expected_version,
            "before": before,
            "after": _snapshot(budget),
        },
    )
    session.commit()
    return budget


@router.delete("/{budget_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_budget(
    budget_id: int,
    version: int = Query(..., gt=0),
    principal: Principal = Depends(require_permission(Perm.BUDGET_WRITE)),
    session: Session = Depends(get_session),
) -> None:
    budget = _repo(session, principal, Perm.BUDGET_WRITE).get_for_update(budget_id)
    if budget is None:
        raise HTTPException(status_code=404, detail="budget does not exist or is not visible")
    if budget.version != version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="budget changed by another user; refresh and retry",
        )
    before = _snapshot(budget)
    audit.record(
        session,
        action="budget.delete",
        actor=(principal.user_id, principal.username),
        target_type="labor_budget",
        target_id=budget.id,
        detail={"before": before, "after": None},
    )
    session.delete(budget)
    session.commit()
