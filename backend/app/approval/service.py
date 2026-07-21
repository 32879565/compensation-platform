"""Reusable, transaction-safe approval state-machine helpers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.models.approval import (
    ApprovalAction,
    ApprovalActionType,
    ApprovalBusinessType,
    ApprovalFlow,
    ApprovalInstance,
    ApprovalInstanceStatus,
    ApprovalStep,
)
from app.models.auth import Role, UserRole
from app.models.org import OrgUnit


class ApprovalError(Exception):
    """A domain-safe approval failure suitable for conversion to HTTP 409/422."""


class ApprovalForbidden(ApprovalError):
    """The actor is not eligible for the current approval step."""


_MAX_ROUTABLE_AMOUNT = Decimal("999999999999.99")


def _range_bounds(
    min_amount: Decimal | None, max_amount: Decimal | None
) -> tuple[Decimal, Decimal]:
    return (
        min_amount if min_amount is not None else Decimal("0"),
        max_amount if max_amount is not None else _MAX_ROUTABLE_AMOUNT,
    )


def _ranges_overlap(
    first_min: Decimal | None,
    first_max: Decimal | None,
    second_min: Decimal | None,
    second_max: Decimal | None,
) -> bool:
    first_lower, first_upper = _range_bounds(first_min, first_max)
    second_lower, second_upper = _range_bounds(second_min, second_max)
    # Both routing endpoints are inclusive, so touching ranges overlap.
    return max(first_lower, second_lower) <= min(first_upper, second_upper)


def _lock_flow_route(
    session: Session, *, business_type: ApprovalBusinessType, org_unit_id: int | None
) -> None:
    """Serialize API flow changes for one routing namespace on PostgreSQL."""

    get_bind = getattr(session, "get_bind", None)
    if not callable(get_bind):
        return
    bind = get_bind()
    if getattr(getattr(bind, "dialect", None), "name", None) != "postgresql":
        return
    key = f"approval-flow-route-v1:{business_type}:{org_unit_id or 'GLOBAL'}"
    session.scalar(select(func.pg_advisory_xact_lock(func.hashtext(key))))


def assert_flow_range_available(
    session: Session,
    *,
    business_type: ApprovalBusinessType,
    org_unit_id: int | None,
    min_amount: Decimal | None,
    max_amount: Decimal | None,
    is_active: bool,
) -> None:
    """Reject active flow ranges that would make routing ambiguous.

    Different organization roots are intentionally allowed to overlap because
    the closest ancestor wins.  At one exact root, however, inclusive amount
    ranges must be disjoint so every request has one deterministic route.
    """

    if not is_active:
        return
    _lock_flow_route(session, business_type=business_type, org_unit_id=org_unit_id)
    statement = select(ApprovalFlow).where(
        ApprovalFlow.business_type == business_type,
        ApprovalFlow.is_active.is_(True),
        ApprovalFlow.is_deleted.is_(False),
    )
    if org_unit_id is None:
        statement = statement.where(ApprovalFlow.org_unit_id.is_(None))
    else:
        statement = statement.where(ApprovalFlow.org_unit_id == org_unit_id)
    for existing in session.scalars(statement.with_for_update()):
        if _ranges_overlap(
            existing.min_amount,
            existing.max_amount,
            min_amount,
            max_amount,
        ):
            raise ApprovalError(
                "Approval-flow amount range overlaps an active flow at the same organization"
            )


@dataclass(frozen=True)
class DecisionResult:
    instance: ApprovalInstance
    action: ApprovalAction
    is_final_approval: bool


def _ancestor_ids(session: Session, org_unit_id: int) -> tuple[int, ...]:
    """Return the target organization followed by its live ancestors.

    The bounded walk rejects a broken/cyclic organization chain rather than
    choosing a potentially too-broad flow.
    """

    parents: dict[int, int | None] = {
        int(unit_id): (int(parent_id) if parent_id is not None else None)
        for unit_id, parent_id in session.execute(
            select(OrgUnit.id, OrgUnit.parent_id).where(OrgUnit.is_deleted.is_(False))
        ).all()
    }
    if org_unit_id not in parents:
        raise ApprovalError("The business document organization is not active")
    result: list[int] = []
    seen: set[int] = set()
    current: int | None = org_unit_id
    while current is not None:
        if current in seen or current not in parents:
            raise ApprovalError("The organization hierarchy is invalid")
        seen.add(current)
        result.append(current)
        current = parents[current]
    return tuple(result)


def select_flow(
    session: Session,
    *,
    business_type: ApprovalBusinessType,
    org_unit_id: int,
    amount: Decimal,
) -> tuple[ApprovalFlow, list[ApprovalStep]]:
    """Select one unambiguous active flow by org specificity and amount range."""

    ancestors = _ancestor_ids(session, org_unit_id)
    candidates = list(
        session.scalars(
            select(ApprovalFlow)
            .where(
                ApprovalFlow.business_type == business_type,
                ApprovalFlow.is_active.is_(True),
                ApprovalFlow.is_deleted.is_(False),
            )
            .with_for_update()
        ).all()
    )
    eligible = [
        flow
        for flow in candidates
        if (flow.org_unit_id is None or flow.org_unit_id in ancestors)
        and (flow.min_amount is None or amount >= flow.min_amount)
        and (flow.max_amount is None or amount <= flow.max_amount)
    ]
    if not eligible:
        raise ApprovalError("No active approval flow matches this organization and amount")

    # The closest organization wins.  At one organization root, the active
    # amount ranges are required to be disjoint.  Keep this fail-closed check
    # too, because administrators could have inserted legacy data outside the
    # API before that validation existed.
    def organization_specificity(flow: ApprovalFlow) -> int:
        organization_specificity = (
            0 if flow.org_unit_id is None else len(ancestors) - ancestors.index(flow.org_unit_id)
        )
        return organization_specificity

    selected_specificity = max(organization_specificity(flow) for flow in eligible)
    selected = [flow for flow in eligible if organization_specificity(flow) == selected_specificity]
    if len(selected) != 1:
        raise ApprovalError("Approval-flow routing is ambiguous; narrow the configured ranges")
    chosen = selected[0]
    steps = list(
        session.scalars(
            select(ApprovalStep)
            .where(ApprovalStep.flow_id == chosen.id)
            .order_by(ApprovalStep.step_order)
            .with_for_update()
        ).all()
    )
    if not steps or [step.step_order for step in steps] != list(range(1, len(steps) + 1)):
        raise ApprovalError("The selected approval flow has invalid ordered steps")
    return chosen, steps


def start_instance(
    session: Session,
    *,
    flow: ApprovalFlow,
    steps: list[ApprovalStep],
    business_type: ApprovalBusinessType,
    business_id: int,
    requester_id: int,
    org_unit_id: int,
    amount: Decimal,
) -> ApprovalInstance:
    snapshot = {
        "flow_code": flow.code,
        "flow_name": flow.name,
        "steps": [
            {"step_order": step.step_order, "name": step.name, "role_code": step.role_code}
            for step in steps
        ],
    }
    instance = ApprovalInstance(
        flow_id=flow.id,
        business_type=business_type,
        business_id=business_id,
        requester_id=requester_id,
        org_unit_id=org_unit_id,
        amount=amount,
        status=ApprovalInstanceStatus.PENDING,
        current_step_order=steps[0].step_order,
        flow_snapshot=snapshot,
        submitted_at=datetime.now(UTC),
    )
    session.add(instance)
    session.flush()
    return instance


def _current_snapshot_step(instance: ApprovalInstance) -> dict:
    if instance.current_step_order is None:
        raise ApprovalError("The approval instance does not have an active step")
    steps = instance.flow_snapshot.get("steps", [])
    for step in steps:
        if step.get("step_order") == instance.current_step_order:
            return step
    raise ApprovalError("The approval instance flow snapshot is invalid")


def user_role_codes(session: Session, user_id: int) -> frozenset[str]:
    return frozenset(
        code
        for (code,) in session.execute(
            select(Role.code)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user_id)
        ).all()
    )


def assert_can_act(session: Session, *, instance: ApprovalInstance, principal: Principal) -> None:
    if instance.status is not ApprovalInstanceStatus.PENDING:
        raise ApprovalError("The approval instance is no longer pending")
    if principal.user_id == instance.requester_id:
        raise ApprovalForbidden("A requester may not approve their own document")
    if not principal.has_permission(Perm.ADJUSTMENT_APPROVE):
        raise ApprovalForbidden("The actor lacks adjustment approval permission")
    scope = resolve_permission_org_scope(session, principal, Perm.ADJUSTMENT_APPROVE)
    if scope is not None and instance.org_unit_id not in scope:
        raise ApprovalForbidden("The document is outside the actor's organization scope")
    step = _current_snapshot_step(instance)
    if step["role_code"] not in user_role_codes(session, principal.user_id):
        raise ApprovalForbidden("The actor does not hold the current approval-step role")


def can_act(session: Session, *, instance: ApprovalInstance, principal: Principal) -> bool:
    try:
        assert_can_act(session, instance=instance, principal=principal)
    except ApprovalError:
        return False
    return True


def can_act_with_context(
    *,
    instance: ApprovalInstance,
    principal: Principal,
    permission_scope: frozenset[int] | None,
    role_codes: frozenset[str],
) -> bool:
    """Pure in-memory eligibility check for a list of already scoped todos.

    ``list_approval_todos`` first evaluates the principal's scope and roles
    once, then calls this helper per row.  Action endpoints continue using
    :func:`assert_can_act`, which rechecks current database authorization at
    the transaction boundary.
    """

    if instance.status is not ApprovalInstanceStatus.PENDING:
        return False
    if principal.user_id == instance.requester_id:
        return False
    if not principal.has_permission(Perm.ADJUSTMENT_APPROVE):
        return False
    if permission_scope is not None and instance.org_unit_id not in permission_scope:
        return False
    try:
        step = _current_snapshot_step(instance)
    except ApprovalError:
        return False
    return step.get("role_code") in role_codes


def current_step_is_final(instance: ApprovalInstance) -> bool:
    step = _current_snapshot_step(instance)
    return step["step_order"] == max(item["step_order"] for item in instance.flow_snapshot["steps"])


def decide(
    session: Session,
    *,
    instance_id: int,
    principal: Principal,
    action: ApprovalActionType,
    comment: str | None,
) -> DecisionResult:
    instance = session.scalars(
        select(ApprovalInstance).where(ApprovalInstance.id == instance_id).with_for_update()
    ).first()
    if instance is None:
        raise ApprovalError("Approval instance not found")
    assert_can_act(session, instance=instance, principal=principal)
    if action not in (ApprovalActionType.APPROVE, ApprovalActionType.REJECT):
        raise ApprovalError("Only approval or rejection is valid for a pending step")
    if action is ApprovalActionType.REJECT and not comment:
        raise ApprovalError("A rejection reason is required")

    current_step = _current_snapshot_step(instance)
    decision = ApprovalAction(
        instance_id=instance.id,
        step_order=current_step["step_order"],
        action=action,
        actor_id=principal.user_id,
        comment=comment,
    )
    session.add(decision)
    is_final = action is ApprovalActionType.APPROVE and current_step_is_final(instance)
    if action is ApprovalActionType.REJECT:
        instance.status = ApprovalInstanceStatus.REJECTED
        instance.current_step_order = None
        instance.resolved_at = datetime.now(UTC)
    elif is_final:
        instance.status = ApprovalInstanceStatus.APPROVED
        instance.current_step_order = None
        instance.resolved_at = datetime.now(UTC)
    else:
        instance.current_step_order = current_step["step_order"] + 1
    session.flush()
    return DecisionResult(instance=instance, action=decision, is_final_approval=is_final)
