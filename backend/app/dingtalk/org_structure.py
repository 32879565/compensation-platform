from __future__ import annotations

import unicodedata
from dataclasses import dataclass

from app.dingtalk.client import DingTalkDepartment, DingTalkOrganizationSnapshot
from app.models.org import OrgType

_MAX_RELATIVE_DEPTH = 32


@dataclass(frozen=True)
class ClassifiedNode:
    department: DingTalkDepartment
    kind: OrgType
    root_id: int
    relative_path: tuple[str, ...]
    depth: int


@dataclass(frozen=True)
class ClassifiedOrganization:
    regions: tuple[ClassifiedNode, ...]
    stores: tuple[ClassifiedNode, ...]
    internal_department_ids: frozenset[int]
    warning_department_ids: frozenset[int]


class OrganizationStructureError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def normalize_org_name(value: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


def classify_organization(
    snapshot: DingTalkOrganizationSnapshot,
    *,
    root_ids: frozenset[int],
    bound_types: dict[int, OrgType],
    exact_store_paths: frozenset[tuple[int, tuple[str, ...]]],
) -> ClassifiedOrganization:
    """Classify configured root subtrees without consulting mutable state."""

    if not root_ids or any(
        not isinstance(root_id, int) or isinstance(root_id, bool) or root_id <= 0
        for root_id in root_ids
    ):
        raise OrganizationStructureError(
            "ORG_SNAPSHOT_INVALID", "configured DingTalk roots are invalid"
        )

    by_id: dict[int, DingTalkDepartment] = {}
    children_by_parent: dict[int | None, list[int]] = {}
    for department in snapshot.departments:
        department_id = department.department_id
        if department_id in by_id:
            raise OrganizationStructureError(
                "ORG_SNAPSHOT_INVALID", "duplicate DingTalk department"
            )
        by_id[department_id] = department
        children_by_parent.setdefault(department.parent_id, []).append(department_id)
    for children in children_by_parent.values():
        children.sort()

    # A configured root may be present when the snapshot came from a broader
    # read. Its ancestry is only used to reject overlapping configured roots;
    # the root itself remains a synchronization boundary, not a classified row.
    for root_id in sorted(root_ids):
        current = by_id.get(root_id)
        seen = {root_id}
        while current is not None and current.parent_id is not None:
            parent_id = current.parent_id
            if parent_id in root_ids:
                if parent_id == root_id:
                    raise OrganizationStructureError(
                        "ORG_SNAPSHOT_INVALID", "DingTalk department cycle"
                    )
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "configured DingTalk roots overlap"
                )
            if parent_id in seen:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "DingTalk department cycle"
                )
            seen.add(parent_id)
            current = by_id.get(parent_id)

    def relative_path(department: DingTalkDepartment) -> tuple[int, tuple[str, ...]]:
        names = [normalize_org_name(department.name)]
        seen = {department.department_id}
        current = department
        while True:
            parent_id = current.parent_id
            if parent_id in root_ids:
                return parent_id, tuple(reversed(names))
            parent = by_id.get(parent_id) if parent_id is not None else None
            if parent is None:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "orphan DingTalk department"
                )
            if parent.department_id in seen:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "DingTalk department cycle"
                )
            seen.add(parent.department_id)
            names.append(normalize_org_name(parent.name))
            if len(names) > _MAX_RELATIVE_DEPTH:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "DingTalk department path exceeds 32 levels"
                )
            current = parent

    paths = {
        department_id: relative_path(by_id[department_id])
        for department_id in sorted(by_id)
        if department_id not in root_ids
    }

    store_ids: set[int] = set()
    for department_id, (root_id, path) in paths.items():
        bound_type = bound_types.get(department_id)
        if bound_type == OrgType.STORE:
            store_ids.add(department_id)
        elif bound_type == OrgType.REGION:
            continue
        elif (root_id, path) in exact_store_paths or path[-1].endswith("店"):
            store_ids.add(department_id)

    def ancestor_ids(department_id: int, root_id: int) -> tuple[int, ...]:
        ancestors: list[int] = []
        current = by_id[department_id]
        while current.parent_id != root_id:
            parent_id = current.parent_id
            parent = by_id.get(parent_id) if parent_id is not None else None
            if parent is None:
                raise OrganizationStructureError(
                    "ORG_SNAPSHOT_INVALID", "orphan DingTalk department"
                )
            ancestors.append(parent.department_id)
            current = parent
        return tuple(ancestors)

    region_ids: set[int] = set()
    internal_ids: set[int] = set()
    for department_id, (root_id, _path) in paths.items():
        ancestors = ancestor_ids(department_id, root_id)
        store_ancestors = store_ids.intersection(ancestors)
        if department_id in store_ids and store_ancestors:
            raise OrganizationStructureError(
                "ORG_NODE_CLASSIFICATION_CONFLICT", "a store contains another store"
            )
        if store_ancestors:
            internal_ids.add(department_id)
        if department_id in store_ids:
            region_ids.update(
                ancestor_id for ancestor_id in ancestors if ancestor_id not in store_ids
            )

    def node(department_id: int, kind: OrgType) -> ClassifiedNode:
        root_id, path = paths[department_id]
        return ClassifiedNode(
            department=by_id[department_id],
            kind=kind,
            root_id=root_id,
            relative_path=path,
            depth=len(path),
        )

    selected_ids = region_ids | store_ids | internal_ids
    return ClassifiedOrganization(
        regions=tuple(
            sorted(
                (node(department_id, OrgType.REGION) for department_id in region_ids),
                key=lambda value: (value.depth, value.department.department_id),
            )
        ),
        stores=tuple(node(department_id, OrgType.STORE) for department_id in sorted(store_ids)),
        internal_department_ids=frozenset(internal_ids),
        warning_department_ids=frozenset(set(paths) - selected_ids),
    )
