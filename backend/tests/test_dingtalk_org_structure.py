from __future__ import annotations

import pytest

from app.dingtalk.client import DingTalkDepartment, DingTalkOrganizationSnapshot
from app.dingtalk.org_structure import (
    OrganizationStructureError,
    classify_organization,
)
from app.models.org import OrgType


def _snapshot(*departments: DingTalkDepartment) -> DingTalkOrganizationSnapshot:
    return DingTalkOrganizationSnapshot(departments=departments, users=())


def test_classifies_region_store_and_internal_departments() -> None:
    snapshot = _snapshot(
        DingTalkDepartment(100, 1, "运营中心"),
        DingTalkDepartment(110, 100, "广州一区"),
        DingTalkDepartment(120, 110, "天河店"),
        DingTalkDepartment(121, 120, "厅面"),
    )

    result = classify_organization(
        snapshot,
        root_ids=frozenset({100}),
        bound_types={},
        exact_store_paths=frozenset(),
    )

    assert [(node.department.department_id, node.kind) for node in result.regions] == [
        (110, OrgType.REGION)
    ]
    assert [node.department.department_id for node in result.stores] == [120]
    assert result.stores[0].root_id == 100
    assert result.stores[0].relative_path == ("广州一区", "天河店")
    assert result.stores[0].depth == 2
    assert result.internal_department_ids == frozenset({121})
    assert result.warning_department_ids == frozenset()


def test_store_detection_priority_and_name_normalization() -> None:
    snapshot = _snapshot(
        DingTalkDepartment(110, 100, " Ｒｅｇｉｏｎ "),
        DingTalkDepartment(120, 110, " Remote Store "),
        DingTalkDepartment(130, 110, " Exact Store "),
        DingTalkDepartment(140, 110, "后缀店"),
        DingTalkDepartment(150, 110, "不应分类店"),
    )

    result = classify_organization(
        snapshot,
        root_ids=frozenset({100}),
        bound_types={120: OrgType.STORE, 150: OrgType.REGION},
        exact_store_paths=frozenset(
            {(100, ("region", "exact store")), (100, ("region", "不应分类店"))}
        ),
    )

    assert [node.department.department_id for node in result.stores] == [120, 130, 140]
    assert [node.department.department_id for node in result.regions] == [110]
    assert result.warning_department_ids == frozenset({150})


@pytest.mark.parametrize(
    ("departments", "root_ids"),
    [
        (
            (
                DingTalkDepartment(110, 100, "重复 A"),
                DingTalkDepartment(110, 100, "重复 B"),
            ),
            frozenset({100}),
        ),
        ((DingTalkDepartment(121, 120, "孤儿部门"),), frozenset({100})),
        (
            (
                DingTalkDepartment(110, 111, "循环 A"),
                DingTalkDepartment(111, 110, "循环 B"),
            ),
            frozenset({100}),
        ),
        (
            (
                DingTalkDepartment(100, 1, "根 A"),
                DingTalkDepartment(200, 100, "根 B"),
                DingTalkDepartment(210, 200, "门店"),
            ),
            frozenset({100, 200}),
        ),
    ],
    ids=["duplicate-id", "orphan", "cycle", "overlapping-roots"],
)
def test_invalid_snapshots_fail_closed(
    departments: tuple[DingTalkDepartment, ...], root_ids: frozenset[int]
) -> None:
    with pytest.raises(OrganizationStructureError) as caught:
        classify_organization(
            _snapshot(*departments),
            root_ids=root_ids,
            bound_types={},
            exact_store_paths=frozenset(),
        )

    assert caught.value.code == "ORG_SNAPSHOT_INVALID"


def test_path_deeper_than_32_levels_fails_closed() -> None:
    departments = []
    parent_id = 100
    for offset in range(33):
        department_id = 101 + offset
        departments.append(DingTalkDepartment(department_id, parent_id, f"第 {offset + 1} 层"))
        parent_id = department_id

    with pytest.raises(OrganizationStructureError) as caught:
        classify_organization(
            _snapshot(*departments),
            root_ids=frozenset({100}),
            bound_types={},
            exact_store_paths=frozenset(),
        )

    assert caught.value.code == "ORG_SNAPSHOT_INVALID"


def test_nested_stores_fail_closed() -> None:
    snapshot = _snapshot(
        DingTalkDepartment(120, 100, "天河店"),
        DingTalkDepartment(121, 120, "二楼店"),
    )

    with pytest.raises(OrganizationStructureError) as caught:
        classify_organization(
            snapshot,
            root_ids=frozenset({100}),
            bound_types={},
            exact_store_paths=frozenset(),
        )

    assert caught.value.code == "ORG_NODE_CLASSIFICATION_CONFLICT"


def test_results_are_immutable_and_deterministically_sorted() -> None:
    snapshot = _snapshot(
        DingTalkDepartment(300, 100, "深层区域"),
        DingTalkDepartment(302, 300, "深层店"),
        DingTalkDepartment(200, 100, "浅层店"),
        DingTalkDepartment(150, 100, "同层区域"),
        DingTalkDepartment(151, 150, "同层店"),
    )

    result = classify_organization(
        snapshot,
        root_ids=frozenset({100}),
        bound_types={},
        exact_store_paths=frozenset(),
    )

    assert [node.department.department_id for node in result.regions] == [150, 300]
    assert [node.department.department_id for node in result.stores] == [151, 200, 302]
    with pytest.raises(AttributeError):
        result.stores[0].depth = 99  # type: ignore[misc]
