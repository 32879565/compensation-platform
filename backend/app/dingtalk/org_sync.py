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
    DingTalkDirectoryUser,
    DingTalkOrganizationSnapshot,
    DingTalkOrganizationUser,
)
from app.dingtalk.org_rules import manager_department_for_title
from app.dingtalk.org_structure import (
    ClassifiedNode,
    OrganizationStructureError,
    classify_organization,
    normalize_org_name,
)
from app.dingtalk.read_sync import (
    LocalEmployeeIdentity,
    blind_index_dingtalk_user_id,
    dingtalk_organization_identity_proof,
    match_directory_users,
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
    applied_stores: int
    applied_reviewers: int
    unresolved: int
    already_applied: bool


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


@dataclass
class _ReviewerDraft:
    row: DingTalkOrgSyncItem
    dingtalk_name: str | None
    current_reviewer_name: str | None
    proposed_employee_name: str | None


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
    return " / ".join(reversed(parts))[:_MAX_PATH_LENGTH]


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
    if for_update:
        org_statement = org_statement.with_for_update()
        employee_statement = employee_statement.with_for_update()
        user_statement = user_statement.with_for_update()
        role_statement = role_statement.with_for_update()
        user_role_statement = user_role_statement.with_for_update()
        scope_statement = scope_statement.with_for_update()

    # Keep the formal-data lock order aligned with reviewer administration:
    # user -> organization -> employee -> RBAC -> review scope.  In
    # particular, locking the store row protects an empty (store, department)
    # scope from a concurrent insertion after its baseline was checked.
    return _LocalState(
        users=tuple(session.scalars(user_statement).all()),
        org_units=tuple(session.scalars(org_statement).all()),
        employees=tuple(session.scalars(employee_statement).all()),
        roles=tuple(session.scalars(role_statement).all()),
        user_roles=tuple(session.scalars(user_role_statement).all()),
        review_scopes=tuple(session.scalars(scope_statement).all()),
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


def _optional_get[T](mapping: dict[int, T], key: int | None) -> T | None:
    return mapping.get(key) if key is not None else None


def _state_indexes(state: _LocalState) -> tuple[
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


def _complete_local_baseline(state: _LocalState) -> str:
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
    )


def _wrap_baselines(items: list[DingTalkOrgSyncItem], complete_local_baseline: str) -> None:
    for item in items:
        item.baseline_fingerprint = _fingerprint(item.baseline_fingerprint, complete_local_baseline)


def _planned_baseline_hash(items: list[DingTalkOrgSyncItem]) -> str:
    return _fingerprint(
        "PLANNED_LOCAL_BASELINES",
        tuple(sorted((item.row_key, item.baseline_fingerprint) for item in items)),
    )


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


def _organization_matching_baseline(
    org_units: tuple[OrgUnit, ...] | list[OrgUnit],
) -> str:
    """Fingerprint every local field that can affect store or parent resolution."""

    return _fingerprint(
        "ORGANIZATION_MATCHING",
        tuple(
            (
                org.id,
                org.parent_id,
                org.type.value,
                _normalize_name(org.name),
                org.code,
                org.dingtalk_dept_id,
                org.status,
                org.is_deleted,
                org.updated_at,
            )
            for org in sorted(org_units, key=lambda candidate: candidate.id)
        ),
    )


def _resolved_store_baseline(
    store: OrgUnit | None,
    *,
    org_units: tuple[OrgUnit, ...] | list[OrgUnit],
) -> str:
    """Bind a resolved local store to the complete candidate set used to select it."""

    return _fingerprint(
        "RESOLVED_STORE",
        _store_baseline(store),
        _organization_matching_baseline(org_units),
    )


def _create_store_baseline(
    *,
    parent: OrgUnit | None,
    code: str,
    name: str,
    org_units: tuple[OrgUnit, ...] | list[OrgUnit],
) -> str:
    existing_code_ids = tuple(sorted(org.id for org in org_units if org.code == code))
    normalized_name = _normalize_name(name)
    existing_name_ids = tuple(
        sorted(
            org.id
            for org in org_units
            if org.type == OrgType.STORE
            and not org.is_deleted
            and _normalize_name(org.name) == normalized_name
        )
    )
    return _fingerprint(
        "CREATE",
        _store_baseline(parent),
        code,
        normalized_name,
        existing_code_ids,
        existing_name_ids,
        _organization_matching_baseline(org_units),
    )


def _lock_org_unit_table_against_phantoms(session: Session) -> None:
    if session.get_bind().dialect.name == "postgresql":
        session.execute(text("LOCK TABLE org_unit IN SHARE ROW EXCLUSIVE MODE"))


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


def preview_organization_sync(
    session: Session,
    snapshot: DingTalkOrganizationSnapshot,
    *,
    encryption_key: str,
    actor: tuple[int, str] | None,
    root_mappings: tuple[tuple[int, str], ...],
    trigger: DingTalkOrgSyncTrigger = DingTalkOrgSyncTrigger.MANUAL,
    now: datetime | None = None,
    dining_manager_titles: frozenset[str] = frozenset({"店长"}),
    kitchen_manager_titles: frozenset[str] = frozenset({"厨房经理"}),
) -> OrganizationPreview:
    """Persist a point-in-time preview without modifying formal organization data."""

    take_organization_sync_lock(session)
    current_time = now or datetime.now(UTC)
    if actor is None and trigger != DingTalkOrgSyncTrigger.SCHEDULED:
        raise DingTalkOrganizationSyncError(
            "ORG_ROOT_CONFIG_INVALID", "A manual organization preview requires an actor"
        )
    state = _load_local_state(session)
    (
        org_units_by_id,
        employees_by_id,
        accounts_by_employee,
        users_by_id,
        role_codes_by_user,
        scope_users_by_pair,
    ) = _state_indexes(state)
    resolved_root_mappings = _resolve_root_mappings(state, root_mappings)
    anchors_by_root = dict(resolved_root_mappings)
    departments_by_id: dict[int, DingTalkDepartment] = {}
    for department in snapshot.departments:
        if department.department_id in departments_by_id:
            raise DingTalkOrganizationSyncError(
                "ORG_SNAPSHOT_INVALID", "DingTalk returned a duplicate department"
            )
        departments_by_id[department.department_id] = department
    path_candidates: dict[tuple[int, tuple[str, ...], OrgType], list[OrgUnit]] = defaultdict(list)
    authority_membership: dict[int, tuple[int, OrgUnit]] = {}
    exact_store_paths: set[tuple[int, tuple[str, ...]]] = set()
    for root_id, anchor in resolved_root_mappings:
        for organization in state.org_units:
            if organization.is_deleted or organization.id == anchor.id:
                continue
            relative_path = _local_relative_path(organization, anchor, org_units_by_id)
            if relative_path is None:
                continue
            if organization.type in (OrgType.REGION, OrgType.STORE):
                path_candidates[(root_id, relative_path, organization.type)].append(organization)
                authority_membership.setdefault(organization.id, (root_id, anchor))
                if organization.type == OrgType.STORE:
                    exact_store_paths.add((root_id, relative_path))

    bound_types = {
        organization.dingtalk_dept_id: organization.type
        for organization in state.org_units
        if not organization.is_deleted and organization.dingtalk_dept_id is not None
    }
    try:
        classified = classify_organization(
            snapshot,
            root_ids=frozenset(anchors_by_root),
            bound_types=bound_types,
            exact_store_paths=frozenset(exact_store_paths),
        )
    except OrganizationStructureError as exc:
        raise DingTalkOrganizationSyncError(exc.code, str(exc)) from None

    local_by_remote_id: dict[int, list[OrgUnit]] = defaultdict(list)
    for organization in state.org_units:
        if organization.dingtalk_dept_id is not None:
            local_by_remote_id[organization.dingtalk_dept_id].append(organization)
    org_units_by_code = {org.code: org for org in state.org_units}
    node_rows: list[DingTalkOrgSyncItem] = []
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
        stable_candidates = local_by_remote_id.get(remote.department_id, [])
        local_candidates = path_candidates.get((node.root_id, node.relative_path, node.kind), [])
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
                or _local_relative_path(
                    stable_candidates[0], anchors_by_root[node.root_id], org_units_by_id
                )
                is None
            ):
                conflict_code = "ORG_NODE_CLASSIFICATION_CONFLICT"
            else:
                proposed = stable_candidates[0]
        elif len(local_candidates) == 1:
            proposed = local_candidates[0]
            match_method = "EXACT_RELATIVE_PATH"
        elif len(local_candidates) > 1:
            conflict_code = "ORG_PATH_AMBIGUOUS"
            match_method = "EXACT_RELATIVE_PATH"

        if node.depth == 1:
            proposed_parent = anchors_by_root[node.root_id]
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
            if f"DINGTALK-{remote.department_id}" in org_units_by_code:
                conflict_code = conflict_code or "ORG_PATH_AMBIGUOUS"
        else:
            matched_local_ids.add(proposed.id)
            if proposed.name.strip() != remote.name.strip():
                change_fields.append("name")
            if (
                proposed_parent is not None and proposed.parent_id != proposed_parent.id
            ) or parent_is_staged_create:
                change_fields.append("parent_id")
            if proposed.status == "HISTORICAL":
                action = DingTalkOrgSyncAction.ACTIVATE
            elif change_fields:
                action = DingTalkOrgSyncAction.UPDATE
            else:
                action = DingTalkOrgSyncAction.LINK
                if proposed.dingtalk_dept_id is None:
                    change_fields = ["dingtalk_dept_id"]

        status = (
            DingTalkOrgSyncItemStatus.READY
            if conflict_code is None
            else DingTalkOrgSyncItemStatus.CONFLICT
        )
        baseline = (
            _create_store_baseline(
                parent=proposed_parent,
                code=f"DINGTALK-{remote.department_id}",
                name=remote.name,
                org_units=state.org_units,
            )
            if proposed is None
            else _resolved_store_baseline(proposed, org_units=state.org_units)
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
        node_rows.append(row)
        rows_by_remote_id[remote.department_id] = row
        node_matches[remote.department_id] = proposed

    ready_rows_by_local_node: dict[int, list[DingTalkOrgSyncItem]] = defaultdict(list)
    for row in node_rows:
        if row.status == DingTalkOrgSyncItemStatus.READY and row.proposed_org_unit_id is not None:
            ready_rows_by_local_node[row.proposed_org_unit_id].append(row)
    for duplicate_rows in ready_rows_by_local_node.values():
        if len(duplicate_rows) > 1:
            for row in duplicate_rows:
                row.status = DingTalkOrgSyncItemStatus.CONFLICT
                row.conflict_code = "ORG_PATH_AMBIGUOUS"

    local_authority_nodes = [
        organization
        for organization in state.org_units
        if organization.id in authority_membership
        and organization.type in (OrgType.REGION, OrgType.STORE)
        and not organization.is_deleted
    ]
    local_only_nodes = [
        organization
        for organization in local_authority_nodes
        if organization.status == "ACTIVE" and organization.id not in matched_local_ids
    ]
    for organization in sorted(local_only_nodes, key=lambda value: (value.type.value, value.id)):
        _root_id, anchor = authority_membership[organization.id]
        node_rows.append(
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
                baseline_fingerprint=_resolved_store_baseline(
                    organization, org_units=state.org_units
                ),
            )
        )

    region_rows = [row for row in node_rows if row.kind == DingTalkOrgSyncItemKind.REGION]
    store_rows = [row for row in node_rows if row.kind == DingTalkOrgSyncItemKind.STORE]
    candidate_departments = [node.department for node in classified.stores]
    candidate_by_id = {department.department_id: department for department in candidate_departments}
    store_matches = {
        department.department_id: node_matches[department.department_id]
        for department in candidate_departments
    }
    local_only_stores = [node for node in local_only_nodes if node.type == OrgType.STORE]

    active_employees = [
        employee
        for employee in state.employees
        if employee.status == EmployeeStatus.ACTIVE and not employee.is_deleted
    ]
    active_remote_users = tuple(user for user in snapshot.users if user.active)
    directory_matches = match_directory_users(
        tuple(
            LocalEmployeeIdentity(
                employee_id=employee.id,
                emp_no=employee.emp_no,
                name=employee.name,
                dingtalk_user_id_hash=employee.dingtalk_user_id_hash,
            )
            for employee in active_employees
        ),
        tuple(
            DingTalkDirectoryUser(
                user_id=user.user_id,
                name=user.name,
                job_number=user.job_number,
                active=user.active,
            )
            for user in active_remote_users
        ),
        encryption_key=encryption_key,
    )
    matches_by_remote_id = {match.user_id: match for match in directory_matches.matches}

    account_ids_by_hash: dict[str, list[int]] = defaultdict(list)
    employee_ids_by_hash: dict[str, list[int]] = defaultdict(list)
    for account in state.users:
        if account.dingtalk_user_id_hash:
            account_ids_by_hash[account.dingtalk_user_id_hash].append(account.id)
    for employee in state.employees:
        if employee.dingtalk_user_id_hash:
            employee_ids_by_hash[employee.dingtalk_user_id_hash].append(employee.id)

    children_by_parent: dict[int, set[int]] = defaultdict(set)
    for department in snapshot.departments:
        if department.parent_id is not None:
            children_by_parent[department.parent_id].add(department.department_id)
    remote_store_ids = set(candidate_by_id)

    def store_subtree(store_department_id: int) -> set[int]:
        result = {store_department_id}
        frontier = [store_department_id]
        while frontier:
            parent_id = frontier.pop()
            for child_id in children_by_parent.get(parent_id, set()):
                if child_id in remote_store_ids and child_id != store_department_id:
                    continue
                if child_id not in result:
                    result.add(child_id)
                    frontier.append(child_id)
        return result

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

    for local_store in local_only_stores:
        local_path = next(
            row.remote_department_path
            for row in store_rows
            if row.proposed_org_unit_id == local_store.id
            and row.action == DingTalkOrgSyncAction.DEACTIVATE
        )
        for review_department in (Department.DINING, Department.KITCHEN):
            add_reviewer_row(
                row_key=(f"REVIEWER:LOCAL:{local_store.id}:{review_department.value}"),
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
                remote_path=local_path,
            )

    for remote_store in candidate_departments:
        proposed_store_for_review = store_matches[remote_store.department_id]
        store_row = next(
            row for row in store_rows if row.remote_department_id == remote_store.department_id
        )
        subtree = store_subtree(remote_store.department_id)
        scoped_users = [
            user
            for user in active_remote_users
            if any(department_id in subtree for department_id in user.department_ids)
        ]
        role_candidates: dict[Department, list[DingTalkOrganizationUser]] = defaultdict(list)
        for remote_candidate in scoped_users:
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
            scope_user_ids = (
                scope_users_by_pair.get((proposed_store_for_review.id, review_department), ())
                if proposed_store_for_review is not None
                else ()
            )
            if store_row.status == DingTalkOrgSyncItemStatus.CONFLICT:
                reviewer_conflict_code = "STORE_UNRESOLVED"
            elif not candidates:
                if scope_user_ids:
                    reviewer_action = "REMOVE"
                    method = "REMOVE_MISSING_MANAGER"
                    status = DingTalkOrgSyncItemStatus.READY
                else:
                    reviewer_conflict_code = "MANAGER_NOT_FOUND"
            elif len(candidates) > 1:
                reviewer_conflict_code = "MULTIPLE_MANAGERS"
            else:
                selected_remote = candidates[0]
                match = matches_by_remote_id.get(selected_remote.user_id)
                if match is None:
                    reviewer_conflict_code = "MANAGER_EMPLOYEE_NOT_MATCHED"
                else:
                    selected_employee = employees_by_id.get(match.employee_id)
                    method = match.method
                    if match.method == "UNIQUE_NAME":
                        reviewer_conflict_code = "WEAK_NAME_MATCH"
                    elif selected_employee is None:
                        reviewer_conflict_code = "MANAGER_EMPLOYEE_NOT_MATCHED"
                    else:
                        accounts = accounts_by_employee.get(selected_employee.id, [])
                        provider_hash = blind_index_dingtalk_user_id(
                            selected_remote.user_id, key=encryption_key
                        )
                        if len(accounts) > 1:
                            reviewer_conflict_code = "MULTIPLE_LOCAL_ACCOUNTS"
                        elif accounts and (
                            accounts[0].is_deleted or accounts[0].status != "ACTIVE"
                        ):
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
                row_key=(
                    f"REVIEWER:REMOTE:{remote_store.department_id}:" f"{review_department.value}"
                ),
                remote_store=remote_store,
                store=proposed_store_for_review,
                department=review_department,
                action=reviewer_action,
                method=method,
                status=status,
                conflict_code=reviewer_conflict_code,
                selected_remote=selected_remote,
                selected_employee=selected_employee,
                remote_name=remote_store.name,
                remote_path=next(
                    row.remote_department_path
                    for row in store_rows
                    if row.remote_department_id == remote_store.department_id
                ),
            )

    drafts_by_identity: dict[tuple[str, Department], list[_ReviewerDraft]] = defaultdict(list)
    for draft in reviewer_drafts:
        if draft.row.remote_user_id_hash is not None and draft.row.department is not None:
            drafts_by_identity[(draft.row.remote_user_id_hash, draft.row.department)].append(draft)
    for drafts in drafts_by_identity.values():
        remote_store_ids_for_user = {draft.row.remote_department_id for draft in drafts}
        if len(remote_store_ids_for_user) <= 1:
            continue
        for draft in drafts:
            draft.row.status = DingTalkOrgSyncItemStatus.CONFLICT
            draft.row.action = DingTalkOrgSyncAction.NO_CHANGE
            draft.row.change_fields = []
            draft.row.conflict_code = "MANAGER_ASSIGNED_MULTIPLE_STORES"

    reviewer_rows = [draft.row for draft in reviewer_drafts]
    all_rows = [*node_rows, *reviewer_rows]
    complete_local_baseline = _complete_local_baseline(state)
    _wrap_baselines(all_rows, complete_local_baseline)
    planned_baseline_hash = _planned_baseline_hash(all_rows)
    snapshot_hash = _snapshot_hash(snapshot, encryption_key=encryption_key)
    root_config_hash = _root_config_hash(resolved_root_mappings)
    reviewer_metadata = {draft.row.row_key: draft for draft in reviewer_drafts}

    if trigger == DingTalkOrgSyncTrigger.SCHEDULED:
        reusable_batches = list(
            session.scalars(
                select(DingTalkOrgSyncBatch)
                .where(
                    DingTalkOrgSyncBatch.status == DingTalkOrgSyncBatchStatus.PREVIEWED,
                    DingTalkOrgSyncBatch.trigger == DingTalkOrgSyncTrigger.SCHEDULED,
                    DingTalkOrgSyncBatch.root_config_hash == root_config_hash,
                    DingTalkOrgSyncBatch.snapshot_hash == snapshot_hash,
                    DingTalkOrgSyncBatch.expires_at > current_time,
                )
                .order_by(DingTalkOrgSyncBatch.id.desc())
                .with_for_update()
            ).all()
        )
        for reusable in reusable_batches:
            reusable_items = list(
                session.scalars(
                    select(DingTalkOrgSyncItem)
                    .where(DingTalkOrgSyncItem.batch_id == reusable.id)
                    .order_by(DingTalkOrgSyncItem.id)
                    .with_for_update()
                ).all()
            )
            if _planned_baseline_hash(reusable_items) != planned_baseline_hash:
                continue
            reusable.last_checked_at = current_time
            session.flush()
            result = _organization_preview_result(
                reusable,
                reusable_items,
                state=state,
                reviewer_metadata=reviewer_metadata,
            )
            session.commit()
            return result

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
        trigger=trigger,
        expires_at=current_time + _PREVIEW_TTL,
        requested_by_user_id=actor[0] if actor is not None else None,
        last_checked_at=current_time,
        remote_region_count=len(classified.regions),
        local_region_count=sum(node.type == OrgType.REGION for node in local_authority_nodes),
        ready_region_count=sum(
            row.status == DingTalkOrgSyncItemStatus.READY for row in region_rows
        ),
        region_conflict_count=sum(
            row.status == DingTalkOrgSyncItemStatus.CONFLICT for row in region_rows
        ),
        remote_store_count=len(candidate_departments),
        local_store_count=sum(node.type == OrgType.STORE for node in local_authority_nodes),
        ready_store_count=sum(row.status == DingTalkOrgSyncItemStatus.READY for row in store_rows),
        store_conflict_count=sum(
            row.status == DingTalkOrgSyncItemStatus.CONFLICT for row in store_rows
        ),
        ready_reviewer_count=sum(
            row.status == DingTalkOrgSyncItemStatus.READY for row in reviewer_rows
        ),
        reviewer_conflict_count=sum(
            row.status == DingTalkOrgSyncItemStatus.CONFLICT for row in reviewer_rows
        ),
        warning_count=len(classified.warning_department_ids),
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
        reviewer_metadata=reviewer_metadata,
    )
    session.commit()
    return result


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
    if _as_utc(batch.expires_at) <= current_time:
        _mark_batch_stale(session, batch, items, actor=actor, error_code="PREVIEW_EXPIRED")
        raise DingTalkOrganizationSyncError(
            "PREVIEW_EXPIRED", "Organization preview expired; preview again"
        )
    if _snapshot_hash(fresh_snapshot, encryption_key=encryption_key) != batch.snapshot_hash:
        _mark_batch_stale(
            session, batch, items, actor=actor, error_code="PROVIDER_SNAPSHOT_CHANGED"
        )
        raise DingTalkOrganizationSyncError(
            "PROVIDER_SNAPSHOT_CHANGED",
            "DingTalk organization changed; preview again",
        )
    if any(item.kind == DingTalkOrgSyncItemKind.REGION for item in items):
        session.rollback()
        raise DingTalkOrganizationSyncError(
            "ORG_REGION_APPLY_NOT_SUPPORTED",
            "Region changes require the hierarchy apply transaction",
        )
    reviewer_conflicts = [
        item
        for item in items
        if item.kind == DingTalkOrgSyncItemKind.REVIEWER
        and item.status == DingTalkOrgSyncItemStatus.CONFLICT
    ]
    if reviewer_conflicts:
        session.rollback()
        raise DingTalkOrganizationSyncError(
            "REVIEWER_CONFLICTS",
            "Reviewer conflicts must be resolved before confirmation",
        )

    _lock_org_unit_table_against_phantoms(session)
    state = _load_local_state(session, for_update=True)
    (
        org_units_by_id,
        employees_by_id,
        accounts_by_employee,
        _users_by_id,
        role_codes_by_user,
        scope_users_by_pair,
    ) = _state_indexes(state)
    org_units_by_code = {org.code: org for org in state.org_units}

    stale = False
    complete_local_baseline = _complete_local_baseline(state)
    for item in items:
        if item.kind in (DingTalkOrgSyncItemKind.REGION, DingTalkOrgSyncItemKind.STORE):
            if item.action == DingTalkOrgSyncAction.CREATE:
                parent = _optional_get(org_units_by_id, item.proposed_parent_org_unit_id)
                expected = _create_store_baseline(
                    parent=parent,
                    code=f"DINGTALK-{item.remote_department_id}",
                    name=item.remote_department_name,
                    org_units=state.org_units,
                )
            else:
                expected = _resolved_store_baseline(
                    _optional_get(org_units_by_id, item.proposed_org_unit_id),
                    org_units=state.org_units,
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

    store_changes: list[dict[str, object]] = []
    reviewer_changes: list[dict[str, object]] = []
    stores_by_remote_id: dict[int, OrgUnit] = {}
    assigned_roles = {(assignment.user_id, assignment.role_id) for assignment in state.user_roles}
    usernames = {user.username for user in state.users}
    try:
        for item in ready_store_items:
            action = item.action
            remote_department_id = item.remote_department_id
            if remote_department_id is None and action != DingTalkOrgSyncAction.DEACTIVATE:
                raise RuntimeError("ready store item is missing its remote department")
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
            if action == DingTalkOrgSyncAction.DEACTIVATE:
                if before_store is None:
                    raise _ConcurrentChange("store baseline changed")
                applied_store = before_store
                applied_store.status = "HISTORICAL"
            elif action == DingTalkOrgSyncAction.CREATE:
                parent = _optional_get(org_units_by_id, item.proposed_parent_org_unit_id)
                duplicate_name = any(
                    org.type == OrgType.STORE
                    and not org.is_deleted
                    and _normalize_name(org.name) == _normalize_name(item.remote_department_name)
                    for org in state.org_units
                )
                if (
                    parent is None
                    or f"DINGTALK-{remote_department_id}" in org_units_by_code
                    or duplicate_name
                ):
                    raise _ConcurrentChange("create store baseline changed")
                applied_store = OrgUnit(
                    code=f"DINGTALK-{remote_department_id}",
                    name=item.remote_department_name,
                    type=OrgType.STORE,
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
                    raise _ConcurrentChange("store baseline changed")
                applied_store = before_store
                applied_store.dingtalk_dept_id = remote_department_id
                if action in {DingTalkOrgSyncAction.ACTIVATE, DingTalkOrgSyncAction.UPDATE}:
                    applied_store.status = "ACTIVE"
                if action in {DingTalkOrgSyncAction.ACTIVATE, DingTalkOrgSyncAction.UPDATE}:
                    if "name" in item.change_fields:
                        applied_store.name = item.remote_department_name
                    if "parent_id" in item.change_fields:
                        applied_store.parent_id = item.proposed_parent_org_unit_id
            if remote_department_id is not None:
                stores_by_remote_id[remote_department_id] = applied_store
            item.status = DingTalkOrgSyncItemStatus.APPLIED
            store_changes.append(
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

        for item in ready_reviewer_items:
            action = item.action
            department = _required_reviewer_department(item)
            target_store = _optional_get(org_units_by_id, item.proposed_org_unit_id)
            if target_store is None and item.remote_department_id is not None:
                target_store = stores_by_remote_id.get(item.remote_department_id)
            if target_store is None:
                raise RuntimeError("ready reviewer item lost its organization")
            # CREATE proposals do not have an internal organization id until
            # the store row above is flushed.  Persist the resolved id so
            # freshness checks cover its reviewer assignments too.
            item.proposed_org_unit_id = target_store.id
            before_user_ids = tuple(scope_users_by_pair.get((target_store.id, department), ()))
            session.execute(
                delete(UserReviewScope).where(
                    UserReviewScope.org_unit_id == target_store.id,
                    UserReviewScope.department == department,
                )
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
                "applied_store_count": batch.ready_store_count,
                "applied_reviewer_count": batch.ready_reviewer_count,
                "unresolved_count": unresolved,
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

    return OrganizationApplyResult(
        applied_stores=batch.ready_store_count,
        applied_reviewers=batch.ready_reviewer_count,
        unresolved=unresolved,
        already_applied=False,
    )
