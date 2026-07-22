"""Persistence and RBAC contracts for direct DingTalk organization sync."""

from __future__ import annotations

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Integer, String, UniqueConstraint

import app.models as models
from app.auth.permissions import PERMISSION_CATALOG, ROLE_DEFINITIONS, Perm
from app.models.auth import User, UserReviewScope
from app.models.dingtalk import (
    DingTalkOrgSyncBatch,
    DingTalkOrgSyncBatchStatus,
    DingTalkOrgSyncItem,
    DingTalkOrgSyncItemKind,
    DingTalkOrgSyncItemStatus,
)
from app.models.org import OrgUnit


def _unique_column_sets(table) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in table.constraints
        if isinstance(constraint, UniqueConstraint)
    }


def test_org_unit_has_nullable_unique_positive_dingtalk_department_identity() -> None:
    column = OrgUnit.__table__.c.dingtalk_dept_id

    assert isinstance(column.type, BigInteger)
    assert column.nullable is True
    assert ("dingtalk_dept_id",) in _unique_column_sets(OrgUnit.__table__)
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_org_unit_dingtalk_dept_id_positive"
        for constraint in OrgUnit.__table__.constraints
    )


def test_user_has_nullable_unique_dingtalk_user_hash() -> None:
    column = User.__table__.c.dingtalk_user_id_hash

    assert isinstance(column.type, String)
    assert column.type.length == 64
    assert column.nullable is True
    assert ("dingtalk_user_id_hash",) in _unique_column_sets(User.__table__)


def test_review_scope_allows_only_one_reviewer_per_store_department() -> None:
    assert ("org_unit_id", "department") in _unique_column_sets(UserReviewScope.__table__)


def test_org_sync_batch_records_preview_lifecycle_and_aggregate_counts() -> None:
    table = DingTalkOrgSyncBatch.__table__

    assert {status.value for status in DingTalkOrgSyncBatchStatus} == {
        "PREVIEWED",
        "APPLIED",
        "STALE",
    }
    assert isinstance(table.c.public_id.type, String)
    assert table.c.public_id.type.length == 32
    assert table.c.public_id.nullable is False
    assert any(
        index.unique and tuple(column.name for column in index.columns) == ("public_id",)
        for index in table.indexes
    )
    assert isinstance(table.c.snapshot_hash.type, String)
    assert table.c.snapshot_hash.type.length == 64
    assert isinstance(table.c.expires_at.type, DateTime)
    assert table.c.requested_by_user_id.nullable is False
    assert table.c.applied_by_user_id.nullable is True
    assert table.c.applied_at.nullable is True
    for column_name in (
        "remote_store_count",
        "local_store_count",
        "ready_store_count",
        "store_conflict_count",
        "ready_reviewer_count",
        "reviewer_conflict_count",
    ):
        column = table.c[column_name]
        assert isinstance(column.type, Integer)
        assert column.nullable is False
    assert {
        constraint.name
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    } >= {
        "ck_dingtalk_org_sync_batch_nonnegative_counts",
        "ck_dingtalk_org_sync_batch_applied_audit",
    }


def test_org_sync_item_keeps_remote_identity_encrypted_and_rows_idempotent() -> None:
    table = DingTalkOrgSyncItem.__table__

    assert {kind.value for kind in DingTalkOrgSyncItemKind} == {"STORE", "REVIEWER"}
    assert {status.value for status in DingTalkOrgSyncItemStatus} == {
        "READY",
        "CONFLICT",
        "APPLIED",
        "IGNORED",
    }
    assert ("batch_id", "row_key") in _unique_column_sets(table)
    assert table.c.batch_id.nullable is False
    assert table.c.row_key.nullable is False
    assert table.c.remote_department_id.nullable is True
    assert table.c.remote_department_name.nullable is False
    assert table.c.remote_department_path.nullable is False
    assert table.c.remote_user_id_hash.type.length == 64
    assert table.c.remote_user_id_hash.nullable is True
    assert table.c.applied_identity_proof.type.length == 64
    assert table.c.applied_identity_proof.nullable is True
    assert table.c.proposed_org_unit_id.nullable is True
    assert table.c.proposed_parent_org_unit_id.nullable is True
    assert table.c.proposed_employee_id.nullable is True
    assert table.c.department.nullable is True
    assert table.c.match_method.nullable is False
    assert table.c.conflict_code.nullable is True
    assert table.c.baseline_fingerprint.type.length == 64
    assert table.c.baseline_fingerprint.nullable is False
    assert any(
        isinstance(constraint, CheckConstraint)
        and constraint.name == "ck_dingtalk_org_sync_item_remote_department_positive"
        for constraint in table.constraints
    )


def test_org_sync_models_are_available_from_the_central_model_registry() -> None:
    assert models.DingTalkOrgSyncBatch is DingTalkOrgSyncBatch
    assert models.DingTalkOrgSyncItem is DingTalkOrgSyncItem
    assert models.DingTalkOrgSyncBatchStatus is DingTalkOrgSyncBatchStatus
    assert models.DingTalkOrgSyncItemKind is DingTalkOrgSyncItemKind
    assert models.DingTalkOrgSyncItemStatus is DingTalkOrgSyncItemStatus


def test_direct_org_sync_permission_is_limited_to_hr_and_super_admin() -> None:
    assert Perm.DINGTALK_ORG_SYNC == "dingtalk_org:sync"
    assert Perm.DINGTALK_ORG_SYNC in PERMISSION_CATALOG
    roles = {definition.code: definition for definition in ROLE_DEFINITIONS}

    assert Perm.DINGTALK_ORG_SYNC in roles["GROUP_HR"].permissions
    assert Perm.DINGTALK_ORG_SYNC in roles["SUPER_ADMIN"].permissions
    assert all(
        Perm.DINGTALK_ORG_SYNC not in definition.permissions
        for code, definition in roles.items()
        if code not in {"GROUP_HR", "SUPER_ADMIN"}
    )
