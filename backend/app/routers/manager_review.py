"""DingTalk H5 payroll review for managers without HR-console access."""

from __future__ import annotations

import secrets
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.audit import service as audit
from app.auth.permissions import Perm
from app.core.config import get_settings
from app.db.session import get_session
from app.dingtalk.client import DingTalkClientError, get_dingtalk_client
from app.dingtalk.manager_security import (
    ManagerReviewClaims,
    ManagerReviewTokenError,
    create_manager_review_token,
    decode_manager_review_token,
)
from app.models.auth import Permission, RolePermission, User, UserReviewScope, UserRole
from app.models.dingtalk import DingTalkDelivery, DingTalkDeliveryKind, DingTalkDeliveryStatus
from app.models.employee import Department, Employee
from app.models.payroll_batch import PayrollBatch
from app.models.payroll_result import BatchConfirmation, PayrollResult
from app.payroll.batch_service import BatchError, confirm_scope, raise_dispute

router = APIRouter(prefix="/api/manager-review", tags=["manager-review"])

_REVIEW_ID_PATTERN = r"^[0-9a-f]{32}$"
_REVIEWABLE_DELIVERY_STATUSES = frozenset(
    {DingTalkDeliveryStatus.SANDBOXED, DingTalkDeliveryStatus.SENT}
)
_AUTH_FAILURE_DETAIL = "Unable to authorize this payroll review."


class ManagerReviewConfigOut(BaseModel):
    enabled: bool
    client_id: str | None
    corp_id: str | None


class ManagerSessionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_id: str = Field(pattern=_REVIEW_ID_PATTERN)
    auth_code: str = Field(min_length=1, max_length=512)

    @field_validator("auth_code")
    @classmethod
    def normalize_auth_code(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("auth_code cannot be blank")
        return normalized


class ManagerSessionOut(BaseModel):
    access_token: str
    token_type: str = "bearer"
    expires_in: int


class ManagerSalaryLineOut(BaseModel):
    code: str
    name: str
    amount: str


class ManagerEmployeePayrollOut(BaseModel):
    employee_id: int
    emp_no: str | None
    employee_name: str
    actual_attendance_days: str
    statutory_holiday_days: str
    statutory_holiday_worked_days: str
    gross: str
    deposit: str
    net: str
    carry_forward: str
    lines: list[ManagerSalaryLineOut]


class ManagerReviewOut(BaseModel):
    review_id: str
    period: str
    store_name: str
    department: Department
    confirmation_status: str
    employees: list[ManagerEmployeePayrollOut]


class ManagerDisputeBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    employee_id: int = Field(gt=0)
    salary_item: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    opinion: str = Field(min_length=1, max_length=1000)

    @field_validator("opinion")
    @classmethod
    def normalize_opinion(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("opinion cannot be blank")
        return normalized


class ManagerDisputeOut(BaseModel):
    dispute_id: int
    batch_status: str


class ManagerConfirmOut(BaseModel):
    confirmation_status: str
    batch_status: str


@dataclass(frozen=True)
class _ManagerReviewContext:
    claims: ManagerReviewClaims
    delivery: DingTalkDelivery
    user: User
    batch: PayrollBatch


def _no_store(response: Response) -> None:
    response.headers["Cache-Control"] = "no-store"
    response.headers["Pragma"] = "no-cache"


def _unauthorized() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=_AUTH_FAILURE_DETAIL,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _bearer_token(request: Request) -> str:
    scheme, _, token = request.headers.get("Authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _unauthorized()
    return token


def _eligible_review_recipient_ids(
    session: Session, *, org_unit_id: int, department: Department
) -> set[int]:
    return set(
        session.scalars(
            select(UserReviewScope.user_id)
            .join(User, User.id == UserReviewScope.user_id)
            .join(UserRole, UserRole.user_id == User.id)
            .join(RolePermission, RolePermission.role_id == UserRole.role_id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(
                UserReviewScope.org_unit_id == org_unit_id,
                UserReviewScope.department == department,
                User.is_deleted.is_(False),
                User.status == "ACTIVE",
                Permission.code == Perm.PAYROLL_REVIEW,
            )
            .distinct()
        ).all()
    )


def _delivery_is_currently_authorized(
    session: Session, *, delivery: DingTalkDelivery, user: User
) -> bool:
    return (
        delivery.recipient_user_id == user.id
        and delivery.kind is DingTalkDeliveryKind.PAYROLL_REVIEW
        and delivery.status in _REVIEWABLE_DELIVERY_STATUSES
        and not user.is_deleted
        and user.status == "ACTIVE"
        and _eligible_review_recipient_ids(
            session,
            org_unit_id=delivery.org_unit_id,
            department=delivery.department,
        )
        == {user.id}
    )


def _load_review_context(
    review_id: str, request: Request, session: Session
) -> _ManagerReviewContext:
    try:
        claims = decode_manager_review_token(_bearer_token(request))
    except ManagerReviewTokenError:
        raise _unauthorized() from None
    delivery = session.scalars(
        select(DingTalkDelivery).where(
            DingTalkDelivery.id == claims.delivery_id,
            DingTalkDelivery.review_public_id == review_id,
        )
    ).first()
    if delivery is None or delivery.batch_version != claims.batch_version:
        raise _unauthorized()
    user = session.get(User, claims.user_id)
    if user is None or not _delivery_is_currently_authorized(session, delivery=delivery, user=user):
        raise _unauthorized()
    batch = session.get(PayrollBatch, delivery.batch_id)
    if batch is None:
        raise _unauthorized()
    if batch.version != delivery.batch_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payroll review has been replaced by a newer version.",
        )
    return _ManagerReviewContext(claims=claims, delivery=delivery, user=user, batch=batch)


def _latest_scoped_results(session: Session, delivery: DingTalkDelivery) -> list[PayrollResult]:
    candidate = aliased(PayrollResult)
    latest_version = (
        select(func.max(candidate.version))
        .where(
            candidate.batch_id == PayrollResult.batch_id,
            candidate.batch_version == PayrollResult.batch_version,
            candidate.employee_id == PayrollResult.employee_id,
        )
        .correlate(PayrollResult)
        .scalar_subquery()
    )
    return list(
        session.scalars(
            select(PayrollResult)
            .where(
                PayrollResult.batch_id == delivery.batch_id,
                PayrollResult.batch_version == delivery.batch_version,
                PayrollResult.org_unit_id == delivery.org_unit_id,
                PayrollResult.department == delivery.department,
                PayrollResult.version == latest_version,
            )
            .order_by(PayrollResult.emp_no_snapshot, PayrollResult.employee_id)
        ).all()
    )


def _money_text(value: object) -> str:
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The payroll snapshot contains an invalid amount.",
        ) from None


def _safe_lines(raw_lines: object) -> list[ManagerSalaryLineOut]:
    if not isinstance(raw_lines, list):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="The payroll snapshot contains invalid line items.",
        )
    lines: list[ManagerSalaryLineOut] = []
    for raw in raw_lines:
        if not isinstance(raw, Mapping):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The payroll snapshot contains invalid line items.",
            )
        code = raw.get("code")
        if not isinstance(code, str) or not code or len(code) > 64:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="The payroll snapshot contains invalid line items.",
            )
        raw_name = raw.get("name")
        name = raw_name if isinstance(raw_name, str) and 0 < len(raw_name) <= 128 else code
        lines.append(
            ManagerSalaryLineOut(code=code, name=name, amount=_money_text(raw.get("amount")))
        )
    return lines


def _result_for_employee(
    session: Session, delivery: DingTalkDelivery, employee_id: int
) -> PayrollResult | None:
    return next(
        (
            row
            for row in _latest_scoped_results(session, delivery)
            if row.employee_id == employee_id
        ),
        None,
    )


@router.get("/config", response_model=ManagerReviewConfigOut)
def manager_review_config(response: Response) -> ManagerReviewConfigOut:
    settings = get_settings()
    _no_store(response)
    enabled = bool(
        settings.dingtalk_corp_id
        and settings.dingtalk_client_id
        and settings.dingtalk_client_secret
    )
    return ManagerReviewConfigOut(
        enabled=enabled,
        client_id=settings.dingtalk_client_id if enabled else None,
        corp_id=settings.dingtalk_corp_id if enabled else None,
    )


@router.post("/session", response_model=ManagerSessionOut)
def create_manager_session(
    body: ManagerSessionBody,
    response: Response,
    session: Session = Depends(get_session),
) -> ManagerSessionOut:
    _no_store(response)
    delivery = session.scalars(
        select(DingTalkDelivery).where(DingTalkDelivery.review_public_id == body.review_id)
    ).first()
    if delivery is None or delivery.recipient_user_id is None:
        raise _unauthorized()
    try:
        identity = get_dingtalk_client().resolve_login_code(body.auth_code)
    except DingTalkClientError:
        raise _unauthorized() from None
    user = session.get(User, delivery.recipient_user_id)
    if (
        user is None
        or user.dingtalk_user_id is None
        or not secrets.compare_digest(user.dingtalk_user_id, identity.user_id)
        or not _delivery_is_currently_authorized(session, delivery=delivery, user=user)
    ):
        raise _unauthorized()
    batch = session.get(PayrollBatch, delivery.batch_id)
    if batch is None:
        raise _unauthorized()
    if batch.version != delivery.batch_version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This payroll review has been replaced by a newer version.",
        )
    token = create_manager_review_token(
        user_id=user.id,
        delivery_id=delivery.id,
        batch_version=delivery.batch_version,
    )
    audit.record(
        session,
        action="manager_review.session.create",
        actor=(user.id, user.username),
        target_type="dingtalk_delivery",
        target_id=delivery.id,
        detail={"batch_id": delivery.batch_id, "batch_version": delivery.batch_version},
    )
    session.commit()
    return ManagerSessionOut(
        access_token=token,
        expires_in=get_settings().dingtalk_review_session_ttl_minutes * 60,
    )


@router.get("/reviews/{review_id}", response_model=ManagerReviewOut)
def get_manager_review(
    review_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> ManagerReviewOut:
    if len(review_id) != 32 or any(char not in "0123456789abcdef" for char in review_id):
        raise _unauthorized()
    _no_store(response)
    context = _load_review_context(review_id, request, session)
    confirmation = session.scalars(
        select(BatchConfirmation).where(
            BatchConfirmation.batch_id == context.delivery.batch_id,
            BatchConfirmation.batch_version == context.delivery.batch_version,
            BatchConfirmation.org_unit_id == context.delivery.org_unit_id,
            BatchConfirmation.department == context.delivery.department,
        )
    ).first()
    if confirmation is None:
        raise HTTPException(status_code=409, detail="This review scope is no longer available.")
    employees = [
        ManagerEmployeePayrollOut(
            employee_id=result.employee_id,
            emp_no=result.emp_no_snapshot,
            employee_name=result.employee_name_snapshot or f"Employee {result.employee_id}",
            actual_attendance_days=_money_text(result.actual_attendance_days),
            statutory_holiday_days=_money_text(result.statutory_holiday_days),
            statutory_holiday_worked_days=_money_text(result.statutory_holiday_worked_days),
            gross=_money_text(result.gross),
            deposit=_money_text(result.deposit),
            net=_money_text(result.net),
            carry_forward=_money_text(result.carry_forward),
            lines=_safe_lines(result.lines),
        )
        for result in _latest_scoped_results(session, context.delivery)
    ]
    audit.record(
        session,
        action="manager_review.payroll.view",
        actor=(context.user.id, context.user.username),
        target_type="dingtalk_delivery",
        target_id=context.delivery.id,
        detail={"employee_count": len(employees)},
    )
    session.commit()
    return ManagerReviewOut(
        review_id=context.delivery.review_public_id,
        period=context.delivery.period_snapshot,
        store_name=context.delivery.org_unit_name_snapshot,
        department=context.delivery.department,
        confirmation_status=confirmation.status.value,
        employees=employees,
    )


@router.post(
    "/reviews/{review_id}/disputes",
    response_model=ManagerDisputeOut,
    status_code=status.HTTP_201_CREATED,
)
def create_manager_dispute(
    review_id: str,
    body: ManagerDisputeBody,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> ManagerDisputeOut:
    _no_store(response)
    context = _load_review_context(review_id, request, session)
    result = _result_for_employee(session, context.delivery, body.employee_id)
    employee = session.get(Employee, body.employee_id) if result is not None else None
    if employee is None:
        raise HTTPException(status_code=404, detail="Payroll review item not found.")
    try:
        dispute = raise_dispute(
            session,
            context.batch,
            employee,
            body.salary_item,
            body.opinion,
            context.user.id,
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="manager_review.dispute.create",
        actor=(context.user.id, context.user.username),
        target_type="comp_dispute",
        target_id=dispute.id,
        detail={"delivery_id": context.delivery.id, "salary_item": body.salary_item},
    )
    session.commit()
    return ManagerDisputeOut(dispute_id=dispute.id, batch_status=context.batch.status.value)


@router.post("/reviews/{review_id}/confirm", response_model=ManagerConfirmOut)
def confirm_manager_review(
    review_id: str,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> ManagerConfirmOut:
    _no_store(response)
    context = _load_review_context(review_id, request, session)
    try:
        confirmation = confirm_scope(
            session,
            context.batch,
            context.delivery.org_unit_id,
            context.delivery.department,
            context.user.id,
        )
    except BatchError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="manager_review.confirm",
        actor=(context.user.id, context.user.username),
        target_type="dingtalk_delivery",
        target_id=context.delivery.id,
        detail={
            "org_unit_id": context.delivery.org_unit_id,
            "department": context.delivery.department.value,
        },
    )
    session.commit()
    return ManagerConfirmOut(
        confirmation_status=confirmation.status.value,
        batch_status=context.batch.status.value,
    )
