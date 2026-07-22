"""Fail-closed freshness guard for salary deliveries routed by DingTalk organization."""

from __future__ import annotations

import hmac
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from app.dingtalk.client import DingTalkOrganizationAccess
from app.dingtalk.org_rules import manager_department_for_title
from app.dingtalk.read_sync import (
    blind_index_dingtalk_user_id,
    dingtalk_organization_identity_proof,
)
from app.models.auth import User, UserReviewScope
from app.models.dingtalk import (
    DingTalkOrgSyncAction,
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncBatchStatus,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
)
from app.models.employee import Department, Employee, EmployeeStatus
from app.models.org import OrgType, OrgUnit


class DingTalkOrganizationFreshnessError(RuntimeError):
    """The requested salary-review scopes lack a recent confirmed DingTalk snapshot."""


def invalidate_applied_reviewer_proofs(
    session: Session,
    *,
    scopes: set[tuple[int, Department]] | frozenset[tuple[int, Department]] = frozenset(),
    employee_ids: set[int] | frozenset[int] = frozenset(),
) -> int:
    """Invalidate direct-sync authorization after any manual routing mutation."""

    predicates = [
        and_(
            DingTalkOrgSyncItem.proposed_org_unit_id == org_unit_id,
            DingTalkOrgSyncItem.department == department,
        )
        for org_unit_id, department in scopes
    ]
    if employee_ids:
        predicates.append(DingTalkOrgSyncItem.proposed_employee_id.in_(employee_ids))
    if not predicates:
        return 0
    result = session.execute(
        update(DingTalkOrgSyncItem)
        .where(
            DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.REVIEWER,
            DingTalkOrgSyncItem.status == DingTalkOrgSyncItemStatus.APPLIED,
            DingTalkOrgSyncItem.applied_identity_proof.is_not(None),
            or_(*predicates),
        )
        .values(applied_identity_proof=None)
    )
    return int(getattr(result, "rowcount", 0) or 0)


def require_recent_organization_scopes(
    session: Session,
    scopes: set[tuple[int, Department]] | frozenset[tuple[int, Department]],
    *,
    freshness_minutes: int = 5,
    encryption_key: str,
    tenant_id: str,
    now: datetime | None = None,
) -> int:
    """Return the qualifying batch id or reject every uncovered review scope.

    Callers must invoke this immediately before staging salary deliveries.  A
    global timestamp is insufficient: every selected store needs both an
    applied store-coverage item and an applied reviewer action in the same
    recent batch.
    """

    if not scopes:
        return 0
    current_time = now or datetime.now(UTC)
    cutoff = current_time - timedelta(minutes=freshness_minutes)
    batch = session.scalars(
        select(DingTalkOrgSyncBatch)
        .where(
            DingTalkOrgSyncBatch.status == DingTalkOrgSyncBatchStatus.APPLIED,
            DingTalkOrgSyncBatch.applied_at.is_not(None),
            DingTalkOrgSyncBatch.applied_at >= cutoff,
        )
        .order_by(DingTalkOrgSyncBatch.applied_at.desc(), DingTalkOrgSyncBatch.id.desc())
        .limit(1)
    ).one_or_none()
    if batch is None:
        raise DingTalkOrganizationFreshnessError(
            "A recent confirmed DingTalk organization sync is required"
        )

    org_ids = {org_unit_id for org_unit_id, _department in scopes}
    items = session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.batch_id == batch.id,
            DingTalkOrgSyncItem.proposed_org_unit_id.in_(org_ids),
        )
    ).all()
    current_store_bindings: dict[int, int] = {
        org_unit_id: remote_department_id
        for org_unit_id, remote_department_id in session.execute(
            select(OrgUnit.id, OrgUnit.dingtalk_dept_id).where(
                OrgUnit.id.in_(org_ids),
                OrgUnit.type == OrgType.STORE,
                OrgUnit.is_deleted.is_(False),
                OrgUnit.status == "ACTIVE",
                OrgUnit.dingtalk_dept_id.is_not(None),
            )
        ).all()
        if remote_department_id is not None
    }
    # Coverage is identity-exact: REGION rows never count, and unrelated
    # historical STORE rows in the same batch neither satisfy nor poison the
    # one current STORE proof required for this local node.
    covered_stores: dict[int, DingTalkOrgSyncItem] = {}
    ambiguous_stores: set[int] = set()
    for item in items:
        if (
            item.kind != DingTalkOrgSyncItemKind.STORE
            or item.status != DingTalkOrgSyncItemStatus.APPLIED
            or item.proposed_org_unit_id is None
            or item.remote_department_id != current_store_bindings.get(item.proposed_org_unit_id)
        ):
            continue
        if item.proposed_org_unit_id in covered_stores:
            ambiguous_stores.add(item.proposed_org_unit_id)
        covered_stores[item.proposed_org_unit_id] = item
    reviewer_proofs: dict[tuple[int, Department], DingTalkOrgSyncItem] = {}
    ambiguous_proofs: set[tuple[int, Department]] = set()
    for item in items:
        if (
            item.kind != DingTalkOrgSyncItemKind.REVIEWER
            or item.status != DingTalkOrgSyncItemStatus.APPLIED
            or item.proposed_org_unit_id is None
            or item.department is None
            or item.proposed_employee_id is None
            or item.applied_identity_proof is None
            or item.remote_department_id != current_store_bindings.get(item.proposed_org_unit_id)
            or item.action != DingTalkOrgSyncAction.ASSIGN_SCOPE
        ):
            continue
        key = (item.proposed_org_unit_id, item.department)
        if key in reviewer_proofs:
            ambiguous_proofs.add(key)
        reviewer_proofs[key] = item

    current_reviewers: dict[tuple[int, Department], tuple[int, str, str]] = {}
    ambiguous_current: set[tuple[int, Department]] = set()
    bindings = session.execute(
        select(
            UserReviewScope.org_unit_id,
            UserReviewScope.department,
            User.employee_id,
            User.dingtalk_user_id_hash,
            Employee.dingtalk_user_id_hash,
        )
        .join(User, User.id == UserReviewScope.user_id)
        .join(Employee, Employee.id == User.employee_id)
        .where(
            UserReviewScope.org_unit_id.in_(org_ids),
            User.is_deleted.is_(False),
            User.status == "ACTIVE",
            User.dingtalk_user_id.is_not(None),
            User.dingtalk_user_id_hash.is_not(None),
            Employee.is_deleted.is_(False),
            Employee.status == EmployeeStatus.ACTIVE,
            Employee.dingtalk_user_id_hash.is_not(None),
        )
    ).all()
    for org_unit_id, department, employee_id, user_hash, employee_hash in bindings:
        if employee_id is None or user_hash is None or employee_hash is None:
            continue
        key = (org_unit_id, department)
        if key in current_reviewers:
            ambiguous_current.add(key)
        current_reviewers[key] = (employee_id, user_hash, employee_hash)

    uncovered = False
    for scope in scopes:
        org_unit_id, _department = scope
        proof = reviewer_proofs.get(scope)
        store_proof = covered_stores.get(org_unit_id)
        current_store_department_id = current_store_bindings.get(org_unit_id)
        current = current_reviewers.get(scope)
        current_identity_matches = False
        if current is not None and current[1] == current[2] and proof is not None:
            try:
                current_proof = dingtalk_organization_identity_proof(
                    current[1],
                    key=encryption_key,
                    tenant_id=tenant_id,
                    batch_public_id=batch.public_id,
                    snapshot_hash=batch.snapshot_hash,
                    remote_department_id=proof.remote_department_id,  # type: ignore[arg-type]
                    org_unit_id=org_unit_id,
                    department=scope[1].value,
                    employee_id=current[0],
                )
            except ValueError:
                current_proof = ""
            current_identity_matches = hmac.compare_digest(
                current_proof,
                proof.applied_identity_proof or "",
            )
        if (
            store_proof is None
            or org_unit_id in ambiguous_stores
            or current_store_department_id is None
            or store_proof.remote_department_id != current_store_department_id
            or proof is None
            or proof.remote_department_id != current_store_department_id
            or current is None
            or scope in ambiguous_proofs
            or scope in ambiguous_current
            or current[0] != proof.proposed_employee_id
            or not current_identity_matches
        ):
            uncovered = True
            break
    if uncovered:
        raise DingTalkOrganizationFreshnessError(
            "Every selected store and current reviewer must match the latest DingTalk sync"
        )
    return batch.id


def require_current_organization_reviewer(
    session: Session,
    *,
    user_id: int,
    org_unit_id: int,
    department: Department,
    access: DingTalkOrganizationAccess,
    encryption_key: str,
    tenant_id: str,
    dining_manager_titles: frozenset[str],
    kitchen_manager_titles: frozenset[str],
) -> int:
    """Validate a live DingTalk manager against the latest applied organization.

    The caller must already hold the organization advisory lock and the shared
    authorization-table locks for the remainder of the salary operation.
    """

    if (
        not access.user.active
        or manager_department_for_title(
            access.user.title,
            dining_titles=dining_manager_titles,
            kitchen_titles=kitchen_manager_titles,
        )
        != department
    ):
        raise DingTalkOrganizationFreshnessError(
            "The DingTalk manager assignment is no longer current"
        )
    try:
        provider_hash = blind_index_dingtalk_user_id(
            access.user.user_id,
            key=encryption_key,
        )
    except ValueError as exc:
        raise DingTalkOrganizationFreshnessError(
            "The DingTalk manager identity is invalid"
        ) from exc

    binding = session.execute(
        select(User, Employee)
        .join(Employee, Employee.id == User.employee_id)
        .where(User.id == user_id)
        .execution_options(populate_existing=True)
    ).one_or_none()
    store = session.scalars(
        select(OrgUnit).where(OrgUnit.id == org_unit_id).execution_options(populate_existing=True)
    ).one_or_none()
    if binding is None or store is None:
        raise DingTalkOrganizationFreshnessError(
            "The local manager assignment is no longer current"
        )
    user, employee = binding
    if (
        user.is_deleted
        or user.status != "ACTIVE"
        or employee.is_deleted
        or employee.status != EmployeeStatus.ACTIVE
        or user.dingtalk_user_id is None
        or user.dingtalk_user_id_hash is None
        or employee.dingtalk_user_id_hash is None
        or user.dingtalk_user_id_hash != employee.dingtalk_user_id_hash
        or not secrets.compare_digest(user.dingtalk_user_id, access.user.user_id)
        or not hmac.compare_digest(user.dingtalk_user_id_hash, provider_hash)
        or store.is_deleted
        or store.status != "ACTIVE"
        or store.type != OrgType.STORE
        or store.dingtalk_dept_id is None
    ):
        raise DingTalkOrganizationFreshnessError(
            "The local manager assignment is no longer current"
        )

    scope_user_ids = set(
        session.scalars(
            select(UserReviewScope.user_id).where(
                UserReviewScope.org_unit_id == org_unit_id,
                UserReviewScope.department == department,
            )
        ).all()
    )
    if scope_user_ids != {user_id}:
        raise DingTalkOrganizationFreshnessError("The local manager scope is no longer current")

    all_store_department_ids = set(
        session.scalars(
            select(OrgUnit.dingtalk_dept_id).where(
                OrgUnit.type == OrgType.STORE,
                OrgUnit.is_deleted.is_(False),
                OrgUnit.dingtalk_dept_id.is_not(None),
            )
        ).all()
    )
    current_store_ids = {
        store_department_id
        for store_department_id in all_store_department_ids
        if any(store_department_id in parent_path for parent_path in access.parent_department_paths)
    }
    if current_store_ids != {store.dingtalk_dept_id}:
        raise DingTalkOrganizationFreshnessError(
            "The DingTalk manager belongs to a different or ambiguous store"
        )

    batch = session.scalars(
        select(DingTalkOrgSyncBatch)
        .where(
            DingTalkOrgSyncBatch.status == DingTalkOrgSyncBatchStatus.APPLIED,
            DingTalkOrgSyncBatch.applied_at.is_not(None),
        )
        .order_by(DingTalkOrgSyncBatch.applied_at.desc(), DingTalkOrgSyncBatch.id.desc())
        .limit(1)
    ).one_or_none()
    if batch is None:
        raise DingTalkOrganizationFreshnessError(
            "A confirmed DingTalk organization sync is required"
        )
    items = session.scalars(
        select(DingTalkOrgSyncItem).where(
            DingTalkOrgSyncItem.batch_id == batch.id,
            DingTalkOrgSyncItem.proposed_org_unit_id == org_unit_id,
        )
    ).all()
    store_items = [
        item
        for item in items
        if item.kind == DingTalkOrgSyncItemKind.STORE
        and item.status == DingTalkOrgSyncItemStatus.APPLIED
        and item.remote_department_id == store.dingtalk_dept_id
    ]
    reviewer_items = [
        item
        for item in items
        if item.kind == DingTalkOrgSyncItemKind.REVIEWER
        and item.status == DingTalkOrgSyncItemStatus.APPLIED
        and item.department == department
        and item.proposed_employee_id == employee.id
        and item.remote_department_id == store.dingtalk_dept_id
        and item.applied_identity_proof is not None
        and item.action == DingTalkOrgSyncAction.ASSIGN_SCOPE
    ]
    if len(store_items) != 1 or len(reviewer_items) != 1:
        raise DingTalkOrganizationFreshnessError(
            "The manager scope is not confirmed by the latest DingTalk sync"
        )
    reviewer_item = reviewer_items[0]
    try:
        expected_proof = dingtalk_organization_identity_proof(
            provider_hash,
            key=encryption_key,
            tenant_id=tenant_id,
            batch_public_id=batch.public_id,
            snapshot_hash=batch.snapshot_hash,
            remote_department_id=store.dingtalk_dept_id,
            org_unit_id=org_unit_id,
            department=department.value,
            employee_id=employee.id,
        )
    except ValueError as exc:
        raise DingTalkOrganizationFreshnessError("The manager identity proof is invalid") from exc
    if not hmac.compare_digest(
        expected_proof,
        reviewer_item.applied_identity_proof or "",
    ):
        raise DingTalkOrganizationFreshnessError("The manager identity proof no longer matches")
    return batch.id
