"""Structural regression tests for the S13f PostgreSQL migration.

The normal ORM test database uses ``Base.metadata.create_all()``, so it cannot
exercise Alembic's data-recovery SQL.  These tests keep the critical upgrade and
downgrade ordering visible even when a PostgreSQL service is unavailable.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from alembic import command


class _Op:
    def __init__(self) -> None:
        self.actions: list[tuple[str, object]] = []

    def add_column(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("add_column", args))

    def create_table(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("create_table", args))

    def drop_table(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("drop_table", args))

    def alter_column(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("alter_column", args))

    def drop_constraint(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("drop_constraint", args))

    def create_unique_constraint(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("create_unique_constraint", args))

    def create_index(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("create_index", args))

    def drop_index(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("drop_index", args))

    def drop_column(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("drop_column", args))

    def execute(self, statement: object) -> None:
        self.actions.append(("execute", statement))


def _migration() -> object:
    path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "c8f31a7d9e24_s13f_review_round_versions_and_correction_permission.py"
    )
    spec = importlib.util.spec_from_file_location("payroll_round_migration", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_upgrade_recovers_legacy_rounds_without_unlocking_locked_batches(monkeypatch) -> None:
    migration = _migration()
    op = _Op()
    monkeypatch.setattr(migration, "op", op)

    migration.upgrade()

    statements = "\n".join(str(value) for action, value in op.actions if action == "execute")
    assert "MAX(result.batch_version)" in statements
    assert "result.org_unit_id = confirmation.org_unit_id" in statements
    assert "result.department = confirmation.department" in statements
    assert "map multi-round confirmation scopes before upgrading" in statements
    assert "confirmations without matching result evidence" in statements
    assert "disputes without matching result evidence" in statements
    assert "SET version =" in statements
    assert "batch.status = 'LOCKED'" in statements
    assert "SET status = 'DRAFT'" in statements
    assert "batch.status <> 'LOCKED'" in statements
    assert "'FINANCE', 'payroll:approve'" not in statements
    assert any(
        action == "create_table" and value[0] == "payroll_batch_s13f_recovery"
        for action, value in op.actions
    )
    assert "RAISE EXCEPTION" in statements


def test_upgrade_repairs_deployed_s13c_schema_before_reading_round_columns(monkeypatch) -> None:
    migration = _migration()
    op = _Op()
    monkeypatch.setattr(migration, "op", op)

    migration.upgrade()

    statements = [str(value) for action, value in op.actions if action == "execute"]
    repair_index = next(
        index
        for index, statement in enumerate(statements)
        if "ADD COLUMN IF NOT EXISTS batch_version" in statement
    )
    first_round_read_index = next(
        index
        for index, statement in enumerate(statements)
        if "COUNT(DISTINCT result.batch_version)" in statement
    )

    assert repair_index < first_round_read_index
    assert "ADD COLUMN IF NOT EXISTS rule_version" in statements[repair_index]
    assert "ADD COLUMN IF NOT EXISTS input_snapshot" in statements[repair_index]
    assert "SET batch_version = 1" in statements[repair_index]
    assert "SET rule_version = 'legacy-pre-s13c'" in statements[repair_index]
    assert "SET input_snapshot = '{}'::jsonb" in statements[repair_index]


def test_downgrade_is_explicitly_forward_only(monkeypatch) -> None:
    migration = _migration()
    op = _Op()
    monkeypatch.setattr(migration, "op", op)

    with pytest.raises(RuntimeError, match="forward-only"):
        migration.downgrade()

    assert op.actions == []


def _alembic_config_with_connection(connection) -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.attributes["connection"] = connection
    return config


def _create_s13f_legacy_schema(
    connection,
    schema: str,
    *,
    ambiguous_dispute: bool = False,
    missing_result_audit_columns: bool = False,
) -> None:
    """Create the b61-shaped subset C8 operates on in an isolated schema."""

    connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    connection.exec_driver_sql(f'SET search_path TO "{schema}", public')
    for statement in (
        """
        CREATE TABLE payroll_batch (
            id BIGINT PRIMARY KEY,
            status VARCHAR(32) NOT NULL,
            locked_at TIMESTAMPTZ NULL,
            locked_by BIGINT NULL,
            version BIGINT NOT NULL
        )
        """,
        f"""
        CREATE TABLE payroll_result (
            id BIGINT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            employee_id BIGINT NOT NULL,
            {'' if missing_result_audit_columns else 'batch_version BIGINT NOT NULL,'}
            version INTEGER NOT NULL,
            org_unit_id BIGINT NULL,
            department VARCHAR(32) NOT NULL,
            CONSTRAINT uq_result_batch_emp_ver UNIQUE (batch_id, employee_id, version)
        )
        """,
        """
        CREATE TABLE batch_confirmation (
            id BIGINT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            org_unit_id BIGINT NOT NULL,
            department VARCHAR(32) NOT NULL,
            CONSTRAINT uq_confirm_scope UNIQUE (batch_id, org_unit_id, department)
        )
        """,
        """
        CREATE TABLE comp_dispute (
            id BIGINT PRIMARY KEY,
            batch_id BIGINT NOT NULL,
            employee_id BIGINT NOT NULL
        )
        """,
        """
        CREATE TABLE permission (
            id BIGSERIAL PRIMARY KEY,
            code VARCHAR(64) NOT NULL UNIQUE,
            name VARCHAR(128) NOT NULL
        )
        """,
        """
        CREATE TABLE "role" (
            id BIGINT PRIMARY KEY,
            code VARCHAR(32) NOT NULL UNIQUE
        )
        """,
        """
        CREATE TABLE role_permission (
            role_id BIGINT NOT NULL,
            permission_id BIGINT NOT NULL,
            PRIMARY KEY (role_id, permission_id)
        )
        """,
        "CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)",
    ):
        connection.execute(text(statement))

    connection.execute(text("""
            INSERT INTO payroll_batch (id, status, locked_at, locked_by, version)
            VALUES (1, 'LOCKED', CURRENT_TIMESTAMP, 77, 2)
            """))
    if missing_result_audit_columns:
        connection.execute(text("""
                INSERT INTO payroll_result
                    (id, batch_id, employee_id, version, org_unit_id, department)
                VALUES (11, 1, 101, 1, 10, 'OTHER')
                """))
    else:
        connection.execute(text("""
                INSERT INTO payroll_result
                    (id, batch_id, employee_id, batch_version, version, org_unit_id, department)
                VALUES (11, 1, 101, 1, 1, 10, 'OTHER')
                """))
    if ambiguous_dispute:
        connection.execute(text("""
                INSERT INTO payroll_result
                    (id, batch_id, employee_id, batch_version, version, org_unit_id, department)
                VALUES (12, 1, 101, 2, 2, 10, 'OTHER')
                """))
    connection.execute(text("""
            INSERT INTO batch_confirmation (id, batch_id, org_unit_id, department)
            VALUES (21, 1, 10, 'OTHER')
            """))
    connection.execute(
        text("INSERT INTO comp_dispute (id, batch_id, employee_id) VALUES (31, 1, 101)")
    )
    connection.execute(text("""
            INSERT INTO "role" (id, code)
            VALUES
                (1, 'SUPER_ADMIN'), (2, 'GROUP_HR'), (3, 'FINANCE'),
                (4, 'REGION_MANAGER'), (5, 'STORE_MANAGER'), (6, 'AUDITOR')
            """))
    connection.execute(text("""
            INSERT INTO permission (id, code, name)
            VALUES
                (10, 'payroll:approve', 'approve'),
                (11, 'payroll:review', 'review')
            """))
    connection.execute(text("""
            INSERT INTO role_permission (role_id, permission_id)
            VALUES (3, 10), (1, 11), (2, 11)
            """))
    connection.execute(text("INSERT INTO alembic_version (version_num) VALUES ('b61e4a9037f2')"))
    connection.commit()


def _drop_schema(connection, schema: str) -> None:
    connection.rollback()
    connection.exec_driver_sql("SET search_path TO public")
    connection.exec_driver_sql(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
    connection.commit()


@pytest.mark.usefixtures("pg_engine")
def test_real_postgresql_upgrade_maps_valid_legacy_review_rounds(pg_engine) -> None:
    schema = f"s13f_upgrade_{uuid4().hex}"
    with pg_engine.connect() as connection:
        _create_s13f_legacy_schema(connection, schema)
        try:
            command.upgrade(_alembic_config_with_connection(connection), "c8f31a7d9e24")

            assert (
                connection.scalar(
                    text("SELECT batch_version FROM batch_confirmation WHERE id = 21")
                )
                == 1
            )
            assert (
                connection.scalar(text("SELECT batch_version FROM comp_dispute WHERE id = 31")) == 1
            )
            assert connection.execute(
                text("SELECT status, version FROM payroll_batch WHERE id = 1")
            ).one() == ("LOCKED", 1)
            assert connection.execute(text("""
                    SELECT status, version
                    FROM payroll_batch_s13f_recovery
                    WHERE batch_id = 1
                    """)).one() == ("LOCKED", 2)
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "c8f31a7d9e24"
            )
            assert connection.scalar(text("""
                    SELECT COUNT(*)
                    FROM role_permission rp
                    JOIN "role" r ON r.id = rp.role_id
                    JOIN permission p ON p.id = rp.permission_id
                    WHERE (r.code = 'FINANCE' AND p.code = 'payroll:approve')
                       OR (r.code IN ('SUPER_ADMIN', 'GROUP_HR') AND p.code = 'payroll:review')
                    """)) == 0
            assert connection.scalar(text("""
                    SELECT COUNT(*)
                    FROM role_permission rp
                    JOIN "role" r ON r.id = rp.role_id
                    JOIN permission p ON p.id = rp.permission_id
                    WHERE r.code = 'GROUP_HR' AND p.code = 'payroll:correct'
                    """)) == 1
        finally:
            _drop_schema(connection, schema)


@pytest.mark.usefixtures("pg_engine")
def test_real_postgresql_upgrade_repairs_pre_audit_payroll_result_schema(pg_engine) -> None:
    """Databases stamped at S13c may predate its audit-column definition."""

    schema = f"s13f_schema_drift_{uuid4().hex}"
    with pg_engine.connect() as connection:
        _create_s13f_legacy_schema(connection, schema, missing_result_audit_columns=True)
        try:
            command.upgrade(_alembic_config_with_connection(connection), "c8f31a7d9e24")

            assert connection.execute(text("""
                    SELECT batch_version, rule_version, input_snapshot
                    FROM payroll_result
                    WHERE id = 11
                    """)).one() == (1, "legacy-pre-s13c", {})
            assert (
                connection.scalar(text("SELECT version_num FROM alembic_version")) == "c8f31a7d9e24"
            )
        finally:
            _drop_schema(connection, schema)


@pytest.mark.usefixtures("pg_engine")
def test_real_postgresql_upgrade_rejects_ambiguous_legacy_dispute(pg_engine) -> None:
    schema = f"s13f_ambiguous_{uuid4().hex}"
    with pg_engine.connect() as connection:
        _create_s13f_legacy_schema(connection, schema, ambiguous_dispute=True)
        try:
            with pytest.raises(DBAPIError, match="map affected disputes before upgrading"):
                command.upgrade(_alembic_config_with_connection(connection), "c8f31a7d9e24")
        finally:
            _drop_schema(connection, schema)
