"""Effective-dated, finalized payroll policy management.

Policies are group-level reference data.  They are intentionally separate from
organization-tree scope because payroll selection is driven by an employee's
declared social-insurance city, not by the employee's reporting line.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_policy import PayrollPolicy
from app.payroll.guards import lock_payroll_input_mutation
from app.payroll.social_tax import (
    ContributionKind,
    ContributionRule,
    DerivedIncomeRule,
    SocialInsurancePolicyInput,
    TaxBracket,
    TaxPolicyInput,
    validate_derived_income_rules,
    validate_social_insurance_policy,
    validate_tax_policy,
)

router = APIRouter(prefix="/api/payroll-policies", tags=["payroll-policies"])


class SocialRuleBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: ContributionKind
    employee_rate: Decimal = Field(ge=0, le=1, max_digits=8, decimal_places=6)
    employer_rate: Decimal = Field(ge=0, le=1, max_digits=8, decimal_places=6)
    base_min: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    base_max: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)

    @model_validator(mode="after")
    def base_range_is_valid(self) -> SocialRuleBody:
        if self.base_max is not None and self.base_max < self.base_min:
            raise ValueError("base_max must not be below base_min")
        return self


class TaxBracketBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    upper_bound: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    rate: Decimal = Field(ge=0, le=1, max_digits=8, decimal_places=6)
    quick_deduction: Decimal = Field(ge=0, max_digits=14, decimal_places=2)


class DerivedIncomeRuleBody(BaseModel):
    """City-policy treatment of an earnings line calculated by the engine."""

    model_config = ConfigDict(extra="forbid")

    code: Literal["OVERTIME", "HOLIDAY"]
    taxable: bool
    in_social_base: bool
    in_housing_base: bool


class PayrollPolicyCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    city: str = Field(min_length=1, max_length=32)
    effective_from: date
    social_rules: list[SocialRuleBody] = Field(min_length=1)
    monthly_basic_deduction: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    tax_brackets: list[TaxBracketBody] = Field(min_length=1)
    derived_income_rules: list[DerivedIncomeRuleBody] = Field(default_factory=list)

    @field_validator("city")
    @classmethod
    def normalize_city(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("city must not be blank")
        return value

    @field_validator("effective_from")
    @classmethod
    def effective_date_is_month_start(cls, value: date) -> date:
        if value.day != 1:
            raise ValueError("effective_from must be the first day of a payroll month")
        return value


class PayrollPolicyUpdate(BaseModel):
    """All changed fields are optional, but a PATCH cannot contain nulls or be empty."""

    model_config = ConfigDict(extra="forbid")

    city: str | None = Field(default=None, min_length=1, max_length=32)
    effective_from: date | None = None
    social_rules: list[SocialRuleBody] | None = Field(default=None, min_length=1)
    monthly_basic_deduction: Decimal | None = Field(
        default=None, ge=0, max_digits=14, decimal_places=2
    )
    tax_brackets: list[TaxBracketBody] | None = Field(default=None, min_length=1)
    derived_income_rules: list[DerivedIncomeRuleBody] | None = None

    @field_validator("city")
    @classmethod
    def normalize_city(cls, value: str | None) -> str | None:
        if value is None:
            return value
        value = value.strip()
        if not value:
            raise ValueError("city must not be blank")
        return value

    @field_validator("effective_from")
    @classmethod
    def effective_date_is_month_start(cls, value: date | None) -> date | None:
        if value is not None and value.day != 1:
            raise ValueError("effective_from must be the first day of a payroll month")
        return value

    @model_validator(mode="after")
    def has_non_null_change(self) -> PayrollPolicyUpdate:
        if not self.model_fields_set:
            raise ValueError("at least one policy field must be changed")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("policy fields cannot be null")
        return self


class PayrollPolicyOut(BaseModel):
    id: int
    city: str
    effective_from: date
    social_rules: list[SocialRuleBody]
    monthly_basic_deduction: Decimal
    tax_brackets: list[TaxBracketBody]
    derived_income_rules: list[DerivedIncomeRuleBody]
    is_finalized: bool
    finalized_by: int | None
    finalized_at: datetime | None

    model_config = ConfigDict(from_attributes=True)


def _decimal(value: object) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def _policy_input(
    *,
    city: str,
    social_rules: Sequence[dict[str, Any]],
    monthly_basic_deduction: Decimal,
    tax_brackets: Sequence[dict[str, Any]],
    derived_income_rules: Sequence[dict[str, Any]],
) -> tuple[SocialInsurancePolicyInput, TaxPolicyInput, tuple[DerivedIncomeRule, ...]]:
    """Convert persisted JSON only at the trusted, validated API boundary."""

    social = SocialInsurancePolicyInput(
        city=city,
        rules=tuple(
            ContributionRule(
                kind=ContributionKind(rule["kind"]),
                employee_rate=_decimal(rule["employee_rate"]),
                employer_rate=_decimal(rule["employer_rate"]),
                base_min=_decimal(rule["base_min"]),
                base_max=_decimal(rule["base_max"]) if rule.get("base_max") is not None else None,
            )
            for rule in social_rules
        ),
    )
    tax = TaxPolicyInput(
        monthly_basic_deduction=_decimal(monthly_basic_deduction),
        brackets=tuple(
            TaxBracket(
                upper_bound=(
                    _decimal(bracket["upper_bound"])
                    if bracket.get("upper_bound") is not None
                    else None
                ),
                rate=_decimal(bracket["rate"]),
                quick_deduction=_decimal(bracket["quick_deduction"]),
            )
            for bracket in tax_brackets
        ),
    )
    derived = tuple(
        DerivedIncomeRule(
            code=str(rule["code"]),
            taxable=rule["taxable"],
            in_social_base=rule["in_social_base"],
            in_housing_base=rule["in_housing_base"],
        )
        for rule in derived_income_rules
    )
    return social, tax, derived


def _validate_policy(
    *,
    city: str,
    social_rules: Sequence[dict[str, Any]],
    monthly_basic_deduction: Decimal,
    tax_brackets: Sequence[dict[str, Any]],
    derived_income_rules: Sequence[dict[str, Any]],
    require_complete_derived_income: bool = False,
    status_code: int = status.HTTP_422_UNPROCESSABLE_CONTENT,
) -> None:
    try:
        social, tax, derived = _policy_input(
            city=city,
            social_rules=social_rules,
            monthly_basic_deduction=monthly_basic_deduction,
            tax_brackets=tax_brackets,
            derived_income_rules=derived_income_rules,
        )
        validate_social_insurance_policy(social)
        validate_tax_policy(tax)
        validate_derived_income_rules(derived, require_complete=require_complete_derived_income)
    except (KeyError, TypeError, ValueError, InvalidOperation) as exc:
        raise HTTPException(
            status_code=status_code, detail=f"Invalid payroll policy: {exc}"
        ) from None


def _serialize_social_rules(items: Sequence[SocialRuleBody]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


def _serialize_tax_brackets(items: Sequence[TaxBracketBody]) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


def _serialize_derived_income_rules(
    items: Sequence[DerivedIncomeRuleBody],
) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in items]


def _period_for_policy(effective_from: date) -> str:
    return f"{effective_from.year:04d}-{effective_from.month:02d}"


def _require_month_start(effective_from: date, *, status_code: int) -> None:
    if effective_from.day != 1:
        raise HTTPException(
            status_code=status_code,
            detail="Payroll policy effective date must be the first day of a payroll month",
        )


def _started_batch_exists(session: Session, effective_from: date) -> bool:
    """Block policy finalization unless all affected history is reopened.

    Reopening a multi-period correction is performed newest-to-oldest.  Every
    affected period is therefore an isolated draft before a policy successor
    can be finalized; any non-draft batch still has a mutable/approved result
    that must be reopened first.
    """

    lock_payroll_input_mutation(session)
    first_period = _period_for_policy(effective_from)
    batches = session.scalars(
        select(PayrollBatch)
        .where(PayrollBatch.period >= first_period)
        .order_by(PayrollBatch.period)
        .with_for_update()
    ).all()
    for batch in batches:
        if batch.status == BatchStatus.DRAFT:
            continue
        return True
    return False


def _out(policy: PayrollPolicy) -> PayrollPolicyOut:
    return PayrollPolicyOut.model_validate(policy)


@router.get("", response_model=list[PayrollPolicyOut])
def list_policies(
    city: str | None = Query(default=None, min_length=1, max_length=32),
    include_drafts: bool = Query(default=False),
    principal: Principal = Depends(require_permission(Perm.POLICY_READ)),
    session: Session = Depends(get_session),
) -> list[PayrollPolicyOut]:
    if include_drafts and not principal.has_permission(Perm.POLICY_WRITE):
        raise HTTPException(
            status_code=403, detail="Draft payroll policies require write permission"
        )
    stmt = select(PayrollPolicy)
    if city is not None:
        stmt = stmt.where(PayrollPolicy.city == city.strip())
    if not include_drafts:
        stmt = stmt.where(PayrollPolicy.is_finalized.is_(True))
    policies = session.scalars(
        stmt.order_by(PayrollPolicy.city, PayrollPolicy.effective_from.desc())
    ).all()
    return [_out(policy) for policy in policies]


@router.get("/active", response_model=PayrollPolicyOut)
def active_policy(
    city: str = Query(min_length=1, max_length=32),
    on_date: date = Query(),
    _principal: Principal = Depends(require_permission(Perm.POLICY_READ)),
    session: Session = Depends(get_session),
) -> PayrollPolicyOut:
    city = city.strip()
    if not city:
        raise HTTPException(status_code=422, detail="city must not be blank")
    policy = session.scalars(
        select(PayrollPolicy)
        .where(
            PayrollPolicy.city == city,
            PayrollPolicy.is_finalized.is_(True),
            PayrollPolicy.effective_from <= on_date,
        )
        .order_by(PayrollPolicy.effective_from.desc())
        .limit(1)
    ).first()
    if policy is None:
        raise HTTPException(
            status_code=404, detail="No finalized payroll policy applies to this city and date"
        )
    return _out(policy)


@router.get("/{policy_id}", response_model=PayrollPolicyOut)
def get_policy(
    policy_id: int,
    principal: Principal = Depends(require_permission(Perm.POLICY_READ)),
    session: Session = Depends(get_session),
) -> PayrollPolicyOut:
    policy = session.get(PayrollPolicy, policy_id)
    if policy is None:
        raise HTTPException(status_code=404, detail="Payroll policy does not exist")
    if not policy.is_finalized and not principal.has_permission(Perm.POLICY_WRITE):
        raise HTTPException(status_code=404, detail="Payroll policy does not exist")
    return _out(policy)


@router.post("", response_model=PayrollPolicyOut, status_code=status.HTTP_201_CREATED)
def create_policy(
    body: PayrollPolicyCreate,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> PayrollPolicyOut:
    social_rules = _serialize_social_rules(body.social_rules)
    tax_brackets = _serialize_tax_brackets(body.tax_brackets)
    derived_income_rules = _serialize_derived_income_rules(body.derived_income_rules)
    _require_month_start(body.effective_from, status_code=status.HTTP_422_UNPROCESSABLE_CONTENT)
    _validate_policy(
        city=body.city,
        social_rules=social_rules,
        monthly_basic_deduction=body.monthly_basic_deduction,
        tax_brackets=tax_brackets,
        derived_income_rules=derived_income_rules,
    )
    policy = PayrollPolicy(
        city=body.city,
        effective_from=body.effective_from,
        social_rules=social_rules,
        monthly_basic_deduction=body.monthly_basic_deduction,
        tax_brackets=tax_brackets,
        derived_income_rules=derived_income_rules,
        created_by=principal.user_id,
    )
    session.add(policy)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A policy already exists for this city and effective date",
        ) from None
    audit.record(
        session,
        action="payroll_policy.create",
        actor=(principal.user_id, principal.username),
        target_type="payroll_policy",
        target_id=policy.id,
        detail={"city": policy.city, "effective_from": policy.effective_from.isoformat()},
    )
    session.commit()
    return _out(policy)


@router.patch("/{policy_id}", response_model=PayrollPolicyOut)
def update_policy(
    policy_id: int,
    body: PayrollPolicyUpdate,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> PayrollPolicyOut:
    policy = session.scalars(
        select(PayrollPolicy).where(PayrollPolicy.id == policy_id).with_for_update()
    ).first()
    if policy is None:
        raise HTTPException(status_code=404, detail="Payroll policy does not exist")
    if policy.is_finalized:
        raise HTTPException(status_code=409, detail="Finalized payroll policies are immutable")

    changes = body.model_dump(exclude_unset=True)
    city = changes.get("city", policy.city)
    effective_from = changes.get("effective_from", policy.effective_from)
    social_rules = (
        _serialize_social_rules(changes["social_rules"])
        if "social_rules" in changes
        else policy.social_rules
    )
    monthly_basic_deduction = changes.get("monthly_basic_deduction", policy.monthly_basic_deduction)
    tax_brackets = (
        _serialize_tax_brackets(changes["tax_brackets"])
        if "tax_brackets" in changes
        else policy.tax_brackets
    )
    derived_income_rules = (
        _serialize_derived_income_rules(changes["derived_income_rules"])
        if "derived_income_rules" in changes
        else policy.derived_income_rules
    )
    _require_month_start(effective_from, status_code=status.HTTP_409_CONFLICT)
    _validate_policy(
        city=city,
        social_rules=social_rules,
        monthly_basic_deduction=monthly_basic_deduction,
        tax_brackets=tax_brackets,
        derived_income_rules=derived_income_rules,
    )
    policy.city = city
    policy.effective_from = effective_from
    policy.social_rules = social_rules
    policy.monthly_basic_deduction = monthly_basic_deduction
    policy.tax_brackets = tax_brackets
    policy.derived_income_rules = derived_income_rules
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A policy already exists for this city and effective date",
        ) from None
    audit.record(
        session,
        action="payroll_policy.update",
        actor=(principal.user_id, principal.username),
        target_type="payroll_policy",
        target_id=policy.id,
        detail={
            "city": policy.city,
            "effective_from": policy.effective_from.isoformat(),
            "changed": sorted(changes.keys()),
        },
    )
    session.commit()
    return _out(policy)


@router.post("/{policy_id}/finalize", response_model=PayrollPolicyOut)
def finalize_policy(
    policy_id: int,
    principal: Principal = Depends(require_permission(Perm.POLICY_WRITE)),
    session: Session = Depends(get_session),
) -> PayrollPolicyOut:
    # Match the batch-run/source-write locking order before taking the policy
    # row lock, preventing a successor policy from racing a batch snapshot.
    lock_payroll_input_mutation(session)
    policy = session.scalars(
        select(PayrollPolicy).where(PayrollPolicy.id == policy_id).with_for_update()
    ).first()
    if policy is None:
        raise HTTPException(status_code=404, detail="Payroll policy does not exist")
    if policy.is_finalized:
        raise HTTPException(status_code=409, detail="Payroll policy is already finalized")
    _require_month_start(policy.effective_from, status_code=status.HTTP_409_CONFLICT)
    _validate_policy(
        city=policy.city,
        social_rules=policy.social_rules,
        monthly_basic_deduction=policy.monthly_basic_deduction,
        tax_brackets=policy.tax_brackets,
        derived_income_rules=policy.derived_income_rules,
        require_complete_derived_income=True,
        status_code=status.HTTP_409_CONFLICT,
    )
    if _started_batch_exists(session, policy.effective_from):
        raise HTTPException(
            status_code=409,
            detail="Finalize the policy before an affected payroll batch starts",
        )
    policy.is_finalized = True
    policy.finalized_by = principal.user_id
    policy.finalized_at = datetime.now(UTC)
    session.flush()
    audit.record(
        session,
        action="payroll_policy.finalize",
        actor=(principal.user_id, principal.username),
        target_type="payroll_policy",
        target_id=policy.id,
        detail={"city": policy.city, "effective_from": policy.effective_from.isoformat()},
    )
    session.commit()
    return _out(policy)
