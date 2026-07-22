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
            assert connection.scalar(
                text("""
                    SELECT count(*)
                    FROM pg_enum
                    JOIN pg_type ON pg_type.oid = pg_enum.enumtypid
                    WHERE pg_type.typname = 'dingtalk_org_sync_item_kind'
                      AND pg_enum.enumlabel = 'REGION'
                    """),
            ) == 1
            assert connection.scalar(
                text("""
                    SELECT count(*)
                    FROM information_schema.tables
                    WHERE table_schema = :schema
                      AND table_name = 'dingtalk_org_sync_notification'
                    """),
                {"schema": schema},
            ) == 1

            assert (
                connection.scalar(
                    text("SELECT dingtalk_user_id_hash FROM app_user WHERE id = :id"),
                    {"id": user_id},
                )
                == expected_digest
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
