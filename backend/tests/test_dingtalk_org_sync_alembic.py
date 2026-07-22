"""Real PostgreSQL coverage for the D20 encrypted identity backfill."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text

from alembic import command
from app.core.config import get_settings
from app.core.crypto import encrypt_pii
from app.dingtalk.read_sync import blind_index_dingtalk_user_id

_D19_REVISION = "h3q6k9m1p457"
_SYNC_ENUM_NAMES = (
    "dingtalk_org_sync_batch_status",
    "dingtalk_org_sync_trigger",
    "dingtalk_org_sync_item_kind",
    "dingtalk_org_sync_action",
    "dingtalk_org_sync_item_status",
)


def _config_with_connection(connection) -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.attributes["connection"] = connection
    return config


def _store(connection, code: str) -> int:
    return connection.scalar(
        text("""
            INSERT INTO org_unit (code, name, type, city)
            VALUES (:code, :name, 'STORE', 'Guangzhou')
            RETURNING id
            """),
        {"code": code, "name": f"{code} Store"},
    )


def _employee(connection, *, org_id: int, emp_no: str, digest: str | None = None) -> int:
    return connection.scalar(
        text("""
            INSERT INTO employee (
                emp_no, name, org_unit_id, employment_type, status, hire_date,
                dingtalk_user_id_hash
            )
            VALUES (
                :emp_no, :name, :org_id, 'FULL_TIME', 'ACTIVE', '2026-01-01',
                :digest
            )
            RETURNING id
            """),
        {
            "emp_no": emp_no,
            "name": f"Employee {emp_no}",
            "org_id": org_id,
            "digest": digest,
        },
    )


def test_d20_backfills_encrypted_legacy_reviewer_identity_on_postgresql(pg_engine) -> None:
    schema = f"d20_identity_success_{uuid4().hex}"
    provider_user_id = "legacy-provider-user-success"
    expected_digest = blind_index_dingtalk_user_id(
        provider_user_id,
        key=get_settings().encryption_key,
    )
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, _D19_REVISION)

            org_id = _store(connection, "D20-SUCCESS")
            employee_id = _employee(connection, org_id=org_id, emp_no="D20-E1")
            user_id = connection.scalar(
                text("""
                    INSERT INTO app_user (
                        username, password_hash, employee_id, dingtalk_user_id
                    )
                    VALUES ('d20-success-user', 'not-a-real-login', :employee_id, :ciphertext)
                    RETURNING id
                    """),
                {
                    "employee_id": employee_id,
                    "ciphertext": encrypt_pii(provider_user_id),
                },
            )
            connection.commit()

            command.upgrade(config, "head")

            batch_columns = dict(
                connection.execute(
                    text("""
                        SELECT column_name, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'dingtalk_org_sync_batch'
                        """),
                    {"schema": schema},
                ).all()
            )
            assert batch_columns["requested_by_user_id"] == "YES"
            assert batch_columns["trigger"] == "NO"
            assert batch_columns["root_config_hash"] == "NO"
            assert batch_columns["local_baseline_hash"] == "NO"
            assert batch_columns["last_checked_at"] == "YES"
            assert {
                "remote_region_count",
                "local_region_count",
                "ready_region_count",
                "region_conflict_count",
                "warning_count",
            } <= batch_columns.keys()
            item_columns = dict(
                connection.execute(
                    text("""
                        SELECT column_name, is_nullable
                        FROM information_schema.columns
                        WHERE table_schema = :schema
                          AND table_name = 'dingtalk_org_sync_item'
                        """),
                    {"schema": schema},
                ).all()
            )
            assert item_columns["action"] == "NO"
            assert item_columns["change_fields"] == "NO"
            assert item_columns["proposed_org_type"] == "YES"
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM pg_enum
                    JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
                    JOIN pg_namespace ON pg_namespace.oid = pg_type.typnamespace
                    WHERE pg_namespace.nspname = :schema
                      AND pg_type.typname = 'dingtalk_org_sync_item_kind'
                      AND pg_enum.enumlabel = 'REGION'
                    """),
                    {"schema": schema},
                )
                == 1
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                      AND table_name = 'dingtalk_org_sync_notification'
                    """),
                    {"schema": schema},
                )
                == 1
            )

            assert (
                connection.scalar(
                    text("SELECT dingtalk_user_id_hash FROM app_user WHERE id = :id"),
                    {"id": user_id},
                )
                == expected_digest
            )
            local_sync_enum_names = set(
                connection.scalars(
                    text("""
                        SELECT pg_type.typname
                        FROM pg_type
                        JOIN pg_namespace ON pg_namespace.oid = pg_type.typnamespace
                        WHERE pg_namespace.nspname = :schema
                          AND pg_type.typname = ANY(:enum_names)
                        """),
                    {"schema": schema, "enum_names": list(_SYNC_ENUM_NAMES)},
                ).all()
            )
            assert local_sync_enum_names == set(_SYNC_ENUM_NAMES)

            scheduled_reuse_index_columns = connection.scalars(
                text("""
                    SELECT table_attribute.attname
                    FROM pg_class AS index_class
                    JOIN pg_namespace AS index_namespace
                      ON index_namespace.oid = index_class.relnamespace
                    JOIN pg_index AS index_metadata
                      ON index_metadata.indexrelid = index_class.oid
                    JOIN LATERAL unnest(index_metadata.indkey) WITH ORDINALITY
                      AS indexed_column(attnum, position) ON TRUE
                    JOIN pg_attribute AS table_attribute
                      ON table_attribute.attrelid = index_metadata.indrelid
                     AND table_attribute.attnum = indexed_column.attnum
                    WHERE index_namespace.nspname = :schema
                      AND index_class.relname = :index_name
                    ORDER BY indexed_column.position
                    """),
                {
                    "schema": schema,
                    "index_name": "ix_dingtalk_org_sync_batch_scheduled_reuse",
                },
            ).all()
            assert scheduled_reuse_index_columns == [
                "trigger",
                "status",
                "root_config_hash",
                "snapshot_hash",
                "local_baseline_hash",
                "expires_at",
                "id",
            ]

            connection.commit()
            command.downgrade(config, _D19_REVISION)

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                _D19_REVISION
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass(:index_name)"),
                    {"index_name": f"{schema}.ix_dingtalk_org_sync_batch_scheduled_reuse"},
                )
                is None
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM pg_type
                    JOIN pg_namespace ON pg_namespace.oid = pg_type.typnamespace
                    WHERE pg_namespace.nspname = :schema
                      AND pg_type.typname = ANY(:enum_names)
                    """),
                    {"schema": schema, "enum_names": list(_SYNC_ENUM_NAMES)},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM pg_type
                    JOIN pg_namespace ON pg_namespace.oid = pg_type.typnamespace
                    WHERE pg_namespace.nspname = 'public'
                      AND pg_type.typname = ANY(:enum_names)
                    """),
                    {"enum_names": list(_SYNC_ENUM_NAMES)},
                )
                == len(_SYNC_ENUM_NAMES)
            )
            assert (
                connection.scalar(
                    text("SELECT dingtalk_user_id_hash FROM employee WHERE id = :id"),
                    {"id": employee_id},
                )
                == expected_digest
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_d20_identity_conflict_rolls_back_schema_and_data(pg_engine) -> None:
    schema = f"d20_identity_conflict_{uuid4().hex}"
    provider_user_id = "legacy-provider-user-conflict"
    expected_digest = blind_index_dingtalk_user_id(
        provider_user_id,
        key=get_settings().encryption_key,
    )
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, _D19_REVISION)

            org_id = _store(connection, "D20-CONFLICT")
            _employee(
                connection,
                org_id=org_id,
                emp_no="D20-OWNER",
                digest=expected_digest,
            )
            target_employee_id = _employee(
                connection,
                org_id=org_id,
                emp_no="D20-TARGET",
            )
            connection.execute(
                text("""
                    INSERT INTO app_user (
                        username, password_hash, employee_id, dingtalk_user_id
                    )
                    VALUES ('d20-conflict-user', 'not-a-real-login', :employee_id, :ciphertext)
                    """),
                {
                    "employee_id": target_employee_id,
                    "ciphertext": encrypt_pii(provider_user_id),
                },
            )
            connection.commit()

            with pytest.raises(RuntimeError, match="owned by another employee"):
                command.upgrade(config, "head")

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                _D19_REVISION
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM information_schema.columns
                    WHERE table_schema = :schema
                      AND table_name = 'app_user'
                      AND column_name = 'dingtalk_user_id_hash'
                    """),
                    {"schema": schema},
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("SELECT dingtalk_user_id_hash FROM employee " "WHERE id = :employee_id"),
                    {"employee_id": target_employee_id},
                )
                is None
            )
            assert (
                connection.scalar(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": f"{schema}.dingtalk_org_sync_batch"},
                )
                is None
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


@pytest.mark.parametrize("identity_state", ["mismatched", "hash_only"])
def test_d20_downgrade_refuses_unrecoverable_app_user_identity(
    pg_engine,
    identity_state: str,
) -> None:
    schema = f"d20_downgrade_identity_{identity_state}_{uuid4().hex}"
    provider_user_id = f"provider-{identity_state}"
    ciphertext = encrypt_pii(provider_user_id) if identity_state == "mismatched" else None
    stored_digest = "f" * 64
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "head")
            user_id = connection.scalar(
                text("""
                    INSERT INTO app_user (
                        username, password_hash, dingtalk_user_id,
                        dingtalk_user_id_hash
                    )
                    VALUES (
                        :username, 'not-a-real-login', :ciphertext, :stored_digest
                    )
                    RETURNING id
                    """),
                {
                    "username": f"d20-downgrade-{identity_state}",
                    "ciphertext": ciphertext,
                    "stored_digest": stored_digest,
                },
            )
            connection.commit()

            with pytest.raises(RuntimeError, match="restore a pre-D20 backup"):
                command.downgrade(config, _D19_REVISION)

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "i4r7l0n2q568"
            )
            retained_identity = connection.execute(
                text("""
                    SELECT dingtalk_user_id, dingtalk_user_id_hash
                    FROM app_user
                    WHERE id = :user_id
                    """),
                {"user_id": user_id},
            ).one()
            assert retained_identity == (ciphertext, stored_digest)
            assert (
                connection.scalar(
                    text("SELECT to_regclass(:table_name)"),
                    {"table_name": f"{schema}.dingtalk_org_sync_batch"},
                )
                is not None
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_d20_downgrade_allows_ciphertext_without_d20_hash(pg_engine) -> None:
    schema = f"d20_downgrade_ciphertext_only_{uuid4().hex}"
    ciphertext = encrypt_pii("provider-ciphertext-only")
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "head")
            user_id = connection.scalar(
                text("""
                    INSERT INTO app_user (
                        username, password_hash, dingtalk_user_id,
                        dingtalk_user_id_hash
                    )
                    VALUES (
                        'd20-downgrade-ciphertext-only', 'not-a-real-login',
                        :ciphertext, NULL
                    )
                    RETURNING id
                    """),
                {"ciphertext": ciphertext},
            )
            connection.commit()

            command.downgrade(config, _D19_REVISION)

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                _D19_REVISION
            )
            assert (
                connection.scalar(
                    text("SELECT dingtalk_user_id FROM app_user WHERE id = :user_id"),
                    {"user_id": user_id},
                )
                == ciphertext
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


@pytest.mark.parametrize("d20_data_kind", ["sync_batch", "org_binding"])
def test_d20_downgrade_refuses_d20_only_rows_without_mutating_them(
    pg_engine,
    d20_data_kind: str,
) -> None:
    schema = f"d20_downgrade_data_{d20_data_kind}_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "head")

            if d20_data_kind == "sync_batch":
                public_id = uuid4().hex
                connection.execute(
                    text("""
                        INSERT INTO dingtalk_org_sync_batch (
                            public_id, snapshot_hash, root_config_hash,
                            local_baseline_hash, expires_at
                        )
                        VALUES (
                            :public_id, :snapshot_hash, :root_config_hash,
                            :local_baseline_hash, now() + interval '1 hour'
                        )
                        """),
                    {
                        "public_id": public_id,
                        "snapshot_hash": "a" * 64,
                        "root_config_hash": "b" * 64,
                        "local_baseline_hash": "c" * 64,
                    },
                )
            else:
                public_id = None
                org_id = _store(connection, "D20-DOWNGRADE-BINDING")
                connection.execute(
                    text("""
                        UPDATE org_unit
                        SET dingtalk_dept_id = 998877
                        WHERE id = :org_id
                        """),
                    {"org_id": org_id},
                )
            connection.commit()

            with pytest.raises(RuntimeError, match="restore a pre-D20 backup"):
                command.downgrade(config, _D19_REVISION)

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "i4r7l0n2q568"
            )
            if d20_data_kind == "sync_batch":
                assert (
                    connection.scalar(
                        text("""
                            SELECT count(*)
                            FROM dingtalk_org_sync_batch
                            WHERE public_id = :public_id
                            """),
                        {"public_id": public_id},
                    )
                    == 1
                )
            else:
                assert (
                    connection.scalar(
                        text("""
                            SELECT dingtalk_dept_id
                            FROM org_unit
                            WHERE id = :org_id
                            """),
                        {"org_id": org_id},
                    )
                    == 998877
                )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()
