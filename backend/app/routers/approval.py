"""Salary-adjustment documents and reusable approval-flow API endpoints."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.approval import service as approvals
from app.audit import service as audit
from app.auth.deps import get_current_principal, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.comp.service import (
    StructureError,
    lock_employee_salary_structure,
    set_component_amount,
)
from app.core.config import DingTalkMode, Settings, get_settings
from app.core.decimal import decimal_text
from app.core.urls import require_http_url
from app.db.session import get_session
from app.dingtalk import service as dingtalk
from app.models.approval import (
    ApprovalAction,
    ApprovalActionType,
    ApprovalBusinessType,
    ApprovalFlow,
    ApprovalInstance,
    ApprovalInstanceStatus,
    ApprovalStep,
    SalaryAdjustment,
    SalaryAdjustmentStatus,
)
from app.models.auth import Permission, Role, RolePermission
from app.models.comp import EmployeeSalaryStructure, SalaryComponentDef
from app.models.dingtalk import CompAppeal
from app.models.employee import Employee
from app.models.org import OrgUnit
from app.payroll.guards import PayrollSourceLockedError, assert_structure_effective_date_mutable
from app.repositories.employee import EmployeeRepository

router = APIRouter(tags=["approval"])


def _require_global_flow_manager(
    principal: Principal = Depends(require_permission(Perm.APPROVAL_FLOW_MANAGE)),
    session: Session = Depends(get_session),
) -> Principal:
    if resolve_permission_org_scope(session, principal, Perm.APPROVAL_FLOW_MANAGE) is not None:
        raise HTTPException(status_code=403, detail="Approval-flow management requires group scope")
    return principal


class FlowStepBody(BaseModel):
    step_order: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=128)
    role_code: str = Field(min_length=1, max_length=32)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized

    @field_validator("role_code")
    @classmethod
    def normalize_role_code(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("must not be blank")
        return normalized


class FlowCreateBody(BaseModel):
    code: str = Field(min_length=1, max_length=64)
    name: str = Field(min_length=1, max_length=128)
    business_type: ApprovalBusinessType
    org_unit_id: int | None = Field(default=None, gt=0)
    min_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    max_amount: Decimal | None = Field(default=None, ge=0, max_digits=14, decimal_places=2)
    is_active: bool = True
    steps: list[FlowStepBody] = Field(min_length=1, max_length=20)

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
    def validate_order_and_range(self) -> FlowCreateBody:
        if (
            self.max_amount is not None
            and self.min_amount is not None
            and self.max_amount < self.min_amount
        ):
            raise ValueError("max_amount must be at least min_amount")
        orders = [step.step_order for step in self.steps]
        if orders != list(range(1, len(orders) + 1)):
            raise ValueError("steps must use consecutive step_order values beginning at 1")
        return self


class FlowStepOut(BaseModel):
    step_order: int
    name: str
    role_code: str

    model_config = {"from_attributes": True}


class FlowOut(BaseModel):
    id: int
    code: str
    name: str
    business_type: ApprovalBusinessType
    org_unit_id: int | None
    min_amount: Decimal | None
    max_amount: Decimal | None
    is_active: bool
    steps: list[FlowStepOut]


class SalaryAdjustmentCreate(BaseModel):
    employee_id: int = Field(gt=0)
    component_id: int = Field(gt=0)
    amount: Decimal = Field(ge=0, max_digits=14, decimal_places=2)
    effective_from: date
    reason: str = Field(min_length=1, max_length=2000)
    attachment_url: str = Field(min_length=1, max_length=512)

    @field_validator("reason")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be blank")
        return value

    @field_validator("attachment_url")
    @classmethod
    def validate_attachment_url(cls, value: str) -> str:
        return require_http_url(value)

    @field_validator("amount")
    @classmethod
    def normalize_signed_zero(cls, value: Decimal) -> Decimal:
        # Decimal treats -0.00 as equal to zero, while audit serialization
        # retains its sign.  Canonicalize it before snapshot comparison so a
        # no-op request cannot become a permanently unfinalizable approval.
        return Decimal("0.00") if value == 0 else value


class SalaryAdjustmentOut(BaseModel):
    id: int
    employee_id: int
    org_unit_id: int
    component_id: int
    amount: Decimal
    effective_from: date
    reason: str
    attachment_url: str
    requester_id: int
    status: SalaryAdjustmentStatus
    before_snapshot: dict
    approval_instance_id: int | None
    applied_structure_id: int | None

    model_config = {"from_attributes": True}


class ApprovalDecisionBody(BaseModel):
    decision: ApprovalActionType
    comment: str | None = Field(default=None, max_length=2000)

    @field_validator("comment", mode="before")
    @classmethod
    def strip_optional_comment(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


class ApprovalActionOut(BaseModel):
    step_order: int
    action: ApprovalActionType
    actor_id: int
    comment: str | None

    model_config = {"from_attributes": True}


class ApprovalInstanceOut(BaseModel):
    id: int
    flow_id: int
    business_type: ApprovalBusinessType
    business_id: int
    requester_id: int
    org_unit_id: int
    amount: Decimal
    status: ApprovalInstanceStatus
    current_step_order: int | None
    flow_snapshot: dict
    actions: list[ApprovalActionOut]


class ApprovalTodoOut(BaseModel):
    id: int
    business_type: ApprovalBusinessType
    business_id: int
    org_unit_id: int
    amount: Decimal
    requester_id: int
    current_step_order: int
    current_step_name: str


def _flow_out(
    session: Session, flow: ApprovalFlow, *, steps: list[ApprovalStep] | None = None
) -> FlowOut:
    if steps is None:
        steps = list(
            session.scalars(
                select(ApprovalStep)
                .where(ApprovalStep.flow_id == flow.id)
                .order_by(ApprovalStep.step_order)
            ).all()
        )
    return FlowOut(
        id=flow.id,
        code=flow.code,
        name=flow.name,
        business_type=flow.business_type,
        org_unit_id=flow.org_unit_id,
        min_amount=flow.min_amount,
        max_amount=flow.max_amount,
        is_active=flow.is_active,
        steps=[FlowStepOut.model_validate(step) for step in steps],
    )


def _structure_snapshot(
    session: Session, *, employee_id: int, component_id: int, effective_from: date
) -> dict[str, object]:
    record = session.scalars(
        select(EmployeeSalaryStructure)
        .where(
            EmployeeSalaryStructure.employee_id == employee_id,
            EmployeeSalaryStructure.component_id == component_id,
            EmployeeSalaryStructure.effective_from <= effective_from,
            (EmployeeSalaryStructure.effective_to.is_(None))
            | (EmployeeSalaryStructure.effective_to > effective_from),
        )
        .order_by(
            EmployeeSalaryStructure.effective_from.desc(), EmployeeSalaryStructure.revision.desc()
        )
        .limit(1)
    ).first()
    if record is None:
        return {"record_exists": False}
    return {
        "record_exists": True,
        "structure_id": record.id,
        "component_id": record.component_id,
        "amount": decimal_text(record.amount),
        "effective_from": str(record.effective_from),
        "effective_to": str(record.effective_to) if record.effective_to else None,
        "revision": record.revision,
    }


def _visible_employee_or_404(
    session: Session, *, principal: Principal, employee_id: int, permission: str
) -> Employee:
    employee = EmployeeRepository(
        session,
        org_scope=resolve_permission_org_scope(session, principal, permission),
    ).get(employee_id)
    if employee is None:
        raise HTTPException(
            status_code=404, detail="Employee not found or outside organization scope"
        )
    return employee


def _visible_adjustment_or_404(
    session: Session, *, principal: Principal, adjustment_id: int
) -> SalaryAdjustment:
    adjustment = session.get(SalaryAdjustment, adjustment_id)
    if adjustment is None:
        raise HTTPException(status_code=404, detail="Salary adjustment not found")
    if adjustment.requester_id == principal.user_id:
        return adjustment
    if not principal.has_permission(Perm.ADJUSTMENT_READ):
        raise HTTPException(status_code=403, detail="Not allowed to view this salary adjustment")
    scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_READ)
    if scope is not None and adjustment.org_unit_id not in scope:
        raise HTTPException(status_code=404, detail="Salary adjustment not found")
    return adjustment


def _require_adjustment_reader_or_creator(principal: Principal) -> None:
    if not (
        principal.has_permission(Perm.ADJUSTMENT_READ)
        or principal.has_permission(Perm.ADJUSTMENT_CREATE)
        or principal.has_permission(Perm.ADJUSTMENT_APPROVE)
    ):
        raise HTTPException(status_code=403, detail="Adjustment read permission is required")


def _require_approval_instance_reader(principal: Principal) -> None:
    if not (
        principal.has_permission(Perm.ADJUSTMENT_READ)
        or principal.has_permission(Perm.ADJUSTMENT_CREATE)
        or principal.has_permission(Perm.ADJUSTMENT_APPROVE)
        or principal.has_permission(Perm.PAYROLL_REVIEW)
    ):
        raise HTTPException(status_code=403, detail="Approval-instance read permission is required")


def _ensure_step_roles_can_approve(session: Session, steps: list[FlowStepBody]) -> None:
    role_codes = {step.role_code for step in steps}
    roles_with_permission = set(
        session.scalars(
            select(Role.code)
            .join(RolePermission, RolePermission.role_id == Role.id)
            .join(Permission, Permission.id == RolePermission.permission_id)
            .where(Role.code.in_(role_codes), Permission.code == Perm.ADJUSTMENT_APPROVE)
        ).all()
    )
    missing = sorted(role_codes - roles_with_permission)
    if missing:
        raise HTTPException(
            status_code=422,
            detail=f"Approval-step roles lack {Perm.ADJUSTMENT_APPROVE}: {', '.join(missing)}",
        )


@router.get("/api/approval-flows", response_model=list[FlowOut])
def list_flows(
    principal: Principal = Depends(require_permission(Perm.ADJUSTMENT_READ)),
    session: Session = Depends(get_session),
) -> list[FlowOut]:
    statement = select(ApprovalFlow).where(ApprovalFlow.is_deleted.is_(False))
    scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_READ)
    if scope is not None:
        if not scope:
            return []
        statement = statement.where(
            or_(ApprovalFlow.org_unit_id.is_(None), ApprovalFlow.org_unit_id.in_(scope))
        )
    flows = list(
        session.scalars(statement.order_by(ApprovalFlow.business_type, ApprovalFlow.code)).all()
    )
    steps_by_flow: dict[int, list[ApprovalStep]] = {flow.id: [] for flow in flows}
    if steps_by_flow:
        steps = session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.flow_id.in_(steps_by_flow))
            .order_by(ApprovalStep.flow_id, ApprovalStep.step_order)
        )
        for step in steps:
            steps_by_flow[step.flow_id].append(step)
    return [_flow_out(session, flow, steps=steps_by_flow[flow.id]) for flow in flows]


@router.post("/api/approval-flows", response_model=FlowOut, status_code=status.HTTP_201_CREATED)
def create_flow(
    body: FlowCreateBody,
    principal: Principal = Depends(_require_global_flow_manager),
    session: Session = Depends(get_session),
) -> FlowOut:
    _ensure_step_roles_can_approve(session, body.steps)
    if body.org_unit_id is not None:
        org = session.get(OrgUnit, body.org_unit_id)
        if org is None or org.is_deleted:
            raise HTTPException(status_code=422, detail="Approval flow organization is not active")
    try:
        approvals.assert_flow_range_available(
            session,
            business_type=body.business_type,
            org_unit_id=body.org_unit_id,
            min_amount=body.min_amount,
            max_amount=body.max_amount,
            is_active=body.is_active,
        )
    except approvals.ApprovalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    flow = ApprovalFlow(
        code=body.code.strip().upper(),
        name=body.name.strip(),
        business_type=body.business_type,
        org_unit_id=body.org_unit_id,
        min_amount=body.min_amount,
        max_amount=body.max_amount,
        is_active=body.is_active,
    )
    session.add(flow)
    try:
        session.flush()
    except IntegrityError:
        session.rollback()
        raise HTTPException(status_code=409, detail="Approval flow code already exists") from None
    steps = [
        ApprovalStep(
            flow_id=flow.id,
            step_order=step.step_order,
            name=step.name.strip(),
            role_code=step.role_code.strip().upper(),
        )
        for step in body.steps
    ]
    session.add_all(steps)
    session.flush()
    audit.record(
        session,
        action="approval_flow.create",
        actor=(principal.user_id, principal.username),
        target_type="approval_flow",
        target_id=flow.id,
        detail={"code": flow.code, "business_type": flow.business_type, "steps": len(steps)},
    )
    session.commit()
    return _flow_out(session, flow)


@router.post(
    "/api/salary-adjustments",
    response_model=SalaryAdjustmentOut,
    status_code=status.HTTP_201_CREATED,
)
def create_salary_adjustment(
    body: SalaryAdjustmentCreate,
    principal: Principal = Depends(require_permission(Perm.ADJUSTMENT_CREATE)),
    session: Session = Depends(get_session),
) -> SalaryAdjustment:
    employee = _visible_employee_or_404(
        session,
        principal=principal,
        employee_id=body.employee_id,
        permission=Perm.ADJUSTMENT_CREATE,
    )
    component = session.get(SalaryComponentDef, body.component_id)
    if component is None or component.is_deleted:
        raise HTTPException(status_code=404, detail="Salary component not found")
    before_snapshot = _structure_snapshot(
        session,
        employee_id=employee.id,
        component_id=component.id,
        effective_from=body.effective_from,
    )
    if before_snapshot.get("amount") == decimal_text(body.amount):
        raise HTTPException(
            status_code=422,
            detail="Salary adjustment must change the current component amount",
        )
    adjustment = SalaryAdjustment(
        employee_id=employee.id,
        org_unit_id=employee.org_unit_id,
        component_id=component.id,
        amount=body.amount,
        effective_from=body.effective_from,
        reason=body.reason,
        attachment_url=body.attachment_url,
        requester_id=principal.user_id,
        status=SalaryAdjustmentStatus.DRAFT,
        before_snapshot=before_snapshot,
    )
    session.add(adjustment)
    session.flush()
    audit.record(
        session,
        action="salary_adjustment.create",
        actor=(principal.user_id, principal.username),
        target_type="salary_adjustment",
        target_id=adjustment.id,
        detail={
            "employee_id": employee.id,
            "component_id": component.id,
            "amount": decimal_text(body.amount),
            "effective_from": str(body.effective_from),
            "attachment_url": body.attachment_url,
        },
    )
    session.commit()
    return adjustment


@router.get("/api/salary-adjustments", response_model=list[SalaryAdjustmentOut])
def list_salary_adjustments(
    status_filter: SalaryAdjustmentStatus | None = Query(default=None, alias="status"),
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> list[SalaryAdjustment]:
    statement = select(SalaryAdjustment)
    if principal.has_permission(Perm.ADJUSTMENT_READ):
        scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_READ)
        if scope is not None:
            if not scope:
                return []
            statement = statement.where(SalaryAdjustment.org_unit_id.in_(scope))
    elif principal.has_permission(Perm.ADJUSTMENT_CREATE):
        statement = statement.where(SalaryAdjustment.requester_id == principal.user_id)
    else:
        raise HTTPException(status_code=403, detail="Adjustment read permission is required")
    if status_filter is not None:
        statement = statement.where(SalaryAdjustment.status == status_filter)
    return list(session.scalars(statement.order_by(SalaryAdjustment.id.desc()).limit(200)).all())


@router.get("/api/salary-adjustments/{adjustment_id}", response_model=SalaryAdjustmentOut)
def get_salary_adjustment(
    adjustment_id: int,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> SalaryAdjustment:
    _require_adjustment_reader_or_creator(principal)
    return _visible_adjustment_or_404(session, principal=principal, adjustment_id=adjustment_id)


@router.post("/api/salary-adjustments/{adjustment_id}/submit", response_model=SalaryAdjustmentOut)
def submit_salary_adjustment(
    adjustment_id: int,
    principal: Principal = Depends(require_permission(Perm.ADJUSTMENT_CREATE)),
    session: Session = Depends(get_session),
) -> SalaryAdjustment:
    adjustment = session.scalars(
        select(SalaryAdjustment).where(SalaryAdjustment.id == adjustment_id).with_for_update()
    ).first()
    if adjustment is None:
        raise HTTPException(status_code=404, detail="Salary adjustment not found")
    if adjustment.requester_id != principal.user_id:
        raise HTTPException(
            status_code=403, detail="Only the requester may submit this salary adjustment"
        )
    scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_CREATE)
    if scope is not None and adjustment.org_unit_id not in scope:
        raise HTTPException(status_code=404, detail="Salary adjustment not found")
    if adjustment.status is not SalaryAdjustmentStatus.DRAFT:
        raise HTTPException(
            status_code=409, detail="Only draft salary adjustments may be submitted"
        )
    try:
        flow, steps = approvals.select_flow(
            session,
            business_type=ApprovalBusinessType.SALARY_ADJUSTMENT,
            org_unit_id=adjustment.org_unit_id,
            amount=adjustment.amount,
        )
        instance = approvals.start_instance(
            session,
            flow=flow,
            steps=steps,
            business_type=ApprovalBusinessType.SALARY_ADJUSTMENT,
            business_id=adjustment.id,
            requester_id=adjustment.requester_id,
            org_unit_id=adjustment.org_unit_id,
            amount=adjustment.amount,
        )
    except approvals.ApprovalError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    adjustment.approval_instance_id = instance.id
    adjustment.status = SalaryAdjustmentStatus.PENDING
    audit.record(
        session,
        action="salary_adjustment.submit",
        actor=(principal.user_id, principal.username),
        target_type="salary_adjustment",
        target_id=adjustment.id,
        detail={"approval_instance_id": instance.id, "flow_id": flow.id},
    )
    session.commit()
    return adjustment


def _approval_instance_out(session: Session, instance: ApprovalInstance) -> ApprovalInstanceOut:
    actions = list(
        session.scalars(
            select(ApprovalAction)
            .where(ApprovalAction.instance_id == instance.id)
            .order_by(ApprovalAction.step_order, ApprovalAction.id)
        ).all()
    )
    return ApprovalInstanceOut(
        id=instance.id,
        flow_id=instance.flow_id,
        business_type=instance.business_type,
        business_id=instance.business_id,
        requester_id=instance.requester_id,
        org_unit_id=instance.org_unit_id,
        amount=instance.amount,
        status=instance.status,
        current_step_order=instance.current_step_order,
        flow_snapshot=instance.flow_snapshot,
        actions=[ApprovalActionOut.model_validate(action) for action in actions],
    )


def _visible_instance_or_404(
    session: Session, *, principal: Principal, instance_id: int
) -> ApprovalInstance:
    instance = session.get(ApprovalInstance, instance_id)
    if instance is None:
        raise HTTPException(status_code=404, detail="Approval instance not found")
    if instance.requester_id == principal.user_id:
        return instance
    if principal.has_permission(Perm.ADJUSTMENT_READ):
        scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_READ)
        if scope is None or instance.org_unit_id in scope:
            return instance
    if principal.has_permission(Perm.ADJUSTMENT_APPROVE) and approvals.can_act(
        session, instance=instance, principal=principal
    ):
        return instance
    raise HTTPException(status_code=404, detail="Approval instance not found")


@router.get("/api/approval-instances/todos", response_model=list[ApprovalTodoOut])
def list_approval_todos(
    principal: Principal = Depends(require_permission(Perm.ADJUSTMENT_APPROVE)),
    session: Session = Depends(get_session),
) -> list[ApprovalTodoOut]:
    statement = select(ApprovalInstance).where(
        ApprovalInstance.status == ApprovalInstanceStatus.PENDING
    )
    scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_APPROVE)
    if scope is not None:
        if not scope:
            return []
        statement = statement.where(ApprovalInstance.org_unit_id.in_(scope))
    instances = list(session.scalars(statement.order_by(ApprovalInstance.id).limit(200)).all())
    role_codes = approvals.user_role_codes(session, principal.user_id)
    todos: list[ApprovalTodoOut] = []
    for instance in instances:
        if not approvals.can_act_with_context(
            instance=instance,
            principal=principal,
            permission_scope=scope,
            role_codes=role_codes,
        ):
            continue
        if instance.current_step_order is None:
            continue
        step = next(
            item
            for item in instance.flow_snapshot["steps"]
            if item["step_order"] == instance.current_step_order
        )
        todos.append(
            ApprovalTodoOut(
                id=instance.id,
                business_type=instance.business_type,
                business_id=instance.business_id,
                org_unit_id=instance.org_unit_id,
                amount=instance.amount,
                requester_id=instance.requester_id,
                current_step_order=instance.current_step_order,
                current_step_name=step["name"],
            )
        )
    return todos


@router.get("/api/approval-instances/{instance_id}", response_model=ApprovalInstanceOut)
def get_approval_instance(
    instance_id: int,
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> ApprovalInstanceOut:
    _require_approval_instance_reader(principal)
    return _approval_instance_out(
        session, _visible_instance_or_404(session, principal=principal, instance_id=instance_id)
    )


@router.post("/api/approval-instances/{instance_id}/decisions", response_model=ApprovalInstanceOut)
def decide_approval_instance(
    instance_id: int,
    body: ApprovalDecisionBody,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(require_permission(Perm.ADJUSTMENT_APPROVE)),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> ApprovalInstanceOut:
    instance = session.scalars(
        select(ApprovalInstance).where(ApprovalInstance.id == instance_id).with_for_update()
    ).first()
    if instance is None:
        raise HTTPException(status_code=404, detail="Approval instance not found")
    # Authorize before reading the business document or probing source locks.
    # This avoids revealing whether a protected employee moved, changed salary
    # structure, or has a locked payroll period to an ineligible approver.
    try:
        approvals.assert_can_act(session, instance=instance, principal=principal)
    except approvals.ApprovalForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except approvals.ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    adjustment: SalaryAdjustment | None = None
    appeal: CompAppeal | None = None
    appeal_status_delivery_id: int | None = None
    if instance.business_type is ApprovalBusinessType.SALARY_ADJUSTMENT:
        adjustment = session.scalars(
            select(SalaryAdjustment)
            .where(SalaryAdjustment.id == instance.business_id)
            .with_for_update()
        ).first()
        if adjustment is None or adjustment.approval_instance_id != instance.id:
            raise HTTPException(
                status_code=409, detail="Approval instance business document is inconsistent"
            )
        if adjustment.status is not SalaryAdjustmentStatus.PENDING:
            raise HTTPException(status_code=409, detail="Salary adjustment is not pending")

        if body.decision is ApprovalActionType.APPROVE and approvals.current_step_is_final(
            instance
        ):
            # Acquire the advisory lock before the employee lock and validate the
            # freshly locked row, so a concurrent transfer/deletion cannot be
            # applied from stale ORM state.
            try:
                employee = lock_employee_salary_structure(
                    session, employee_id=adjustment.employee_id
                )
            except StructureError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            if (
                employee is None
                or employee.is_deleted
                or employee.org_unit_id != adjustment.org_unit_id
            ):
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Employee organization changed since submission; "
                        "cancel and resubmit the adjustment"
                    ),
                )
            # Snapshot comparison and the later structure append share the same
            # locked employee parent row.  Otherwise two final approvals can both
            # accept an old snapshot while one waits to write behind the other.
            current_snapshot = _structure_snapshot(
                session,
                employee_id=adjustment.employee_id,
                component_id=adjustment.component_id,
                effective_from=adjustment.effective_from,
            )
            if current_snapshot != adjustment.before_snapshot:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "Salary structure changed since submission; "
                        "create a new adjustment from current data"
                    ),
                )
            try:
                correction_round = assert_structure_effective_date_mutable(
                    session,
                    employee_id=adjustment.employee_id,
                    effective_from=adjustment.effective_from,
                )
            except PayrollSourceLockedError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            if correction_round:
                raise HTTPException(
                    status_code=409,
                    detail="A reopened payroll source requires the dedicated correction workflow",
                )
    elif instance.business_type is ApprovalBusinessType.COMP_APPEAL:
        try:
            appeal = dingtalk.lock_appeal_for_instance(session, instance=instance)
        except dingtalk.DingTalkError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
    else:
        raise HTTPException(status_code=409, detail="Unsupported approval business type")

    try:
        result = approvals.decide(
            session,
            instance_id=instance.id,
            principal=principal,
            action=body.decision,
            comment=body.comment,
        )
    except approvals.ApprovalForbidden as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None
    except approvals.ApprovalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    if adjustment is not None:
        if body.decision is ApprovalActionType.REJECT:
            adjustment.status = SalaryAdjustmentStatus.REJECTED
        elif result.is_final_approval:
            try:
                structure = set_component_amount(
                    session,
                    employee_id=adjustment.employee_id,
                    component_id=adjustment.component_id,
                    amount=adjustment.amount,
                    effective_from=adjustment.effective_from,
                    source_adjustment_id=adjustment.id,
                    source_reason=adjustment.reason,
                    source_attachment_url=adjustment.attachment_url,
                )
            except StructureError as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from None
            adjustment.status = SalaryAdjustmentStatus.APPROVED
            adjustment.applied_structure_id = structure.id
    elif appeal is not None:
        try:
            status_delivery = dingtalk.apply_appeal_approval_outcome(
                session,
                appeal=appeal,
                action=body.decision,
                is_final_approval=result.is_final_approval,
                comment=body.comment,
                approved_by=principal.user_id,
                settings=settings,
            )
            if status_delivery is not None:
                appeal_status_delivery_id = status_delivery.id
        except dingtalk.DingTalkError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    audit.record(
        session,
        action="approval_instance.decide",
        actor=(principal.user_id, principal.username),
        target_type="approval_instance",
        target_id=instance.id,
        detail={
            "business_type": instance.business_type,
            "business_id": instance.business_id,
            "action": body.decision,
            "step_order": result.action.step_order,
            "is_final": result.is_final_approval,
        },
    )
    session.commit()
    if settings.dingtalk_mode is DingTalkMode.LIVE and appeal_status_delivery_id is not None:
        background_tasks.add_task(
            dingtalk.dispatch_live_deliveries,
            (appeal_status_delivery_id,),
        )
    return _approval_instance_out(session, result.instance)
