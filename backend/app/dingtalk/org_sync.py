"""Two-phase DingTalk organization synchronization for HR confirmation.

Provider user identifiers and names live only in the in-memory provider
snapshot.  A preview persists a keyed digest plus internal proposals; apply
re-reads DingTalk and resolves that digest back to the raw identifier only
inside the confirmation transaction.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import unicodedata
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.service import revoke_all_for_user
from app.core.security import hash_password
from app.dingtalk.client import (
    DingTalkDepartment,
    DingTalkOrganizationSnapshot,
    DingTalkOrganizationUser,
)
from app.dingtalk.org_freshness import invalidate_applied_reviewer_proofs
from app.dingtalk.org_rules import manager_department_for_title
from app.dingtalk.org_structure import (
    ClassifiedNode,
    ClassifiedOrganization,
    OrganizationStructureError,
    classify_organization,
    normalize_org_name,
)
from app.dingtalk.read_sync import (
    blind_index_dingtalk_user_id,
    dingtalk_organization_identity_proof,
)
from app.models.auth import Role, User, UserReviewScope, UserRole
from app.models.dingtalk import (
    DingTalkOrgSyncAction,
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncBatchStatus,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
    DingTalkOrgSyncTrigger,
)
from app.models.employee import Department, Employee, EmployeeStatus
from app.models.org import OrgType, OrgUnit

_PREVIEW_TTL = timedelta(minutes=15)
_MAX_PATH_LENGTH = 1024
_ORG_SYNC_LOCK_NAME = "compensation-platform:dingtalk-organization-sync:v2"
_ALLOWED_REVIEWER_ROLES = frozenset({"STORE_MANAGER", "EMPLOYEE"})


class DingTalkOrganizationSyncError(RuntimeError):
    """A safe, stable organization-sync failure for the HTTP adapter."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class _ConcurrentChange(RuntimeError):
    """Internal marker for a write race detected after baseline validation."""


@dataclass(frozen=True)
class OrganizationNodePreviewItem:
    id: int
    kind: OrgType
    action: DingTalkOrgSyncAction
    change_fields: tuple[str, ...]
    match_method: str
    remote_department_id: int | None
    remote_department_name: str
    remote_department_path: str
    proposed_org_unit_id: int | None
    proposed_org_unit_name: str | None
    proposed_parent_org_unit_id: int | None
    proposed_parent_org_unit_name: str | None
    status: DingTalkOrgSyncItemStatus
    conflict_code: str | None


@dataclass(frozen=True)
class ReviewerPreviewItem:
    id: int
    action: str
    match_method: str
    remote_department_id: int | None
    remote_department_name: str
    remote_department_path: str
    department: Department
    dingtalk_name: str | None
    current_reviewer_name: str | None
    proposed_employee_id: int | None
    proposed_employee_name: str | None
    status: DingTalkOrgSyncItemStatus
    conflict_code: str | None


@dataclass(frozen=True)
class OrganizationPreview:
    batch_id: str
    trigger: DingTalkOrgSyncTrigger
    created_at: datetime
    last_checked_at: datetime
    expires_at: datetime
    remote_regions: int
    local_regions: int
    ready_regions: int
    region_conflicts: int
    remote_stores: int
    local_stores: int
    ready_stores: int
    store_conflicts: int
    ready_reviewers: int
    reviewer_conflicts: int
    warnings: int
    region_items: tuple[OrganizationNodePreviewItem, ...]
    store_items: tuple[OrganizationNodePreviewItem, ...]
    reviewer_items: tuple[ReviewerPreviewItem, ...]


@dataclass(frozen=True)
class OrganizationApplyResult:
    applied_regions: int
    applied_stores: int
    applied_reviewers: int
    unresolved: int
    already_applied: bool


@dataclass(frozen=True)
class _AppliedReviewerIdentityProof:
    batch_public_id: str
    snapshot_hash: str
    remote_department_id: int
    org_unit_id: int
    department: Department
    employee_id: int
    identity_proof: str


def get_latest_organization_preview(session: Session) -> OrganizationPreview | None:
    """Return the newest persisted organization preview without contacting DingTalk."""

    batch = session.scalars(
        select(DingTalkOrgSyncBatch)
        .where(
            DingTalkOrgSyncBatch.status.in_(
                (
                    DingTalkOrgSyncBatchStatus.PREVIEWED,
                    DingTalkOrgSyncBatchStatus.APPLIED,
                    DingTalkOrgSyncBatchStatus.STALE,
                )
            )
        )
        .order_by(DingTalkOrgSyncBatch.created_at.desc(), DingTalkOrgSyncBatch.id.desc())
        .limit(1)
    ).one_or_none()
    if batch is None:
        return None

    items = _load_preview_items(session, batch.id)
    state = _load_local_state(session)
    return _organization_preview_result(
        batch,
        items,
        state=state,
        reviewer_metadata=_stored_reviewer_metadata(state, items),
    )


def get_applied_organization_sync_result(
    session: Session,
    public_id: str,
) -> OrganizationApplyResult | None:
    """Return a completed result locally so an idempotent retry needs no provider read."""

    batch = session.scalars(
        select(DingTalkOrgSyncBatch).where(DingTalkOrgSyncBatch.public_id == public_id)
    ).one_or_none()
    if batch is None:
        raise DingTalkOrganizationSyncError("BATCH_NOT_FOUND", "Organization preview not found")
    if batch.status != DingTalkOrgSyncBatchStatus.APPLIED:
        return None
    unresolved = session.scalar(
        select(func.count())
        .select_from(DingTalkOrgSyncItem)
        .where(
            DingTalkOrgSyncItem.batch_id == batch.id,
            DingTalkOrgSyncItem.status == DingTalkOrgSyncItemStatus.CONFLICT,
        )
    )
    return OrganizationApplyResult(
        applied_regions=batch.ready_region_count,
        applied_stores=batch.ready_store_count,
        applied_reviewers=batch.ready_reviewer_count,
        unresolved=int(unresolved or 0),
        already_applied=True,
    )


@dataclass(frozen=True)
class _LocalState:
    org_units: tuple[OrgUnit, ...]
    employees: tuple[Employee, ...]
    users: tuple[User, ...]
    roles: tuple[Role, ...]
    user_roles: tuple[UserRole, ...]
    review_scopes: tuple[UserReviewScope, ...]
    applied_reviewer_proofs: tuple[_AppliedReviewerIdentityProof, ...]


@dataclass
class _ReviewerDraft:
    row: DingTalkOrgSyncItem
    dingtalk_name: str | None
    current_reviewer_name: str | None
    proposed_employee_name: str | None


@dataclass(frozen=True)
class _ReviewerIdentityIndex:
    trusted_by_hash: dict[str, tuple[Employee, ...]]
    all_by_hash: dict[str, tuple[Employee, ...]]
    active_by_emp_no: dict[str, tuple[Employee, ...]]


@dataclass(frozen=True)
class _AuthorityIndex:
    anchors_by_root: dict[int, OrgUnit]
    roots_by_org_id: dict[int, frozenset[int]]
    anchor_by_org_id: dict[int, OrgUnit]
    path_candidates: dict[tuple[int, tuple[str, ...], OrgType], tuple[OrgUnit, ...]]
    bound_path_candidates: dict[tuple[int, tuple[str, ...], OrgType], tuple[OrgUnit, ...]]
    exact_store_paths: frozenset[tuple[int, tuple[str, ...]]]
    local_by_remote_id: dict[int, tuple[OrgUnit, ...]]
    local_nodes: tuple[OrgUnit, ...]
    org_units_by_code: dict[str, OrgUnit]


@dataclass(frozen=True)
class _NodePlan:
    classified: ClassifiedOrganization
    rows: tuple[DingTalkOrgSyncItem, ...]
    region_rows: tuple[DingTalkOrgSyncItem, ...]
    store_rows: tuple[DingTalkOrgSyncItem, ...]
    store_row_by_remote_id: dict[int, DingTalkOrgSyncItem]
    store_matches: dict[int, OrgUnit | None]
    local_authority_nodes: tuple[OrgUnit, ...]
    local_only_stores: tuple[OrgUnit, ...]


def take_organization_sync_lock(session: Session) -> None:
    """Serialize organization apply and manual provider-identity corrections."""

    if session.get_bind().dialect.name == "postgresql":
        session.scalar(select(func.pg_advisory_xact_lock(func.hashtext(_ORG_SYNC_LOCK_NAME))))


def take_organization_access_lock(session: Session) -> None:
    """Share the organization lock across concurrent manager review requests."""

    if session.get_bind().dialect.name == "postgresql":
        session.scalar(
            select(func.pg_advisory_xact_lock_shared(func.hashtext(_ORG_SYNC_LOCK_NAME)))
        )


def _normalize_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold().strip()
    normalized = "".join(character for character in normalized if character.isalnum())
    return normalized[:-1] if normalized.endswith("店") else normalized


def _normalize_title(value: str | None) -> str:
    if not value:
        return ""
    return "".join(unicodedata.normalize("NFKC", value).split())


def _fingerprint(*parts: object) -> str:
    encoded = json.dumps(parts, ensure_ascii=False, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _snapshot_hash(snapshot: DingTalkOrganizationSnapshot, *, encryption_key: str) -> str:
    """Return an HMAC over a canonical snapshot without storing raw user ids."""

    departments = sorted(
        (department.department_id, department.parent_id, department.name)
        for department in snapshot.departments
    )
    users = sorted(
        (
            blind_index_dingtalk_user_id(user.user_id, key=encryption_key),
            user.name,
            user.job_number,
            user.title,
            user.active,
            tuple(sorted(user.department_ids)),
        )
        for user in snapshot.users
    )
    payload = json.dumps((departments, users), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    derived_key = hashlib.sha256(
        b"compensation-platform:dingtalk-org-snapshot:v2\0" + encryption_key.encode("utf-8")
    ).digest()
    return hmac.new(derived_key, payload, hashlib.sha256).hexdigest()


def _root_config_hash(root_mappings: tuple[tuple[int, OrgUnit], ...]) -> str:
    """Fingerprint configured remote-root to immutable-local-anchor bindings only."""

    payload = json.dumps(
        sorted((int(remote_root_id), anchor.code) for remote_root_id, anchor in root_mappings),
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _persisted_reviewer_action(action: str) -> tuple[DingTalkOrgSyncAction, list[str]]:
    mapping = {
        "ASSIGN": (DingTalkOrgSyncAction.ASSIGN_SCOPE, ["reviewer_scope"]),
        "REMOVE": (DingTalkOrgSyncAction.REMOVE_SCOPE, ["reviewer_scope"]),
        "CONFLICT": (DingTalkOrgSyncAction.NO_CHANGE, []),
    }
    try:
        return mapping[action]
    except KeyError as exc:
        raise RuntimeError("invalid reviewer persistence action") from exc


def _relative_remote_path(
    node: ClassifiedNode,
    departments_by_id: dict[int, DingTalkDepartment],
) -> str:
    parts: list[str] = []
    current = node.department
    visited: set[int] = set()
    while current.department_id != node.root_id:
        if current.department_id in visited:
            raise RuntimeError("classified organization path contains a cycle")
        visited.add(current.department_id)
        parts.append(current.name)
        parent = departments_by_id.get(current.parent_id) if current.parent_id else None
        if parent is None:
            if current.parent_id == node.root_id:
                break
            raise RuntimeError("classified organization path left its configured root")
        current = parent
    path = " / ".join(reversed(parts))
    if len(path) > _MAX_PATH_LENGTH:
        raise DingTalkOrganizationSyncError(
            "ORG_SNAPSHOT_INVALID", "DingTalk organization path exceeds the storage limit"
        )
    return path


def _local_relative_path(
    organization: OrgUnit,
    anchor: OrgUnit,
    org_units_by_id: dict[int, OrgUnit],
) -> tuple[str, ...] | None:
    """Return a normalized path only when the node remains below the anchor."""

    parts: list[str] = []
    current: OrgUnit | None = organization
    visited: set[int] = set()
    while current is not None and current.id not in visited:
        if current.id == anchor.id:
            return tuple(reversed(parts))
        visited.add(current.id)
        parts.append(normalize_org_name(current.name))
        current = org_units_by_id.get(current.parent_id) if current.parent_id else None
    return None


def _display_local_relative_path(
    organization: OrgUnit,
    anchor: OrgUnit,
    org_units_by_id: dict[int, OrgUnit],
) -> str:
    parts: list[str] = []
    current: OrgUnit | None = organization
    visited: set[int] = set()
    while current is not None and current.id not in visited:
        if current.id == anchor.id:
            return " / ".join(reversed(parts))[:_MAX_PATH_LENGTH]
        visited.add(current.id)
        parts.append(current.name)
        current = org_units_by_id.get(current.parent_id) if current.parent_id else None
    raise RuntimeError("organization left its validated authority anchor")


def _load_local_state(session: Session, *, for_update: bool = False) -> _LocalState:
    org_statement = select(OrgUnit).order_by(OrgUnit.id)
    employee_statement = select(Employee).order_by(Employee.id)
    user_statement = select(User).order_by(User.id)
    role_statement = select(Role).order_by(Role.id)
    user_role_statement = select(UserRole).order_by(UserRole.id)
    scope_statement = select(UserReviewScope).order_by(UserReviewScope.id)
    proof_statement = (
        select(DingTalkOrgSyncBatch, DingTalkOrgSyncItem)
        .join(DingTalkOrgSyncItem, DingTalkOrgSyncItem.batch_id == DingTalkOrgSyncBatch.id)
        .where(
            DingTalkOrgSyncBatch.status == DingTalkOrgSyncBatchStatus.APPLIED,
            DingTalkOrgSyncItem.kind == DingTalkOrgSyncItemKind.REVIEWER,
            DingTalkOrgSyncItem.status == DingTalkOrgSyncItemStatus.APPLIED,
            DingTalkOrgSyncItem.action == DingTalkOrgSyncAction.ASSIGN_SCOPE,
            DingTalkOrgSyncItem.applied_identity_proof.is_not(None),
            DingTalkOrgSyncItem.remote_department_id.is_not(None),
            DingTalkOrgSyncItem.proposed_org_unit_id.is_not(None),
            DingTalkOrgSyncItem.department.is_not(None),
            DingTalkOrgSyncItem.proposed_employee_id.is_not(None),
        )
        .order_by(DingTalkOrgSyncBatch.id, DingTalkOrgSyncItem.id)
    )
    if for_update:
        org_statement = org_statement.with_for_update()
        employee_statement = employee_statement.with_for_update()
        user_statement = user_statement.with_for_update()
        role_statement = role_statement.with_for_update()
        user_role_statement = user_role_statement.with_for_update()
        scope_statement = scope_statement.with_for_update()
        proof_statement = proof_statement.with_for_update()

    # Keep the formal-data lock order aligned with reviewer administration:
    # user -> organization -> employee -> RBAC -> review scope.  In
    # particular, locking the store row protects an empty (store, department)
    # scope from a concurrent insertion after its baseline was checked.
    users = tuple(session.scalars(user_statement).all())
    org_units = tuple(session.scalars(org_statement).all())
    employees = tuple(session.scalars(employee_statement).all())
    roles = tuple(session.scalars(role_statement).all())
    user_roles = tuple(session.scalars(user_role_statement).all())
    review_scopes = tuple(session.scalars(scope_statement).all())
    proof_rows = session.execute(proof_statement).all()
    return _LocalState(
        users=users,
        org_units=org_units,
        employees=employees,
        roles=roles,
        user_roles=user_roles,
        review_scopes=review_scopes,
        applied_reviewer_proofs=tuple(
            _AppliedReviewerIdentityProof(
                batch_public_id=batch.public_id,
                snapshot_hash=batch.snapshot_hash,
                remote_department_id=item.remote_department_id,  # type: ignore[arg-type]
                org_unit_id=item.proposed_org_unit_id,  # type: ignore[arg-type]
                department=item.department,  # type: ignore[arg-type]
                employee_id=item.proposed_employee_id,  # type: ignore[arg-type]
                identity_proof=item.applied_identity_proof,  # type: ignore[arg-type]
            )
            for batch, item in proof_rows
        ),
    )


def _resolve_root_mappings(
    state: _LocalState,
    root_mappings: tuple[tuple[int, str], ...],
) -> tuple[tuple[int, OrgUnit], ...]:
    if not root_mappings:
        raise DingTalkOrganizationSyncError(
            "ORG_ROOT_CONFIG_INVALID", "DingTalk organization roots are not configured"
        )
    remote_ids: set[int] = set()
    resolved: list[tuple[int, OrgUnit]] = []
    for remote_id, anchor_code in root_mappings:
        if (
            not isinstance(remote_id, int)
            or isinstance(remote_id, bool)
            or remote_id <= 0
            or remote_id in remote_ids
            or not anchor_code.strip()
        ):
            raise DingTalkOrganizationSyncError(
                "ORG_ROOT_CONFIG_INVALID", "DingTalk organization root mapping is invalid"
            )
        remote_ids.add(remote_id)
        candidates = [
            organization for organization in state.org_units if organization.code == anchor_code
        ]
        if len(candidates) != 1:
            raise DingTalkOrganizationSyncError(
                "ORG_ROOT_CONFIG_INVALID", "Configured local organization anchor is not unique"
            )
        anchor = candidates[0]
        if anchor.is_deleted or anchor.status != "ACTIVE" or anchor.type == OrgType.STORE:
            raise DingTalkOrganizationSyncError(
                "ORG_ROOT_CONFIG_INVALID", "Configured local organization anchor is invalid"
            )
        resolved.append((remote_id, anchor))
    return tuple(sorted(resolved, key=lambda pair: pair[0]))


def _build_authority_index(
    state: _LocalState,
    resolved_root_mappings: tuple[tuple[int, OrgUnit], ...],
    org_units_by_id: dict[int, OrgUnit],
) -> _AuthorityIndex:
    """Build root-aware local indexes once, while preserving shared anchors."""

    roots_by_anchor_id: dict[int, set[int]] = defaultdict(set)
    anchors_by_id: dict[int, OrgUnit] = {}
    for root_id, anchor in resolved_root_mappings:
        roots_by_anchor_id[anchor.id].add(root_id)
        anchors_by_id[anchor.id] = anchor
    distinct_anchors = tuple(anchors_by_id.values())
    for index, first in enumerate(distinct_anchors):
        for second in distinct_anchors[index + 1 :]:
            if (
                _local_relative_path(first, second, org_units_by_id) is not None
                or _local_relative_path(second, first, org_units_by_id) is not None
            ):
                raise DingTalkOrganizationSyncError(
                    "ORG_ROOT_CONFIG_INVALID",
                    "Configured local organization anchors overlap",
                )

    mutable_paths: dict[tuple[int, tuple[str, ...], OrgType], list[OrgUnit]] = defaultdict(list)
    mutable_bound_paths: dict[tuple[int, tuple[str, ...], OrgType], list[OrgUnit]] = defaultdict(
        list
    )
    roots_by_org_id: dict[int, set[int]] = defaultdict(set)
    anchor_by_org_id: dict[int, OrgUnit] = {}
    exact_store_paths: set[tuple[int, tuple[str, ...]]] = set()
    local_nodes: dict[int, OrgUnit] = {}
    for anchor_id, root_ids in roots_by_anchor_id.items():
        anchor = anchors_by_id[anchor_id]
        for organization in state.org_units:
            if organization.is_deleted or organization.id == anchor.id:
                continue
            relative_path = _local_relative_path(organization, anchor, org_units_by_id)
            if relative_path is None or organization.type not in (OrgType.REGION, OrgType.STORE):
                continue
            roots_by_org_id[organization.id].update(root_ids)
            anchor_by_org_id[organization.id] = anchor
            local_nodes[organization.id] = organization
            for root_id in root_ids:
                key = (root_id, relative_path, organization.type)
                if organization.dingtalk_dept_id is None:
                    mutable_paths[key].append(organization)
                else:
                    mutable_bound_paths[key].append(organization)
                if organization.type == OrgType.STORE:
                    exact_store_paths.add((root_id, relative_path))

    local_by_remote_id: dict[int, list[OrgUnit]] = defaultdict(list)
    for organization in state.org_units:
        if organization.dingtalk_dept_id is not None:
            local_by_remote_id[organization.dingtalk_dept_id].append(organization)
    return _AuthorityIndex(
        anchors_by_root=dict(resolved_root_mappings),
        roots_by_org_id={
            org_id: frozenset(root_ids) for org_id, root_ids in roots_by_org_id.items()
        },
        anchor_by_org_id=anchor_by_org_id,
        path_candidates={key: tuple(values) for key, values in mutable_paths.items()},
        bound_path_candidates={key: tuple(values) for key, values in mutable_bound_paths.items()},
        exact_store_paths=frozenset(exact_store_paths),
        local_by_remote_id={key: tuple(values) for key, values in local_by_remote_id.items()},
        local_nodes=tuple(sorted(local_nodes.values(), key=lambda value: value.id)),
        org_units_by_code={organization.code: organization for organization in state.org_units},
    )


def _optional_get[T](mapping: dict[int, T], key: int | None) -> T | None:
    return mapping.get(key) if key is not None else None


def _state_indexes(
    state: _LocalState,
) -> tuple[
    dict[int, OrgUnit],
    dict[int, Employee],
    dict[int, list[User]],
    dict[int, User],
    dict[int, tuple[str, ...]],
    dict[tuple[int, Department], tuple[int, ...]],
]:
    org_units_by_id = {org.id: org for org in state.org_units}
    employees_by_id = {employee.id: employee for employee in state.employees}
    users_by_id = {user.id: user for user in state.users}
    accounts_by_employee: dict[int, list[User]] = defaultdict(list)
    for user in state.users:
        if user.employee_id is not None:
            accounts_by_employee[user.employee_id].append(user)
    role_codes_by_id = {role.id: role.code for role in state.roles}
    role_codes_by_user: dict[int, list[str]] = defaultdict(list)
    for assignment in state.user_roles:
        role_code = role_codes_by_id.get(assignment.role_id)
        if role_code is not None:
            role_codes_by_user[assignment.user_id].append(role_code)
    scopes: dict[tuple[int, Department], list[int]] = defaultdict(list)
    for scope in state.review_scopes:
        scopes[(scope.org_unit_id, scope.department)].append(scope.user_id)
    return (
        org_units_by_id,
        employees_by_id,
        accounts_by_employee,
        users_by_id,
        {user_id: tuple(sorted(codes)) for user_id, codes in role_codes_by_user.items()},
        {key: tuple(sorted(user_ids)) for key, user_ids in scopes.items()},
    )


def _validated_reviewer_identity_bindings(
    state: _LocalState,
    *,
    encryption_key: str,
    tenant_id: str,
) -> frozenset[tuple[int, str]]:
    """Return only provider hashes backed by a recomputable applied proof."""

    org_units_by_id = {organization.id: organization for organization in state.org_units}
    employees_by_id = {employee.id: employee for employee in state.employees}
    accounts_by_employee: dict[int, list[User]] = defaultdict(list)
    users_by_id = {user.id: user for user in state.users}
    for user in state.users:
        if user.employee_id is not None:
            accounts_by_employee[user.employee_id].append(user)
    scopes_by_pair: dict[tuple[int, Department], list[int]] = defaultdict(list)
    for scope in state.review_scopes:
        scopes_by_pair[(scope.org_unit_id, scope.department)].append(scope.user_id)

    trusted: set[tuple[int, str]] = set()
    for evidence in state.applied_reviewer_proofs:
        store = org_units_by_id.get(evidence.org_unit_id)
        employee = employees_by_id.get(evidence.employee_id)
        accounts = accounts_by_employee.get(evidence.employee_id, [])
        if (
            store is None
            or store.is_deleted
            or store.status != "ACTIVE"
            or store.type != OrgType.STORE
            or store.dingtalk_dept_id != evidence.remote_department_id
            or employee is None
            or employee.is_deleted
            or employee.status != EmployeeStatus.ACTIVE
            or employee.dingtalk_user_id_hash is None
            or len(accounts) != 1
        ):
            continue
        account = accounts[0]
        if (
            users_by_id.get(account.id) is not account
            or account.is_deleted
            or account.status != "ACTIVE"
            or account.dingtalk_user_id is None
            or account.dingtalk_user_id_hash != employee.dingtalk_user_id_hash
            or tuple(sorted(scopes_by_pair.get((store.id, evidence.department), [])))
            != (account.id,)
        ):
            continue
        try:
            expected_proof = dingtalk_organization_identity_proof(
                employee.dingtalk_user_id_hash,
                key=encryption_key,
                tenant_id=tenant_id,
                batch_public_id=evidence.batch_public_id,
                snapshot_hash=evidence.snapshot_hash,
                remote_department_id=evidence.remote_department_id,
                org_unit_id=evidence.org_unit_id,
                department=evidence.department.value,
                employee_id=evidence.employee_id,
            )
        except ValueError:
            continue
        if hmac.compare_digest(expected_proof, evidence.identity_proof):
            trusted.add((employee.id, employee.dingtalk_user_id_hash))
    return frozenset(trusted)


def _complete_local_baseline(
    state: _LocalState,
    *,
    trusted_bindings: frozenset[tuple[int, str]] = frozenset(),
) -> str:
    """Fingerprint every local row that can alter organization or reviewer planning."""

    return _fingerprint(
        "COMPLETE_LOCAL_BASELINE",
        tuple(
            (
                org.id,
                org.parent_id,
                org.type.value,
                org.name,
                org.code,
                org.dingtalk_dept_id,
                org.city,
                org.status,
                org.is_deleted,
                org.updated_at,
            )
            for org in state.org_units
        ),
        tuple(
            (
                employee.id,
                employee.emp_no,
                employee.name,
                employee.version,
                employee.status.value,
                employee.org_unit_id,
                employee.department.value,
                employee.dingtalk_user_id_hash,
                employee.is_deleted,
                employee.updated_at,
            )
            for employee in state.employees
        ),
        tuple(
            (
                user.id,
                user.employee_id,
                user.status,
                user.login_enabled,
                user.dingtalk_user_id_hash,
                user.is_deleted,
                user.updated_at,
            )
            for user in state.users
        ),
        tuple((role.id, role.code, role.updated_at) for role in state.roles),
        tuple((assignment.user_id, assignment.role_id) for assignment in state.user_roles),
        tuple(
            (scope.user_id, scope.org_unit_id, scope.department.value)
            for scope in state.review_scopes
        ),
        tuple(sorted(trusted_bindings)),
    )


def _wrap_baselines(items: list[DingTalkOrgSyncItem], complete_local_baseline: str) -> None:
    for item in items:
        item.baseline_fingerprint = _fingerprint(item.baseline_fingerprint, complete_local_baseline)


def _store_baseline(store: OrgUnit | None) -> str:
    if store is None:
        return _fingerprint(None)
    return _fingerprint(
        store.id,
        store.parent_id,
        store.type.value,
        store.name,
        store.code,
        store.dingtalk_dept_id,
        store.city,
        store.status,
        store.is_deleted,
        store.updated_at,
    )


def _resolved_store_baseline(store: OrgUnit | None) -> str:
    """Fingerprint only the resolved target; the batch stores the complete local hash."""

    return _fingerprint("RESOLVED_ORGANIZATION", _store_baseline(store))


def _create_store_baseline(
    *,
    parent: OrgUnit | None,
    code: str,
    name: str,
) -> str:
    return _fingerprint(
        "CREATE",
        _store_baseline(parent),
        code,
        normalize_org_name(name),
    )


def _dingtalk_org_code(kind: DingTalkOrgSyncItemKind | OrgType, remote_id: int) -> str:
    if kind in (DingTalkOrgSyncItemKind.REGION, OrgType.REGION):
        return f"DINGTALK-R-{remote_id}"
    if kind in (DingTalkOrgSyncItemKind.STORE, OrgType.STORE):
        return f"DINGTALK-S-{remote_id}"
    raise RuntimeError("reviewer items do not have organization codes")


def _lock_org_unit_table_against_phantoms(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE org_unit IN SHARE ROW EXCLUSIVE MODE"))


def _locked_local_depths(
    parent_by_id: dict[int, int | None],
    organization_ids: set[int],
) -> dict[int, int]:
    """Resolve local hierarchy depths without trusting staged display paths."""

    depths: dict[int, int] = {}
    for organization_id in organization_ids:
        current_id = organization_id
        path: list[int] = []
        seen: set[int] = set()
        while current_id not in depths:
            if current_id in seen:
                raise _ConcurrentChange("organization hierarchy contains a cycle")
            seen.add(current_id)
            if current_id not in parent_by_id:
                raise _ConcurrentChange("organization hierarchy contains an orphan")
            path.append(current_id)
            parent_id = parent_by_id[current_id]
            if parent_id is None:
                depth = 0
                break
            current_id = parent_id
        else:
            depth = depths[current_id]
        for path_id in reversed(path):
            depth += 1
            depths[path_id] = depth
    return {organization_id: depths[organization_id] for organization_id in organization_ids}


def _reviewer_baseline(
    *,
    store: OrgUnit | None,
    department: Department,
    employee: Employee | None,
    accounts: list[User],
    scope_user_ids: tuple[int, ...],
    role_codes_by_user: dict[int, tuple[str, ...]],
) -> str:
    employee_state: object
    if employee is None:
        employee_state = None
    else:
        employee_state = (
            employee.id,
            employee.emp_no,
            employee.name,
            employee.version,
            employee.status.value,
            employee.org_unit_id,
            employee.department.value,
            employee.dingtalk_user_id_hash,
            employee.is_deleted,
            employee.updated_at,
        )
    account_state = tuple(
        sorted(
            (
                account.id,
                account.employee_id,
                account.status,
                account.login_enabled,
                account.dingtalk_user_id_hash,
                account.is_deleted,
                account.updated_at,
                role_codes_by_user.get(account.id, ()),
            )
            for account in accounts
        )
    )
    scope_accounts = tuple(
        (
            user_id,
            role_codes_by_user.get(user_id, ()),
        )
        for user_id in scope_user_ids
    )
    return _fingerprint(
        "REVIEWER",
        _store_baseline(store),
        department.value,
        employee_state,
        account_state,
        scope_accounts,
    )


def _manager_department(
    remote_user: DingTalkOrganizationUser,
    *,
    dining_manager_titles: frozenset[str],
    kitchen_manager_titles: frozenset[str],
) -> Department | None:
    return manager_department_for_title(
        remote_user.title,
        dining_titles=dining_manager_titles,
        kitchen_titles=kitchen_manager_titles,
    )


def _current_reviewer_name(
    scope_user_ids: tuple[int, ...], users_by_id: dict[int, User]
) -> str | None:
    if len(scope_user_ids) != 1:
        return None
    user = users_by_id.get(scope_user_ids[0])
    return user.username if user is not None else None


def _required_reviewer_department(item: DingTalkOrgSyncItem) -> Department:
    if item.department not in (Department.DINING, Department.KITCHEN):
        raise RuntimeError("reviewer sync item is missing its review department")
    return item.department


def _reviewer_preview_action(item: DingTalkOrgSyncItem) -> str:
    if item.action == DingTalkOrgSyncAction.ASSIGN_SCOPE:
        return "ASSIGN"
    if item.action == DingTalkOrgSyncAction.REMOVE_SCOPE:
        return "REMOVE"
    return "CONFLICT"


def _organization_preview_result(
    batch: DingTalkOrgSyncBatch,
    items: list[DingTalkOrgSyncItem],
    *,
    state: _LocalState,
    reviewer_metadata: dict[str, _ReviewerDraft],
) -> OrganizationPreview:
    proposed_names = {org.id: org.name for org in state.org_units if not org.is_deleted}

    def node_view(row: DingTalkOrgSyncItem) -> OrganizationNodePreviewItem:
        if row.proposed_org_type not in (OrgType.REGION, OrgType.STORE):
            raise RuntimeError("organization node preview is missing its proposed type")
        proposed_name = _optional_get(proposed_names, row.proposed_org_unit_id)
        if proposed_name is None and row.action == DingTalkOrgSyncAction.CREATE:
            proposed_name = row.remote_department_name
        return OrganizationNodePreviewItem(
            id=row.id,
            kind=row.proposed_org_type,
            action=row.action,
            change_fields=tuple(row.change_fields),
            match_method=row.match_method,
            remote_department_id=row.remote_department_id,
            remote_department_name=row.remote_department_name,
            remote_department_path=row.remote_department_path,
            proposed_org_unit_id=row.proposed_org_unit_id,
            proposed_org_unit_name=proposed_name,
            proposed_parent_org_unit_id=row.proposed_parent_org_unit_id,
            proposed_parent_org_unit_name=_optional_get(
                proposed_names, row.proposed_parent_org_unit_id
            ),
            status=row.status,
            conflict_code=row.conflict_code,
        )

    reviewer_views: list[ReviewerPreviewItem] = []
    for row in items:
        if row.kind != DingTalkOrgSyncItemKind.REVIEWER:
            continue
        metadata = reviewer_metadata.get(row.row_key)
        reviewer_views.append(
            ReviewerPreviewItem(
                id=row.id,
                action=_reviewer_preview_action(row),
                match_method=row.match_method,
                remote_department_id=row.remote_department_id,
                remote_department_name=row.remote_department_name,
                remote_department_path=row.remote_department_path,
                department=_required_reviewer_department(row),
                dingtalk_name=metadata.dingtalk_name if metadata else None,
                current_reviewer_name=(metadata.current_reviewer_name if metadata else None),
                proposed_employee_id=row.proposed_employee_id,
                proposed_employee_name=(metadata.proposed_employee_name if metadata else None),
                status=row.status,
                conflict_code=row.conflict_code,
            )
        )

    region_items = [item for item in items if item.kind == DingTalkOrgSyncItemKind.REGION]
    store_items = [item for item in items if item.kind == DingTalkOrgSyncItemKind.STORE]
    if batch.last_checked_at is None:
        raise RuntimeError("organization preview is missing its last check time")
    return OrganizationPreview(
        batch_id=batch.public_id,
        trigger=batch.trigger,
        created_at=batch.created_at,
        last_checked_at=batch.last_checked_at,
        expires_at=batch.expires_at,
        remote_regions=batch.remote_region_count,
        local_regions=batch.local_region_count,
        ready_regions=batch.ready_region_count,
        region_conflicts=batch.region_conflict_count,
        remote_stores=batch.remote_store_count,
        local_stores=batch.local_store_count,
        ready_stores=batch.ready_store_count,
        store_conflicts=batch.store_conflict_count,
        ready_reviewers=batch.ready_reviewer_count,
        reviewer_conflicts=batch.reviewer_conflict_count,
        warnings=batch.warning_count,
        region_items=tuple(node_view(row) for row in region_items),
        store_items=tuple(node_view(row) for row in store_items),
        reviewer_items=tuple(reviewer_views),
    )


def _plan_organization_nodes(
    state: _LocalState,
    snapshot: DingTalkOrganizationSnapshot,
    *,
    authority: _AuthorityIndex,
    org_units_by_id: dict[int, OrgUnit],
) -> _NodePlan:
    departments_by_id: dict[int, DingTalkDepartment] = {}
    for department in snapshot.departments:
        if department.department_id in departments_by_id:
            raise DingTalkOrganizationSyncError(
                "ORG_SNAPSHOT_INVALID", "DingTalk returned a duplicate department"
            )
        departments_by_id[department.department_id] = department
    bound_types = {
        organization.dingtalk_dept_id: organization.type
        for organization in state.org_units
        if not organization.is_deleted and organization.dingtalk_dept_id is not None
    }
    try:
        classified = classify_organization(
            snapshot,
            root_ids=frozenset(authority.anchors_by_root),
            bound_types=bound_types,
            exact_store_paths=authority.exact_store_paths,
        )
    except OrganizationStructureError as exc:
        raise DingTalkOrganizationSyncError(exc.code, str(exc)) from None

    rows: list[DingTalkOrgSyncItem] = []
    rows_by_remote_id: dict[int, DingTalkOrgSyncItem] = {}
    node_matches: dict[int, OrgUnit | None] = {}
    matched_local_ids: set[int] = set()
    classified_nodes = sorted(
        (*classified.regions, *classified.stores),
        key=lambda node: (node.depth, node.department.department_id),
    )
    classified_ids = {node.department.department_id for node in classified_nodes}
    remote_nodes_by_path: dict[tuple[int, tuple[str, ...], OrgType], list[int]] = defaultdict(list)
    for node in classified_nodes:
        remote_nodes_by_path[(node.root_id, node.relative_path, node.kind)].append(
            node.department.department_id
        )
    ambiguous_remote_ids = {
        remote_id
        for remote_ids in remote_nodes_by_path.values()
        if len(remote_ids) > 1
        for remote_id in remote_ids
    }

    for node in classified_nodes:
        remote = node.department
        stable_candidates = authority.local_by_remote_id.get(remote.department_id, ())
        key = (node.root_id, node.relative_path, node.kind)
        local_candidates = authority.path_candidates.get(key, ())
        bound_path_candidates = authority.bound_path_candidates.get(key, ())
        proposed: OrgUnit | None = None
        proposed_parent: OrgUnit | None = None
        parent_is_staged_create = False
        conflict_code: str | None = (
            "ORG_PATH_AMBIGUOUS" if remote.department_id in ambiguous_remote_ids else None
        )
        match_method = "NO_LOCAL_PATH_MATCH"
        if stable_candidates:
            match_method = "STABLE_DEPARTMENT_ID"
            if (
                len(stable_candidates) != 1
                or stable_candidates[0].type != node.kind
                or stable_candidates[0].is_deleted
                or node.root_id
                not in authority.roots_by_org_id.get(stable_candidates[0].id, frozenset())
            ):
                conflict_code = "ORG_NODE_CLASSIFICATION_CONFLICT"
            else:
                proposed = stable_candidates[0]
        elif bound_path_candidates:
            conflict_code = "ORG_PATH_AMBIGUOUS"
            match_method = "BOUND_TO_DIFFERENT_DEPARTMENT_ID"
        elif len(local_candidates) == 1:
            proposed = local_candidates[0]
            match_method = "EXACT_RELATIVE_PATH"
        elif len(local_candidates) > 1:
            conflict_code = "ORG_PATH_AMBIGUOUS"
            match_method = "EXACT_RELATIVE_PATH"

        if node.depth == 1:
            proposed_parent = authority.anchors_by_root[node.root_id]
        else:
            parent_row = rows_by_remote_id.get(remote.parent_id or -1)
            if parent_row is None or (remote.parent_id or -1) not in classified_ids:
                conflict_code = conflict_code or "ORG_PATH_AMBIGUOUS"
            elif parent_row.status == DingTalkOrgSyncItemStatus.CONFLICT:
                conflict_code = conflict_code or "ORG_PATH_AMBIGUOUS"
            elif parent_row.proposed_org_unit_id is not None:
                proposed_parent = org_units_by_id[parent_row.proposed_org_unit_id]
            elif parent_row.action == DingTalkOrgSyncAction.CREATE:
                parent_is_staged_create = True
            else:
                conflict_code = conflict_code or "ORG_PATH_AMBIGUOUS"

        change_fields: list[str] = []
        if proposed is None:
            action = DingTalkOrgSyncAction.CREATE
            if (
                _dingtalk_org_code(DingTalkOrgSyncItemKind(node.kind.value), remote.department_id)
                in authority.org_units_by_code
            ):
                conflict_code = conflict_code or "ORG_PATH_AMBIGUOUS"
        else:
            matched_local_ids.add(proposed.id)
            if normalize_org_name(proposed.name) != normalize_org_name(remote.name):
                change_fields.append("name")
            if (
                proposed_parent is not None and proposed.parent_id != proposed_parent.id
            ) or parent_is_staged_create:
                change_fields.append("parent_id")
            if proposed.dingtalk_dept_id is None:
                change_fields.append("dingtalk_dept_id")
            if proposed.status == "HISTORICAL":
                action = DingTalkOrgSyncAction.ACTIVATE
            elif any(field != "dingtalk_dept_id" for field in change_fields):
                action = DingTalkOrgSyncAction.UPDATE
            else:
                action = DingTalkOrgSyncAction.LINK

        status = (
            DingTalkOrgSyncItemStatus.READY
            if conflict_code is None
            else DingTalkOrgSyncItemStatus.CONFLICT
        )
        baseline = (
            _create_store_baseline(
                parent=proposed_parent,
                code=_dingtalk_org_code(
                    DingTalkOrgSyncItemKind(node.kind.value), remote.department_id
                ),
                name=remote.name,
            )
            if proposed is None
            else _resolved_store_baseline(proposed)
        )
        row = DingTalkOrgSyncItem(
            row_key=f"{node.kind.value}:REMOTE:{remote.department_id}",
            kind=DingTalkOrgSyncItemKind(node.kind.value),
            action=action,
            change_fields=change_fields,
            status=status,
            remote_department_id=remote.department_id,
            remote_department_name=remote.name,
            remote_department_path=_relative_remote_path(node, departments_by_id),
            remote_user_id_hash=None,
            proposed_org_unit_id=proposed.id if proposed else None,
            proposed_parent_org_unit_id=proposed_parent.id if proposed_parent else None,
            proposed_employee_id=None,
            proposed_org_type=node.kind,
            department=None,
            match_method=match_method,
            conflict_code=conflict_code,
            baseline_fingerprint=baseline,
        )
        rows.append(row)
        rows_by_remote_id[remote.department_id] = row
        node_matches[remote.department_id] = proposed

    ready_rows_by_local_node: dict[int, list[DingTalkOrgSyncItem]] = defaultdict(list)
    for row in rows:
        if row.status == DingTalkOrgSyncItemStatus.READY and row.proposed_org_unit_id is not None:
            ready_rows_by_local_node[row.proposed_org_unit_id].append(row)
    for duplicate_rows in ready_rows_by_local_node.values():
        if len(duplicate_rows) > 1:
            for row in duplicate_rows:
                row.status = DingTalkOrgSyncItemStatus.CONFLICT
                row.conflict_code = "ORG_PATH_AMBIGUOUS"

    local_only_nodes = [
        organization
        for organization in authority.local_nodes
        if organization.status == "ACTIVE" and organization.id not in matched_local_ids
    ]
    for organization in sorted(local_only_nodes, key=lambda value: (value.type.value, value.id)):
        anchor = authority.anchor_by_org_id[organization.id]
        rows.append(
            DingTalkOrgSyncItem(
                row_key=f"{organization.type.value}:LOCAL:{organization.id}",
                kind=DingTalkOrgSyncItemKind(organization.type.value),
                action=DingTalkOrgSyncAction.DEACTIVATE,
                change_fields=[],
                status=DingTalkOrgSyncItemStatus.READY,
                remote_department_id=None,
                remote_department_name=organization.name,
                remote_department_path=_display_local_relative_path(
                    organization, anchor, org_units_by_id
                ),
                remote_user_id_hash=None,
                proposed_org_unit_id=organization.id,
                proposed_parent_org_unit_id=organization.parent_id,
                proposed_employee_id=None,
                proposed_org_type=organization.type,
                department=None,
                match_method="MISSING_IN_DINGTALK",
                conflict_code=None,
                baseline_fingerprint=_resolved_store_baseline(organization),
            )
        )

    region_rows = tuple(row for row in rows if row.kind == DingTalkOrgSyncItemKind.REGION)
    store_rows = tuple(row for row in rows if row.kind == DingTalkOrgSyncItemKind.STORE)
    store_row_by_remote_id = {
        row.remote_department_id: row for row in store_rows if row.remote_department_id is not None
    }
    return _NodePlan(
        classified=classified,
        rows=tuple(rows),
        region_rows=region_rows,
        store_rows=store_rows,
        store_row_by_remote_id=store_row_by_remote_id,
        store_matches={
            node.department.department_id: node_matches[node.department.department_id]
            for node in classified.stores
        },
        local_authority_nodes=authority.local_nodes,
        local_only_stores=tuple(node for node in local_only_nodes if node.type == OrgType.STORE),
    )


def _nearest_remote_store_by_department(
    snapshot: DingTalkOrganizationSnapshot,
    remote_store_ids: frozenset[int],
) -> dict[int, int | None]:
    departments_by_id = {
        department.department_id: department for department in snapshot.departments
    }
    nearest: dict[int, int | None] = {}
    for department_id in departments_by_id:
        chain: list[int] = []
        current_id: int | None = department_id
        seen: set[int] = set()
        resolved: int | None = None
        while current_id is not None and current_id not in seen:
            if current_id in nearest:
                resolved = nearest[current_id]
                break
            if current_id in remote_store_ids:
                resolved = current_id
                break
            seen.add(current_id)
            chain.append(current_id)
            current = departments_by_id.get(current_id)
            current_id = current.parent_id if current is not None else None
        for chained_id in chain:
            nearest[chained_id] = resolved
        nearest[department_id] = resolved
    return nearest


def _index_reviewer_identities(
    employees: tuple[Employee, ...],
    *,
    trusted_bindings: frozenset[tuple[int, str]] = frozenset(),
) -> _ReviewerIdentityIndex:
    """Index all strict reviewer identity keys in one employee pass."""

    trusted_by_hash: dict[str, list[Employee]] = defaultdict(list)
    all_by_hash: dict[str, list[Employee]] = defaultdict(list)
    active_by_emp_no: dict[str, list[Employee]] = defaultdict(list)
    for employee in employees:
        if employee.dingtalk_user_id_hash:
            all_by_hash[employee.dingtalk_user_id_hash].append(employee)
            if (employee.id, employee.dingtalk_user_id_hash) in trusted_bindings:
                trusted_by_hash[employee.dingtalk_user_id_hash].append(employee)
        if employee.status == EmployeeStatus.ACTIVE and not employee.is_deleted:
            active_by_emp_no[employee.emp_no.strip()].append(employee)
    return _ReviewerIdentityIndex(
        trusted_by_hash={key: tuple(values) for key, values in trusted_by_hash.items()},
        all_by_hash={key: tuple(values) for key, values in all_by_hash.items()},
        active_by_emp_no={key: tuple(values) for key, values in active_by_emp_no.items()},
    )


def _match_reviewer_identity(
    remote_user: DingTalkOrganizationUser,
    *,
    identity_index: _ReviewerIdentityIndex,
    encryption_key: str,
) -> tuple[Employee | None, str, str | None]:
    """Resolve a reviewer only by an established binding or exact employee number."""

    provider_hash = blind_index_dingtalk_user_id(remote_user.user_id, key=encryption_key)
    all_hash_owners = identity_index.all_by_hash.get(provider_hash, ())
    trusted_matches = identity_index.trusted_by_hash.get(provider_hash, ())
    if len(trusted_matches) > 1:
        return None, "STABLE_ID", "ORG_IDENTITY_CONFLICT"
    if len(trusted_matches) == 1:
        stable_employee = trusted_matches[0]
        if all_hash_owners != (stable_employee,):
            return None, "STABLE_ID", "ORG_IDENTITY_CONFLICT"
        if stable_employee.status == EmployeeStatus.ACTIVE and not stable_employee.is_deleted:
            return stable_employee, "STABLE_ID", None
        return None, "STABLE_ID", "ORG_EMPLOYEE_MATCH_FAILED"

    job_number = (remote_user.job_number or "").strip()
    if not job_number:
        return None, "JOB_NUMBER", "ORG_EMPLOYEE_MATCH_FAILED"
    job_number_matches = identity_index.active_by_emp_no.get(job_number, ())
    if len(job_number_matches) == 1:
        employee = job_number_matches[0]
        if employee.dingtalk_user_id_hash not in (None, provider_hash) or any(
            owner.id != employee.id for owner in all_hash_owners
        ):
            return None, "JOB_NUMBER", "ORG_IDENTITY_CONFLICT"
        return employee, "JOB_NUMBER", None
    return (
        None,
        "JOB_NUMBER",
        "ORG_EMPLOYEE_MATCH_FAILED" if not job_number_matches else "ORG_IDENTITY_CONFLICT",
    )


def _plan_organization_reviewers(
    state: _LocalState,
    snapshot: DingTalkOrganizationSnapshot,
    *,
    encryption_key: str,
    node_plan: _NodePlan,
    dining_manager_titles: frozenset[str],
    kitchen_manager_titles: frozenset[str],
    trusted_bindings: frozenset[tuple[int, str]] = frozenset(),
) -> tuple[_ReviewerDraft, ...]:
    (
        _org_units_by_id,
        _employees_by_id,
        accounts_by_employee,
        users_by_id,
        role_codes_by_user,
        scope_users_by_pair,
    ) = _state_indexes(state)
    identity_index = _index_reviewer_identities(
        state.employees,
        trusted_bindings=trusted_bindings,
    )
    active_remote_users = tuple(user for user in snapshot.users if user.active)
    account_ids_by_hash: dict[str, list[int]] = defaultdict(list)
    employee_ids_by_hash: dict[str, list[int]] = defaultdict(list)
    for account in state.users:
        if account.dingtalk_user_id_hash:
            account_ids_by_hash[account.dingtalk_user_id_hash].append(account.id)
    for provider_hash, employees in identity_index.all_by_hash.items():
        employee_ids_by_hash[provider_hash].extend(employee.id for employee in employees)

    remote_store_ids = frozenset(node_plan.store_row_by_remote_id)
    nearest_store = _nearest_remote_store_by_department(snapshot, remote_store_ids)
    users_by_store: dict[int, list[DingTalkOrganizationUser]] = defaultdict(list)
    for user in active_remote_users:
        user_store_ids = {
            store_id
            for department_id in user.department_ids
            if (store_id := nearest_store.get(department_id)) is not None
        }
        for store_id in user_store_ids:
            users_by_store[store_id].append(user)

    reviewer_drafts: list[_ReviewerDraft] = []

    def add_reviewer_row(
        *,
        row_key: str,
        remote_store: DingTalkDepartment | None,
        store: OrgUnit | None,
        department: Department,
        action: str,
        method: str,
        status: DingTalkOrgSyncItemStatus,
        conflict_code: str | None,
        selected_remote: DingTalkOrganizationUser | None,
        selected_employee: Employee | None,
        remote_name: str,
        remote_path: str,
    ) -> None:
        scope_user_ids = scope_users_by_pair.get((store.id, department), ()) if store else ()
        accounts = (
            accounts_by_employee.get(selected_employee.id, [])
            if selected_employee is not None
            else []
        )
        provider_hash = (
            blind_index_dingtalk_user_id(selected_remote.user_id, key=encryption_key)
            if selected_remote is not None
            else None
        )
        persisted_action, change_fields = _persisted_reviewer_action(action)
        row = DingTalkOrgSyncItem(
            row_key=row_key,
            kind=DingTalkOrgSyncItemKind.REVIEWER,
            status=status,
            action=persisted_action,
            remote_department_id=(remote_store.department_id if remote_store is not None else None),
            remote_department_name=remote_name,
            remote_department_path=remote_path,
            remote_user_id_hash=provider_hash,
            proposed_org_unit_id=store.id if store else None,
            proposed_parent_org_unit_id=None,
            proposed_employee_id=(selected_employee.id if selected_employee is not None else None),
            proposed_org_type=None,
            department=department,
            match_method=method,
            conflict_code=conflict_code,
            change_fields=change_fields,
            baseline_fingerprint=_reviewer_baseline(
                store=store,
                department=department,
                employee=selected_employee,
                accounts=accounts,
                scope_user_ids=scope_user_ids,
                role_codes_by_user=role_codes_by_user,
            ),
        )
        reviewer_drafts.append(
            _ReviewerDraft(
                row=row,
                dingtalk_name=(selected_remote.name if selected_remote else None),
                current_reviewer_name=_current_reviewer_name(scope_user_ids, users_by_id),
                proposed_employee_name=(selected_employee.name if selected_employee else None),
            )
        )

    local_store_paths = {
        row.proposed_org_unit_id: row.remote_department_path
        for row in node_plan.store_rows
        if row.action == DingTalkOrgSyncAction.DEACTIVATE and row.proposed_org_unit_id is not None
    }
    for local_store in node_plan.local_only_stores:
        for review_department in (Department.DINING, Department.KITCHEN):
            add_reviewer_row(
                row_key=f"REVIEWER:LOCAL:{local_store.id}:{review_department.value}",
                remote_store=None,
                store=local_store,
                department=review_department,
                action="REMOVE",
                method="CLEAR_UNCOVERED_STORE",
                status=DingTalkOrgSyncItemStatus.READY,
                conflict_code=None,
                selected_remote=None,
                selected_employee=None,
                remote_name=local_store.name,
                remote_path=local_store_paths[local_store.id],
            )

    for classified_store in node_plan.classified.stores:
        remote_store = classified_store.department
        proposed_store = node_plan.store_matches[remote_store.department_id]
        store_row = node_plan.store_row_by_remote_id[remote_store.department_id]
        role_candidates: dict[Department, list[DingTalkOrganizationUser]] = defaultdict(list)
        for remote_candidate in users_by_store.get(remote_store.department_id, ()):
            manager_department = _manager_department(
                remote_candidate,
                dining_manager_titles=dining_manager_titles,
                kitchen_manager_titles=kitchen_manager_titles,
            )
            if manager_department is not None:
                role_candidates[manager_department].append(remote_candidate)

        for review_department in (Department.DINING, Department.KITCHEN):
            candidates = role_candidates.get(review_department, [])
            selected_remote: DingTalkOrganizationUser | None = None
            selected_employee: Employee | None = None
            method = "NONE"
            reviewer_conflict_code: str | None = None
            reviewer_action = "CONFLICT"
            status = DingTalkOrgSyncItemStatus.CONFLICT
            if store_row.status == DingTalkOrgSyncItemStatus.CONFLICT:
                reviewer_conflict_code = "STORE_UNRESOLVED"
            elif not candidates:
                reviewer_action = "REMOVE"
                method = "REMOVE_MISSING_MANAGER"
                status = DingTalkOrgSyncItemStatus.READY
            elif len(candidates) > 1:
                reviewer_conflict_code = "ORG_MANAGER_AMBIGUOUS"
            else:
                selected_remote = candidates[0]
                selected_employee, method, reviewer_conflict_code = _match_reviewer_identity(
                    selected_remote,
                    identity_index=identity_index,
                    encryption_key=encryption_key,
                )
                if reviewer_conflict_code is not None:
                    selected_employee = None
                elif selected_employee is None:
                    reviewer_conflict_code = "ORG_EMPLOYEE_MATCH_FAILED"
                else:
                    accounts = accounts_by_employee.get(selected_employee.id, [])
                    provider_hash = blind_index_dingtalk_user_id(
                        selected_remote.user_id, key=encryption_key
                    )
                    if len(accounts) > 1:
                        reviewer_conflict_code = "MULTIPLE_LOCAL_ACCOUNTS"
                    elif accounts and (accounts[0].is_deleted or accounts[0].status != "ACTIVE"):
                        reviewer_conflict_code = "MANAGER_ACCOUNT_INACTIVE"
                    elif accounts and any(
                        code not in _ALLOWED_REVIEWER_ROLES
                        for code in role_codes_by_user.get(accounts[0].id, ())
                    ):
                        reviewer_conflict_code = "MANAGER_ACCOUNT_PRIVILEGED"
                    elif accounts and accounts[0].dingtalk_user_id_hash not in (
                        None,
                        provider_hash,
                    ):
                        reviewer_conflict_code = "MANAGER_IDENTITY_CONFLICT"
                    elif accounts and accounts[0].dingtalk_user_id not in (
                        None,
                        selected_remote.user_id,
                    ):
                        reviewer_conflict_code = "MANAGER_IDENTITY_CONFLICT"
                    else:
                        expected_account_id = accounts[0].id if accounts else None
                        account_owners = set(account_ids_by_hash.get(provider_hash, []))
                        employee_owners = set(employee_ids_by_hash.get(provider_hash, []))
                        if expected_account_id is not None:
                            account_owners.discard(expected_account_id)
                        employee_owners.discard(selected_employee.id)
                        if account_owners or employee_owners:
                            reviewer_conflict_code = "MANAGER_IDENTITY_CONFLICT"
                        else:
                            reviewer_action = "ASSIGN"
                            status = DingTalkOrgSyncItemStatus.READY
            add_reviewer_row(
                row_key=f"REVIEWER:REMOTE:{remote_store.department_id}:{review_department.value}",
                remote_store=remote_store,
                store=proposed_store,
                department=review_department,
                action=reviewer_action,
                method=method,
                status=status,
                conflict_code=reviewer_conflict_code,
                selected_remote=selected_remote,
                selected_employee=selected_employee,
                remote_name=remote_store.name,
                remote_path=store_row.remote_department_path,
            )

    drafts_by_identity: dict[tuple[str, Department], list[_ReviewerDraft]] = defaultdict(list)
    for draft in reviewer_drafts:
        if draft.row.remote_user_id_hash is not None and draft.row.department is not None:
            drafts_by_identity[(draft.row.remote_user_id_hash, draft.row.department)].append(draft)
    for drafts in drafts_by_identity.values():
        remote_store_ids_for_user = {draft.row.remote_department_id for draft in drafts}
        if len(remote_store_ids_for_user) > 1:
            for draft in drafts:
                draft.row.status = DingTalkOrgSyncItemStatus.CONFLICT
                draft.row.action = DingTalkOrgSyncAction.NO_CHANGE
                draft.row.change_fields = []
                draft.row.conflict_code = "MANAGER_ASSIGNED_MULTIPLE_STORES"
    return tuple(reviewer_drafts)


def _find_reusable_scheduled_batch(
    session: Session,
    *,
    root_config_hash: str,
    snapshot_hash: str,
    local_baseline_hash: str,
    current_time: datetime,
    for_update: bool = False,
) -> DingTalkOrgSyncBatch | None:
    statement = (
        select(DingTalkOrgSyncBatch)
        .where(
            DingTalkOrgSyncBatch.trigger == DingTalkOrgSyncTrigger.SCHEDULED,
            DingTalkOrgSyncBatch.status == DingTalkOrgSyncBatchStatus.PREVIEWED,
            DingTalkOrgSyncBatch.root_config_hash == root_config_hash,
            DingTalkOrgSyncBatch.snapshot_hash == snapshot_hash,
            DingTalkOrgSyncBatch.local_baseline_hash == local_baseline_hash,
            DingTalkOrgSyncBatch.expires_at > current_time,
        )
        .order_by(DingTalkOrgSyncBatch.id.desc())
        .limit(1)
    )
    if for_update:
        statement = statement.with_for_update()
    return session.scalars(statement).one_or_none()


def _load_preview_items(
    session: Session,
    batch_id: int,
    *,
    for_update: bool = False,
) -> list[DingTalkOrgSyncItem]:
    statement = (
        select(DingTalkOrgSyncItem)
        .where(DingTalkOrgSyncItem.batch_id == batch_id)
        .order_by(DingTalkOrgSyncItem.id)
    )
    if for_update:
        statement = statement.with_for_update()
    return list(session.scalars(statement).all())


def _staged_reviewer_metadata(
    state: _LocalState,
    snapshot: DingTalkOrganizationSnapshot,
    *,
    encryption_key: str,
    items: list[DingTalkOrgSyncItem],
) -> dict[str, _ReviewerDraft]:
    """Rebuild display-only reviewer fields for a reusable scheduled preview.

    The staged rows deliberately retain no provider names.  A scheduled cache
    hit still has the identical snapshot and local baseline, so derive the
    presentation fields from those inputs without entering either planner.
    """

    remote_names_by_hash: dict[str, list[str]] = defaultdict(list)
    for remote_user in snapshot.users:
        if remote_user.active:
            remote_names_by_hash[
                blind_index_dingtalk_user_id(remote_user.user_id, key=encryption_key)
            ].append(remote_user.name)

    return _stored_reviewer_metadata(state, items, remote_names_by_hash=remote_names_by_hash)


def _stored_reviewer_metadata(
    state: _LocalState,
    items: list[DingTalkOrgSyncItem],
    *,
    remote_names_by_hash: dict[str, list[str]] | None = None,
) -> dict[str, _ReviewerDraft]:
    """Build safe reviewer display fields from persisted state only.

    Provider names are available only when a caller supplies a just-read snapshot.
    Latest-status reads intentionally omit them rather than touching the provider.
    """

    (
        _org_units_by_id,
        employees_by_id,
        _accounts_by_employee,
        users_by_id,
        _role_codes_by_user,
        scope_users_by_pair,
    ) = _state_indexes(state)
    metadata: dict[str, _ReviewerDraft] = {}
    for item in items:
        if item.kind != DingTalkOrgSyncItemKind.REVIEWER:
            continue
        department = _required_reviewer_department(item)
        scope_user_ids = (
            scope_users_by_pair.get((item.proposed_org_unit_id, department), ())
            if item.proposed_org_unit_id is not None
            else ()
        )
        remote_names = (
            remote_names_by_hash.get(item.remote_user_id_hash, [])
            if remote_names_by_hash is not None and item.remote_user_id_hash is not None
            else []
        )
        metadata[item.row_key] = _ReviewerDraft(
            row=item,
            dingtalk_name=remote_names[0] if len(remote_names) == 1 else None,
            current_reviewer_name=_current_reviewer_name(scope_user_ids, users_by_id),
            proposed_employee_name=(
                employee.name
                if (employee := _optional_get(employees_by_id, item.proposed_employee_id))
                is not None
                else None
            ),
        )
    return metadata


def _reuse_scheduled_preview(
    session: Session,
    batch: DingTalkOrgSyncBatch,
    *,
    state: _LocalState,
    current_time: datetime,
    snapshot: DingTalkOrganizationSnapshot,
    encryption_key: str,
) -> OrganizationPreview:
    items = _load_preview_items(session, batch.id, for_update=True)
    batch.last_checked_at = current_time
    session.flush()
    result = _organization_preview_result(
        batch,
        items,
        state=state,
        reviewer_metadata=_staged_reviewer_metadata(
            state,
            snapshot,
            encryption_key=encryption_key,
            items=items,
        ),
    )
    session.commit()
    return result


def _persist_organization_preview(
    session: Session,
    *,
    state: _LocalState,
    node_plan: _NodePlan,
    reviewer_drafts: tuple[_ReviewerDraft, ...],
    complete_local_baseline: str,
    snapshot_hash: str,
    root_config_hash: str,
    trigger: DingTalkOrgSyncTrigger,
    actor: tuple[int, str] | None,
    current_time: datetime,
) -> OrganizationPreview:
    reviewer_rows = [draft.row for draft in reviewer_drafts]
    all_rows = [*node_plan.rows, *reviewer_rows]
    _wrap_baselines(all_rows, complete_local_baseline)
    if trigger == DingTalkOrgSyncTrigger.MANUAL:
        previous_batches = list(
            session.scalars(
                select(DingTalkOrgSyncBatch)
                .where(
                    DingTalkOrgSyncBatch.status == DingTalkOrgSyncBatchStatus.PREVIEWED,
                    DingTalkOrgSyncBatch.root_config_hash == root_config_hash,
                )
                .order_by(DingTalkOrgSyncBatch.id)
                .with_for_update()
            ).all()
        )
        previous_ids = [previous.id for previous in previous_batches]
        if previous_ids:
            previous_items = list(
                session.scalars(
                    select(DingTalkOrgSyncItem)
                    .where(DingTalkOrgSyncItem.batch_id.in_(previous_ids))
                    .order_by(DingTalkOrgSyncItem.batch_id, DingTalkOrgSyncItem.id)
                    .with_for_update()
                ).all()
            )
            for previous in previous_batches:
                previous.status = DingTalkOrgSyncBatchStatus.STALE
            _clear_staged_hashes(previous_items)

    batch = DingTalkOrgSyncBatch(
        status=DingTalkOrgSyncBatchStatus.PREVIEWED,
        created_at=current_time,
        updated_at=current_time,
        snapshot_hash=snapshot_hash,
        root_config_hash=root_config_hash,
        local_baseline_hash=complete_local_baseline,
        trigger=trigger,
        expires_at=current_time + _PREVIEW_TTL,
        requested_by_user_id=actor[0] if actor is not None else None,
        last_checked_at=current_time,
        remote_region_count=len(node_plan.classified.regions),
        local_region_count=sum(
            node.type == OrgType.REGION for node in node_plan.local_authority_nodes
        ),
        ready_region_count=sum(
            row.status == DingTalkOrgSyncItemStatus.READY for row in node_plan.region_rows
        ),
        region_conflict_count=sum(
            row.status == DingTalkOrgSyncItemStatus.CONFLICT for row in node_plan.region_rows
        ),
        remote_store_count=len(node_plan.classified.stores),
        local_store_count=sum(
            node.type == OrgType.STORE for node in node_plan.local_authority_nodes
        ),
        ready_store_count=sum(
            row.status == DingTalkOrgSyncItemStatus.READY for row in node_plan.store_rows
        ),
        store_conflict_count=sum(
            row.status == DingTalkOrgSyncItemStatus.CONFLICT for row in node_plan.store_rows
        ),
        ready_reviewer_count=sum(
            row.status == DingTalkOrgSyncItemStatus.READY for row in reviewer_rows
        ),
        reviewer_conflict_count=sum(
            row.status == DingTalkOrgSyncItemStatus.CONFLICT for row in reviewer_rows
        ),
        warning_count=(
            len(node_plan.classified.warning_department_ids)
            + sum(row.match_method == "REMOVE_MISSING_MANAGER" for row in reviewer_rows)
        ),
    )
    session.add(batch)
    session.flush()
    for item in all_rows:
        item.batch_id = batch.id
        session.add(item)
    session.flush()
    audit.record(
        session,
        action="dingtalk.organization.preview",
        actor=actor,
        target_type="dingtalk_org_sync_batch",
        target_id=batch.id,
        detail={
            "remote_region_count": batch.remote_region_count,
            "local_region_count": batch.local_region_count,
            "ready_region_count": batch.ready_region_count,
            "region_conflict_count": batch.region_conflict_count,
            "remote_store_count": batch.remote_store_count,
            "local_store_count": batch.local_store_count,
            "ready_store_count": batch.ready_store_count,
            "store_conflict_count": batch.store_conflict_count,
            "ready_reviewer_count": batch.ready_reviewer_count,
            "reviewer_conflict_count": batch.reviewer_conflict_count,
            "warning_count": batch.warning_count,
        },
    )
    result = _organization_preview_result(
        batch,
        all_rows,
        state=state,
        reviewer_metadata={draft.row.row_key: draft for draft in reviewer_drafts},
    )
    session.commit()
    return result


def preview_organization_sync(
    session: Session,
    snapshot: DingTalkOrganizationSnapshot,
    *,
    encryption_key: str,
    tenant_id: str,
    actor: tuple[int, str] | None,
    root_mappings: tuple[tuple[int, str], ...],
    trigger: DingTalkOrgSyncTrigger = DingTalkOrgSyncTrigger.MANUAL,
    now: datetime | None = None,
    dining_manager_titles: frozenset[str] = frozenset({"店长"}),
    kitchen_manager_titles: frozenset[str] = frozenset({"厨房经理"}),
) -> OrganizationPreview:
    """Persist a point-in-time preview without modifying formal organization data."""

    current_time = now or datetime.now(UTC)
    if not tenant_id.strip():
        raise DingTalkOrganizationSyncError(
            "TENANT_NOT_CONFIGURED",
            "DingTalk CorpId is required before organization preview",
        )
    if actor is None and trigger != DingTalkOrgSyncTrigger.SCHEDULED:
        raise DingTalkOrganizationSyncError(
            "ORG_ROOT_CONFIG_INVALID", "A manual organization preview requires an actor"
        )

    state = _load_local_state(session)
    org_units_by_id = {organization.id: organization for organization in state.org_units}
    resolved_root_mappings = _resolve_root_mappings(state, root_mappings)
    authority = _build_authority_index(state, resolved_root_mappings, org_units_by_id)
    root_config_hash = _root_config_hash(resolved_root_mappings)
    snapshot_hash = _snapshot_hash(snapshot, encryption_key=encryption_key)
    trusted_bindings = _validated_reviewer_identity_bindings(
        state,
        encryption_key=encryption_key,
        tenant_id=tenant_id,
    )
    complete_local_baseline = _complete_local_baseline(
        state,
        trusted_bindings=trusted_bindings,
    )

    reusable = None
    if trigger == DingTalkOrgSyncTrigger.SCHEDULED:
        reusable = _find_reusable_scheduled_batch(
            session,
            root_config_hash=root_config_hash,
            snapshot_hash=snapshot_hash,
            local_baseline_hash=complete_local_baseline,
            current_time=current_time,
        )
    if reusable is not None:
        take_organization_sync_lock(session)
        session.expire_all()
        locked_state = _load_local_state(session)
        locked_roots = _resolve_root_mappings(locked_state, root_mappings)
        locked_root_hash = _root_config_hash(locked_roots)
        locked_trusted_bindings = _validated_reviewer_identity_bindings(
            locked_state,
            encryption_key=encryption_key,
            tenant_id=tenant_id,
        )
        locked_local_hash = _complete_local_baseline(
            locked_state,
            trusted_bindings=locked_trusted_bindings,
        )
        if locked_root_hash != root_config_hash or locked_local_hash != complete_local_baseline:
            session.rollback()
            raise DingTalkOrganizationSyncError(
                "ORG_LOCAL_BASELINE_CHANGED",
                "Local organization data changed during preview; retry",
            )
        locked_reusable = _find_reusable_scheduled_batch(
            session,
            root_config_hash=locked_root_hash,
            snapshot_hash=snapshot_hash,
            local_baseline_hash=locked_local_hash,
            current_time=current_time,
            for_update=True,
        )
        if locked_reusable is None:
            session.rollback()
            raise DingTalkOrganizationSyncError(
                "ORG_CONCURRENT_CHANGE",
                "Organization preview changed concurrently; retry",
            )
        return _reuse_scheduled_preview(
            session,
            locked_reusable,
            state=locked_state,
            current_time=current_time,
            snapshot=snapshot,
            encryption_key=encryption_key,
        )

    node_plan = _plan_organization_nodes(
        state,
        snapshot,
        authority=authority,
        org_units_by_id=org_units_by_id,
    )
    reviewer_drafts = _plan_organization_reviewers(
        state,
        snapshot,
        encryption_key=encryption_key,
        node_plan=node_plan,
        dining_manager_titles=dining_manager_titles,
        kitchen_manager_titles=kitchen_manager_titles,
        trusted_bindings=trusted_bindings,
    )

    take_organization_sync_lock(session)
    session.expire_all()
    locked_state = _load_local_state(session)
    locked_roots = _resolve_root_mappings(locked_state, root_mappings)
    locked_root_hash = _root_config_hash(locked_roots)
    locked_trusted_bindings = _validated_reviewer_identity_bindings(
        locked_state,
        encryption_key=encryption_key,
        tenant_id=tenant_id,
    )
    locked_local_hash = _complete_local_baseline(
        locked_state,
        trusted_bindings=locked_trusted_bindings,
    )
    if locked_root_hash != root_config_hash or locked_local_hash != complete_local_baseline:
        session.rollback()
        raise DingTalkOrganizationSyncError(
            "ORG_LOCAL_BASELINE_CHANGED",
            "Local organization data changed during preview; retry",
        )

    if trigger == DingTalkOrgSyncTrigger.SCHEDULED:
        locked_reusable = _find_reusable_scheduled_batch(
            session,
            root_config_hash=locked_root_hash,
            snapshot_hash=snapshot_hash,
            local_baseline_hash=locked_local_hash,
            current_time=current_time,
            for_update=True,
        )
        if locked_reusable is not None:
            return _reuse_scheduled_preview(
                session,
                locked_reusable,
                state=locked_state,
                current_time=current_time,
                snapshot=snapshot,
                encryption_key=encryption_key,
            )

    return _persist_organization_preview(
        session,
        state=locked_state,
        node_plan=node_plan,
        reviewer_drafts=reviewer_drafts,
        complete_local_baseline=locked_local_hash,
        snapshot_hash=snapshot_hash,
        root_config_hash=locked_root_hash,
        trigger=trigger,
        actor=actor,
        current_time=current_time,
    )


def _clear_staged_hashes(items: list[DingTalkOrgSyncItem]) -> None:
    for item in items:
        item.remote_user_id_hash = None


def _mark_batch_stale(
    session: Session,
    batch: DingTalkOrgSyncBatch,
    items: list[DingTalkOrgSyncItem],
    *,
    actor: tuple[int, str],
    error_code: str,
) -> None:
    batch.status = DingTalkOrgSyncBatchStatus.STALE
    _clear_staged_hashes(items)
    audit.record(
        session,
        action="dingtalk.organization.stale",
        result="FAIL",
        actor=actor,
        target_type="dingtalk_org_sync_batch",
        target_id=batch.id,
        detail={"error_code": error_code},
    )
    session.commit()


def _new_reviewer_username(employee_id: int, usernames: set[str]) -> str:
    base = f"dingtalk-reviewer-{employee_id}"
    candidate = base
    suffix = 1
    while candidate in usernames:
        suffix += 1
        candidate = f"{base}-{suffix}"
    usernames.add(candidate)
    return candidate


def _lock_batch_and_items(
    session: Session, public_id: str
) -> tuple[DingTalkOrgSyncBatch | None, list[DingTalkOrgSyncItem]]:
    batch = session.scalars(
        select(DingTalkOrgSyncBatch)
        .where(DingTalkOrgSyncBatch.public_id == public_id)
        .with_for_update()
    ).one_or_none()
    if batch is None:
        return None, []
    items = list(
        session.scalars(
            select(DingTalkOrgSyncItem)
            .where(DingTalkOrgSyncItem.batch_id == batch.id)
            .order_by(DingTalkOrgSyncItem.id)
            .with_for_update()
        ).all()
    )
    return batch, items


def apply_organization_sync(
    session: Session,
    public_id: str,
    *,
    fresh_snapshot: DingTalkOrganizationSnapshot,
    encryption_key: str,
    tenant_id: str,
    actor: tuple[int, str],
    root_mappings: tuple[tuple[int, str], ...],
    now: datetime | None = None,
) -> OrganizationApplyResult:
    """Apply an unchanged preview after a fresh provider read and full baseline check."""

    if not tenant_id.strip():
        raise DingTalkOrganizationSyncError(
            "TENANT_NOT_CONFIGURED",
            "DingTalk CorpId is required before organization confirmation",
        )
    take_organization_sync_lock(session)
    current_time = now or datetime.now(UTC)
    batch, items = _lock_batch_and_items(session, public_id)
    if batch is None:
        session.rollback()
        raise DingTalkOrganizationSyncError("BATCH_NOT_FOUND", "Organization preview not found")
    unresolved = sum(item.status == DingTalkOrgSyncItemStatus.CONFLICT for item in items)
    if batch.status == DingTalkOrgSyncBatchStatus.APPLIED:
        result = OrganizationApplyResult(
            applied_regions=batch.ready_region_count,
            applied_stores=batch.ready_store_count,
            applied_reviewers=batch.ready_reviewer_count,
            unresolved=unresolved,
            already_applied=True,
        )
        session.rollback()
        return result
    if batch.status != DingTalkOrgSyncBatchStatus.PREVIEWED:
        session.rollback()
        raise DingTalkOrganizationSyncError(
            "BATCH_STALE", "Organization preview is stale; preview again"
        )
    if any(item.status == DingTalkOrgSyncItemStatus.CONFLICT for item in items):
        session.rollback()
        raise DingTalkOrganizationSyncError(
            "ORG_PREVIEW_HAS_CONFLICTS",
            "Organization conflicts must be resolved before confirmation",
        )
    if _as_utc(batch.expires_at) <= current_time:
        _mark_batch_stale(session, batch, items, actor=actor, error_code="PREVIEW_EXPIRED")
        raise DingTalkOrganizationSyncError(
            "PREVIEW_EXPIRED", "Organization preview expired; preview again"
        )
    _lock_org_unit_table_against_phantoms(session)
    state = _load_local_state(session, for_update=True)
    try:
        current_root_mappings = _resolve_root_mappings(state, root_mappings)
    except DingTalkOrganizationSyncError:
        _mark_batch_stale(
            session,
            batch,
            items,
            actor=actor,
            error_code="ORG_ROOT_CONFIG_CHANGED",
        )
        raise DingTalkOrganizationSyncError(
            "ORG_ROOT_CONFIG_CHANGED",
            "Organization root configuration changed; preview again",
        ) from None
    if _root_config_hash(current_root_mappings) != batch.root_config_hash:
        _mark_batch_stale(
            session,
            batch,
            items,
            actor=actor,
            error_code="ORG_ROOT_CONFIG_CHANGED",
        )
        raise DingTalkOrganizationSyncError(
            "ORG_ROOT_CONFIG_CHANGED",
            "Organization root configuration changed; preview again",
        )
    trusted_bindings = _validated_reviewer_identity_bindings(
        state,
        encryption_key=encryption_key,
        tenant_id=tenant_id,
    )
    complete_local_baseline = _complete_local_baseline(
        state,
        trusted_bindings=trusted_bindings,
    )
    if complete_local_baseline != batch.local_baseline_hash:
        _mark_batch_stale(session, batch, items, actor=actor, error_code="CONCURRENT_CHANGE")
        raise DingTalkOrganizationSyncError(
            "CONCURRENT_CHANGE",
            "Organization data changed during confirmation; preview again",
        )

    if _snapshot_hash(fresh_snapshot, encryption_key=encryption_key) != batch.snapshot_hash:
        _mark_batch_stale(
            session, batch, items, actor=actor, error_code="PROVIDER_SNAPSHOT_CHANGED"
        )
        raise DingTalkOrganizationSyncError(
            "PROVIDER_SNAPSHOT_CHANGED",
            "DingTalk organization changed; preview again",
        )
    (
        org_units_by_id,
        employees_by_id,
        accounts_by_employee,
        _users_by_id,
        role_codes_by_user,
        scope_users_by_pair,
    ) = _state_indexes(state)
    org_units_by_code = {org.code: org for org in state.org_units}
    original_scope_users_by_pair = dict(scope_users_by_pair)

    stale = False
    for item in items:
        if item.kind in (DingTalkOrgSyncItemKind.REGION, DingTalkOrgSyncItemKind.STORE):
            if item.action == DingTalkOrgSyncAction.CREATE:
                parent = _optional_get(org_units_by_id, item.proposed_parent_org_unit_id)
                expected = _create_store_baseline(
                    parent=parent,
                    code=_dingtalk_org_code(item.kind, item.remote_department_id),  # type: ignore[arg-type]
                    name=item.remote_department_name,
                )
            else:
                expected = _resolved_store_baseline(
                    _optional_get(org_units_by_id, item.proposed_org_unit_id)
                )
        else:
            department = _required_reviewer_department(item)
            baseline_store = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            baseline_employee = _optional_get(employees_by_id, item.proposed_employee_id)
            accounts = (
                accounts_by_employee.get(baseline_employee.id, []) if baseline_employee else []
            )
            scope_user_ids = (
                scope_users_by_pair.get((baseline_store.id, department), ())
                if baseline_store
                else ()
            )
            expected = _reviewer_baseline(
                store=baseline_store,
                department=department,
                employee=baseline_employee,
                accounts=accounts,
                scope_user_ids=scope_user_ids,
                role_codes_by_user=role_codes_by_user,
            )
        expected = _fingerprint(expected, complete_local_baseline)
        if expected != item.baseline_fingerprint:
            stale = True
            break
    if stale:
        _mark_batch_stale(session, batch, items, actor=actor, error_code="CONCURRENT_CHANGE")
        raise DingTalkOrganizationSyncError(
            "CONCURRENT_CHANGE",
            "Organization data changed during confirmation; preview again",
        )

    fresh_users_by_hash: dict[str, list[DingTalkOrganizationUser]] = defaultdict(list)
    for user in fresh_snapshot.users:
        fresh_users_by_hash[blind_index_dingtalk_user_id(user.user_id, key=encryption_key)].append(
            user
        )

    ready_region_items = [
        item
        for item in items
        if item.kind == DingTalkOrgSyncItemKind.REGION
        and item.status == DingTalkOrgSyncItemStatus.READY
    ]
    ready_store_items = [
        item
        for item in items
        if item.kind == DingTalkOrgSyncItemKind.STORE
        and item.status == DingTalkOrgSyncItemStatus.READY
    ]
    ready_reviewer_items = [
        item
        for item in items
        if item.kind == DingTalkOrgSyncItemKind.REVIEWER
        and item.status == DingTalkOrgSyncItemStatus.READY
    ]
    ready_node_items = [*ready_region_items, *ready_store_items]
    manager_role = next((role for role in state.roles if role.code == "STORE_MANAGER"), None)
    if manager_role is None:
        session.rollback()
        raise DingTalkOrganizationSyncError(
            "ROLE_NOT_CONFIGURED", "Store manager role is not configured"
        )

    # Validate every identity one last time before the first formal mutation.
    for item in ready_reviewer_items:
        if item.action == DingTalkOrgSyncAction.REMOVE_SCOPE:
            if item.proposed_employee_id is not None or item.remote_user_id_hash is not None:
                session.rollback()
                raise DingTalkOrganizationSyncError(
                    "INVALID_PREVIEW", "Organization preview is invalid; preview again"
                )
            continue
        if (
            item.action != DingTalkOrgSyncAction.ASSIGN_SCOPE
            or item.match_method not in {"STABLE_ID", "JOB_NUMBER"}
            or item.proposed_employee_id is None
            or item.remote_user_id_hash is None
            or len(fresh_users_by_hash.get(item.remote_user_id_hash, [])) != 1
        ):
            session.rollback()
            raise DingTalkOrganizationSyncError(
                "INVALID_PREVIEW", "Organization preview is invalid; preview again"
            )
        employee = employees_by_id.get(item.proposed_employee_id)
        if employee is None:
            session.rollback()
            raise DingTalkOrganizationSyncError(
                "CONCURRENT_CHANGE",
                "Organization data changed during confirmation; preview again",
            )
        accounts = accounts_by_employee.get(employee.id, [])
        fresh_provider_user = fresh_users_by_hash[item.remote_user_id_hash][0]
        if (
            len(accounts) > 1
            or (
                accounts
                and any(
                    role not in _ALLOWED_REVIEWER_ROLES
                    for role in role_codes_by_user.get(accounts[0].id, ())
                )
            )
            or (
                accounts
                and accounts[0].dingtalk_user_id
                not in (
                    None,
                    fresh_provider_user.user_id,
                )
            )
        ):
            session.rollback()
            raise DingTalkOrganizationSyncError(
                "CONCURRENT_CHANGE",
                "Organization data changed during confirmation; preview again",
            )
        account_id = accounts[0].id if accounts else None
        for account in state.users:
            if (
                account.id != account_id
                and account.dingtalk_user_id_hash == item.remote_user_id_hash
            ):
                session.rollback()
                raise DingTalkOrganizationSyncError(
                    "CONCURRENT_CHANGE",
                    "Organization data changed during confirmation; preview again",
                )
        for other_employee in state.employees:
            if (
                other_employee.id != employee.id
                and other_employee.dingtalk_user_id_hash == item.remote_user_id_hash
            ):
                session.rollback()
                raise DingTalkOrganizationSyncError(
                    "CONCURRENT_CHANGE",
                    "Organization data changed during confirmation; preview again",
                )

    region_changes: list[dict[str, object]] = []
    store_changes: list[dict[str, object]] = []
    reviewer_changes: list[dict[str, object]] = []
    nodes_by_remote_id: dict[int, OrgUnit] = {}
    assigned_roles = {(assignment.user_id, assignment.role_id) for assignment in state.user_roles}
    usernames = {user.username for user in state.users}
    remote_parent_by_id = {
        department.department_id: department.parent_id for department in fresh_snapshot.departments
    }
    locked_parent_by_id = {
        organization.id: organization.parent_id for organization in state.org_units
    }

    def remote_item_depth(item: DingTalkOrgSyncItem) -> int:
        return item.remote_department_path.count(" / ") + 1

    def resolve_node_parent(item: DingTalkOrgSyncItem) -> OrgUnit | None:
        parent = _optional_get(org_units_by_id, item.proposed_parent_org_unit_id)
        if parent is not None:
            return parent
        if item.remote_department_id is None:
            return None
        remote_parent_id = remote_parent_by_id.get(item.remote_department_id)
        return nodes_by_remote_id.get(remote_parent_id) if remote_parent_id is not None else None

    try:
        deactivate_items = [
            item for item in ready_node_items if item.action == DingTalkOrgSyncAction.DEACTIVATE
        ]
        if any(item.proposed_org_unit_id is None for item in deactivate_items):
            raise _ConcurrentChange("deactivation target baseline changed")
        deactivation_depths = _locked_local_depths(
            locked_parent_by_id,
            {
                item.proposed_org_unit_id
                for item in deactivate_items
                if item.proposed_org_unit_id is not None
            },
        )
        active_node_items = sorted(
            (item for item in ready_node_items if item.action != DingTalkOrgSyncAction.DEACTIVATE),
            key=lambda item: (remote_item_depth(item), item.remote_department_id or 0, item.id),
        )
        for item in active_node_items:
            action = item.action
            remote_department_id = item.remote_department_id
            if remote_department_id is None:
                raise RuntimeError("ready organization item is missing its remote department")
            before_store = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            before_state = (
                {
                    "org_unit_id": before_store.id,
                    "parent_org_unit_id": before_store.parent_id,
                    "status": before_store.status,
                }
                if before_store is not None
                else None
            )
            if action == DingTalkOrgSyncAction.CREATE:
                parent = resolve_node_parent(item)
                code = _dingtalk_org_code(item.kind, remote_department_id)
                if parent is None or code in org_units_by_code:
                    raise _ConcurrentChange("create organization baseline changed")
                applied_store = OrgUnit(
                    code=code,
                    name=item.remote_department_name,
                    type=OrgType(item.kind.value),
                    parent_id=parent.id,
                    dingtalk_dept_id=remote_department_id,
                    city=None,
                    status="ACTIVE",
                )
                session.add(applied_store)
                session.flush()
                item.proposed_org_unit_id = applied_store.id
                org_units_by_id[applied_store.id] = applied_store
                org_units_by_code[applied_store.code] = applied_store
            else:
                if before_store is None:
                    raise _ConcurrentChange("organization baseline changed")
                applied_store = before_store
                if "dingtalk_dept_id" in item.change_fields:
                    applied_store.dingtalk_dept_id = remote_department_id
                if action == DingTalkOrgSyncAction.ACTIVATE:
                    applied_store.status = "ACTIVE"
                if action in {DingTalkOrgSyncAction.ACTIVATE, DingTalkOrgSyncAction.UPDATE}:
                    if "name" in item.change_fields:
                        applied_store.name = item.remote_department_name
                    if "parent_id" in item.change_fields:
                        parent = resolve_node_parent(item)
                        if parent is None:
                            raise _ConcurrentChange("organization parent baseline changed")
                        applied_store.parent_id = parent.id
            nodes_by_remote_id[remote_department_id] = applied_store
            item.status = DingTalkOrgSyncItemStatus.APPLIED
            changes = (
                region_changes if item.kind == DingTalkOrgSyncItemKind.REGION else store_changes
            )
            changes.append(
                {
                    "item_id": item.id,
                    "action": action.value,
                    "before": before_state,
                    "after": {
                        "org_unit_id": applied_store.id,
                        "parent_org_unit_id": applied_store.parent_id,
                        "status": applied_store.status,
                    },
                }
            )

        # Remove every affected authorization edge and revoke every displaced
        # account before any node becomes historical.
        children_by_parent: dict[int, list[int]] = defaultdict(list)
        for organization_id, parent_id in locked_parent_by_id.items():
            if parent_id is not None:
                children_by_parent[parent_id].append(organization_id)

        deactivated_store_ids: set[int] = set()
        for item in deactivate_items:
            deactivate_root = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            if deactivate_root is None:
                raise _ConcurrentChange("organization baseline changed")
            stack = [deactivate_root.id]
            visited_descendants: set[int] = set()
            while stack:
                organization_id = stack.pop()
                if organization_id in visited_descendants:
                    raise _ConcurrentChange("organization hierarchy contains a cycle")
                visited_descendants.add(organization_id)
                descendant = org_units_by_id[organization_id]
                if descendant.type == OrgType.STORE:
                    deactivated_store_ids.add(descendant.id)
                stack.extend(children_by_parent.get(organization_id, ()))

        affected_scope_pairs: set[tuple[int, Department]] = set()
        for item in ready_reviewer_items:
            department = _required_reviewer_department(item)
            target = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            if target is None and item.remote_department_id is not None:
                target = nodes_by_remote_id.get(item.remote_department_id)
            if target is None:
                raise _ConcurrentChange("reviewer organization baseline changed")
            item.proposed_org_unit_id = target.id
            affected_scope_pairs.add((target.id, department))
        affected_scope_pairs.update(
            (scope.org_unit_id, scope.department)
            for scope in state.review_scopes
            if scope.org_unit_id in deactivated_store_ids
        )
        displaced_user_ids = {
            scope.user_id
            for scope in state.review_scopes
            if (scope.org_unit_id, scope.department) in affected_scope_pairs
        }
        invalidate_applied_reviewer_proofs(session, scopes=affected_scope_pairs)
        for org_unit_id, department in sorted(
            affected_scope_pairs, key=lambda value: (value[0], value[1].value)
        ):
            session.execute(
                delete(UserReviewScope).where(
                    UserReviewScope.org_unit_id == org_unit_id,
                    UserReviewScope.department == department,
                )
            )
            scope_users_by_pair[(org_unit_id, department)] = ()
        for user_id in sorted(displaced_user_ids):
            revoke_all_for_user(session, user_id)

        for item in sorted(
            deactivate_items,
            key=lambda item: (
                -deactivation_depths[item.proposed_org_unit_id],  # type: ignore[index]
                item.proposed_org_unit_id or 0,
                item.id,
            ),
        ):
            node_to_deactivate = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            if node_to_deactivate is None:
                raise _ConcurrentChange("organization baseline changed")
            before_state = {
                "org_unit_id": node_to_deactivate.id,
                "parent_org_unit_id": node_to_deactivate.parent_id,
                "status": node_to_deactivate.status,
            }
            node_to_deactivate.status = "HISTORICAL"
            item.status = DingTalkOrgSyncItemStatus.APPLIED
            changes = (
                region_changes if item.kind == DingTalkOrgSyncItemKind.REGION else store_changes
            )
            changes.append(
                {
                    "item_id": item.id,
                    "action": item.action.value,
                    "before": before_state,
                    "after": {
                        "org_unit_id": node_to_deactivate.id,
                        "parent_org_unit_id": node_to_deactivate.parent_id,
                        "status": node_to_deactivate.status,
                    },
                }
            )

        for item in ready_reviewer_items:
            action = item.action
            department = _required_reviewer_department(item)
            target_store = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            if target_store is None and item.remote_department_id is not None:
                target_store = nodes_by_remote_id.get(item.remote_department_id)
            if target_store is None:
                raise RuntimeError("ready reviewer item lost its organization")
            # CREATE proposals do not have an internal organization id until
            # the store row above is flushed.  Persist the resolved id so
            # freshness checks cover its reviewer assignments too.
            item.proposed_org_unit_id = target_store.id
            before_user_ids = tuple(
                original_scope_users_by_pair.get((target_store.id, department), ())
            )
            after_user_ids: tuple[int, ...] = ()
            employee_id: int | None = None
            if action == DingTalkOrgSyncAction.ASSIGN_SCOPE:
                employee = employees_by_id[item.proposed_employee_id]  # type: ignore[index]
                employee_id = employee.id
                accounts = accounts_by_employee.get(employee.id, [])
                reviewer_account: User | None = accounts[0] if accounts else None
                if reviewer_account is None:
                    reviewer_account = User(
                        username=_new_reviewer_username(employee.id, usernames),
                        password_hash=hash_password(secrets.token_urlsafe(48)),
                        employee_id=employee.id,
                        status="ACTIVE",
                        login_enabled=False,
                        created_by=actor[0],
                    )
                    session.add(reviewer_account)
                    session.flush()
                    accounts_by_employee[employee.id] = [reviewer_account]
                else:
                    reviewer_account.login_enabled = False
                    revoke_all_for_user(session, reviewer_account.id)
                provider_user = fresh_users_by_hash[item.remote_user_id_hash][0]  # type: ignore[index]
                reviewer_account.dingtalk_user_id = provider_user.user_id
                reviewer_account.dingtalk_user_id_hash = item.remote_user_id_hash
                employee.dingtalk_user_id_hash = item.remote_user_id_hash
                if (reviewer_account.id, manager_role.id) not in assigned_roles:
                    session.add(UserRole(user_id=reviewer_account.id, role_id=manager_role.id))
                    assigned_roles.add((reviewer_account.id, manager_role.id))
                session.add(
                    UserReviewScope(
                        user_id=reviewer_account.id,
                        org_unit_id=target_store.id,
                        department=department,
                    )
                )
                after_user_ids = (reviewer_account.id,)
                item.applied_identity_proof = dingtalk_organization_identity_proof(
                    item.remote_user_id_hash,  # type: ignore[arg-type]
                    key=encryption_key,
                    tenant_id=tenant_id,
                    batch_public_id=batch.public_id,
                    snapshot_hash=batch.snapshot_hash,
                    remote_department_id=item.remote_department_id,  # type: ignore[arg-type]
                    org_unit_id=target_store.id,
                    department=department.value,
                    employee_id=employee.id,
                )
            else:
                item.applied_identity_proof = None
            item.status = DingTalkOrgSyncItemStatus.APPLIED
            reviewer_changes.append(
                {
                    "item_id": item.id,
                    "action": action.value,
                    "org_unit_id": target_store.id,
                    "department": department.value,
                    "employee_id": employee_id,
                    "before_user_ids": before_user_ids,
                    "after_user_ids": after_user_ids,
                }
            )

        _clear_staged_hashes(items)
        batch.status = DingTalkOrgSyncBatchStatus.APPLIED
        batch.applied_by_user_id = actor[0]
        batch.applied_at = current_time
        session.flush()
        audit.record(
            session,
            action="dingtalk.organization.apply",
            actor=actor,
            target_type="dingtalk_org_sync_batch",
            target_id=batch.id,
            detail={
                "applied_region_count": batch.ready_region_count,
                "applied_store_count": batch.ready_store_count,
                "applied_reviewer_count": batch.ready_reviewer_count,
                "unresolved_count": unresolved,
                "region_changes": region_changes,
                "store_changes": store_changes,
                "reviewer_changes": reviewer_changes,
            },
        )
        session.commit()
    except (IntegrityError, _ConcurrentChange):
        session.rollback()
        take_organization_sync_lock(session)
        failed_batch, failed_items = _lock_batch_and_items(session, public_id)
        if failed_batch is not None and failed_batch.status == DingTalkOrgSyncBatchStatus.PREVIEWED:
            _mark_batch_stale(
                session,
                failed_batch,
                failed_items,
                actor=actor,
                error_code="CONCURRENT_CHANGE",
            )
        raise DingTalkOrganizationSyncError(
            "CONCURRENT_CHANGE",
            "Organization data changed during confirmation; preview again",
        ) from None
    except Exception:
        session.rollback()
        raise

    return OrganizationApplyResult(
        applied_regions=batch.ready_region_count,
        applied_stores=batch.ready_store_count,
        applied_reviewers=batch.ready_reviewer_count,
        unresolved=unresolved,
        already_applied=False,
    )
