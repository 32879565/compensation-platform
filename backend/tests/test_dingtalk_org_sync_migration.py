"""D20 migration contracts for direct DingTalk organization sync."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import BigInteger, Integer, String, UniqueConstraint
from sqlalchemy.dialects import postgresql


class _Connection:
    def __init__(
        self,
        duplicate_scope_count: int,
        legacy_rows: list[dict[str, object]] | None = None,
        employee_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.duplicate_scope_count = duplicate_scope_count
        self.legacy_rows = legacy_rows or []
        self.employee_rows = employee_rows or []
        self.statements: list[str] = []
        self.dropped_schema_items: list[str] = []
        self.dialect = postgresql.dialect()

    def scalar(self, statement: object) -> int:
        self.statements.append(str(statement))
        return self.duplicate_scope_count

    def execute(self, statement: object, _params: object = None):
        rendered = str(statement)
        self.statements.append(rendered)
        selected_rows = (
            self.employee_rows
            if "SELECT id, dingtalk_user_id_hash" in rendered
            else self.legacy_rows
        )

        class _Rows:
            def mappings(self):
                return self

            def all(inner_self) -> list[object]:
                return list(selected_rows)

        return _Rows()

    def _run_ddl_visitor(self, _visitor, element, **_kwargs: object) -> None:
        self.dropped_schema_items.append(element.name)


class _Op:
    def __init__(
        self,
        duplicate_scope_count: int,
        legacy_rows: list[dict[str, object]] | None = None,
        employee_rows: list[dict[str, object]] | None = None,
    ) -> None:
        self.connection = _Connection(duplicate_scope_count, legacy_rows, employee_rows)
        self.actions: list[tuple[str, object]] = []

    def get_bind(self) -> _Connection:
        return self.connection

    def __getattr__(self, name: str):
        def recorder(*args: object, **kwargs: object) -> None:
            self.actions.append((name, (args, kwargs)))

        return recorder


def _migration() -> object:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "i4r7l0n2q568_d20_dingtalk_org_sync.py"
    )
    spec = importlib.util.spec_from_file_location("d20_dingtalk_org_sync", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_fails_closed_before_ddl_when_reviewer_scopes_are_ambiguous(
    monkeypatch,
) -> None:
    migration = _migration()
    op = _Op(duplicate_scope_count=2)
    monkeypatch.setattr(migration, "op", op)

    with pytest.raises(RuntimeError, match="duplicate.*reviewer scope"):
        migration.upgrade()

    assert op.actions == []
    assert "GROUP BY org_unit_id, department" in op.connection.statements[0]


def test_upgrade_adds_direct_sync_schema_and_hr_permissions(monkeypatch) -> None:
    migration = _migration()
    op = _Op(duplicate_scope_count=0)
    monkeypatch.setattr(migration, "op", op)

    migration.upgrade()

    assert migration.down_revision == "h3q6k9m1p457"
    added_columns = {
        (args[0], args[1].name): args[1]
        for name, (args, _kwargs) in op.actions
        if name == "add_column"
    }
    assert isinstance(added_columns[("org_unit", "dingtalk_dept_id")].type, BigInteger)
    assert added_columns[("org_unit", "dingtalk_dept_id")].nullable is True
    assert isinstance(added_columns[("app_user", "dingtalk_user_id_hash")].type, String)
    assert added_columns[("app_user", "dingtalk_user_id_hash")].type.length == 64
    assert added_columns[("app_user", "dingtalk_user_id_hash")].nullable is True

    unique_constraints = {
        args[0]: (args[1], tuple(args[2]))
        for name, (args, _kwargs) in op.actions
        if name == "create_unique_constraint"
    }
    assert unique_constraints["uq_org_unit_dingtalk_dept_id"] == (
        "org_unit",
        ("dingtalk_dept_id",),
    )
    assert unique_constraints["uq_app_user_dingtalk_user_id_hash"] == (
        "app_user",
        ("dingtalk_user_id_hash",),
    )
    assert unique_constraints["uq_user_review_scope_org_department"] == (
        "user_review_scope",
        ("org_unit_id", "department"),
    )

    created_tables = {
        args[0]: args[1:] for name, (args, _kwargs) in op.actions if name == "create_table"
    }
    assert set(created_tables) == {
        "dingtalk_org_sync_batch",
        "dingtalk_org_sync_item",
        "dingtalk_org_sync_notification",
    }
    batch_columns = {
        column.name: column
        for column in created_tables["dingtalk_org_sync_batch"]
        if hasattr(column, "name")
    }
    item_columns = {
        column.name: column
        for column in created_tables["dingtalk_org_sync_item"]
        if hasattr(column, "name")
    }
    assert batch_columns["public_id"].type.length == 32
    assert batch_columns["snapshot_hash"].type.length == 64
    assert batch_columns["requested_by_user_id"].nullable is True
    assert batch_columns["applied_by_user_id"].nullable is True
    assert batch_columns["trigger"].nullable is False
    assert batch_columns["root_config_hash"].type.length == 64
    assert batch_columns["last_checked_at"].nullable is True
    for column_name in (
        "remote_region_count",
        "local_region_count",
        "ready_region_count",
        "region_conflict_count",
        "warning_count",
    ):
        assert isinstance(batch_columns[column_name].type, Integer)
        assert batch_columns[column_name].nullable is False
    assert item_columns["remote_user_id_hash"].type.length == 64
    assert item_columns["applied_identity_proof"].type.length == 64
    assert item_columns["applied_identity_proof"].nullable is True
    assert item_columns["remote_department_id"].nullable is True
    assert item_columns["proposed_parent_org_unit_id"].nullable is True
    assert item_columns["baseline_fingerprint"].nullable is False
    assert item_columns["action"].nullable is False
    assert item_columns["change_fields"].nullable is False
    assert item_columns["proposed_org_type"].nullable is True
    notification_columns = {
        column.name: column
        for column in created_tables["dingtalk_org_sync_notification"]
        if hasattr(column, "name")
    }
    assert notification_columns["idempotency_key"].type.length == 160
    notification_constraints = {
        constraint.name
        for constraint in created_tables["dingtalk_org_sync_notification"]
        if isinstance(constraint, UniqueConstraint)
    }
    assert "uq_dingtalk_org_sync_notification_key" in notification_constraints

    executed_sql = "\n".join(
        str(args[0]) for name, (args, _kwargs) in op.actions if name == "execute"
    )
    assert "dingtalk_org:sync" in executed_sql
    assert "GROUP_HR" in executed_sql
    assert "SUPER_ADMIN" in executed_sql


def test_downgrade_removes_sync_schema_in_dependency_order(monkeypatch) -> None:
    migration = _migration()
    op = _Op(duplicate_scope_count=0)
    monkeypatch.setattr(migration, "op", op)

    migration.downgrade()

    dropped_tables = [args[0] for name, (args, _kwargs) in op.actions if name == "drop_table"]
    assert dropped_tables == [
        "dingtalk_org_sync_notification",
        "dingtalk_org_sync_item",
        "dingtalk_org_sync_batch",
    ]
    dropped_columns = {
        (args[0], args[1]) for name, (args, _kwargs) in op.actions if name == "drop_column"
    }
    assert dropped_columns == {
        ("app_user", "dingtalk_user_id_hash"),
        ("org_unit", "dingtalk_dept_id"),
    }
    dropped_constraints = {
        args[0] for name, (args, _kwargs) in op.actions if name == "drop_constraint"
    }
    assert dropped_constraints >= {
        "uq_user_review_scope_org_department",
        "uq_app_user_dingtalk_user_id_hash",
        "uq_org_unit_dingtalk_dept_id",
        "ck_org_unit_dingtalk_dept_id_positive",
    }
    assert set(op.connection.dropped_schema_items) == {
        "dingtalk_org_sync_item_status",
        "dingtalk_org_sync_item_kind",
        "dingtalk_org_sync_batch_status",
        "dingtalk_org_sync_action",
        "dingtalk_org_sync_trigger",
    }


def test_backfill_rejects_duplicate_legacy_reviewer_bindings(monkeypatch) -> None:
    migration = _migration()
    op = _Op(
        duplicate_scope_count=0,
        legacy_rows=[
            {
                "id": 1,
                "employee_id": None,
                "dingtalk_user_id": "cipher-1",
                "employee_hash": None,
            },
            {
                "id": 2,
                "employee_id": None,
                "dingtalk_user_id": "cipher-2",
                "employee_hash": None,
            },
        ],
    )
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(
        migration,
        "_decrypt_legacy_pii_v1",
        lambda _value, *, key: "same-provider-user",
    )
    monkeypatch.setattr(
        migration,
        "get_settings",
        lambda: type("_Settings", (), {"encryption_key": "migration-test-key"})(),
    )

    with pytest.raises(RuntimeError, match="duplicate existing DingTalk"):
        migration._backfill_dingtalk_user_hashes()


def test_backfill_rejects_multiple_provider_identities_for_one_employee(monkeypatch) -> None:
    migration = _migration()
    op = _Op(
        duplicate_scope_count=0,
        legacy_rows=[
            {
                "id": 1,
                "employee_id": 7,
                "dingtalk_user_id": "cipher-1",
                "employee_hash": None,
            },
            {
                "id": 2,
                "employee_id": 7,
                "dingtalk_user_id": "cipher-2",
                "employee_hash": None,
            },
        ],
    )
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(
        migration,
        "_decrypt_legacy_pii_v1",
        lambda value, *, key: str(value),
    )
    monkeypatch.setattr(
        migration,
        "get_settings",
        lambda: type("_Settings", (), {"encryption_key": "migration-test-key"})(),
    )

    with pytest.raises(RuntimeError, match="multiple DingTalk.*one employee"):
        migration._backfill_dingtalk_user_hashes()

    assert not any(statement.startswith("UPDATE") for statement in op.connection.statements)


def test_backfill_rejects_provider_identity_owned_by_another_employee(
    monkeypatch,
) -> None:
    migration = _migration()
    monkeypatch.setattr(
        migration,
        "get_settings",
        lambda: type("_Settings", (), {"encryption_key": "migration-test-key"})(),
    )
    digest = migration._blind_index_dingtalk_user_id_v1("provider-user-1", key="migration-test-key")
    op = _Op(
        duplicate_scope_count=0,
        legacy_rows=[
            {
                "id": 1,
                "employee_id": 8,
                "dingtalk_user_id": "cipher-1",
                "employee_hash": None,
            }
        ],
        employee_rows=[{"id": 7, "dingtalk_user_id_hash": digest}],
    )
    monkeypatch.setattr(migration, "op", op)
    monkeypatch.setattr(
        migration,
        "_decrypt_legacy_pii_v1",
        lambda _value, *, key: "provider-user-1",
    )

    with pytest.raises(RuntimeError, match="owned by another employee"):
        migration._backfill_dingtalk_user_hashes()

    assert not any(statement.startswith("UPDATE") for statement in op.connection.statements)
