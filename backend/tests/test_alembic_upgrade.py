"""End-to-end migration-chain smoke coverage against an empty PostgreSQL schema."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session

from alembic import command
from app.importing.migrate_legacy import assert_legacy_load_revision, migrate_rows


def _config_with_connection(connection) -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.attributes["connection"] = connection
    return config


def test_supplied_connection_does_not_commit_caller_owned_transaction(pg_engine) -> None:
    """Alembic must not commit work owned by an ``engine.begin()`` caller."""

    schema = f"alembic_outer_transaction_{uuid4().hex}"
    with pg_engine.begin() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))

    try:
        with pytest.raises(RuntimeError, match="active transaction"):
            with pg_engine.begin() as connection:
                connection.execute(text(f'SET search_path TO "{schema}", public'))
                connection.execute(text("CREATE TABLE caller_owned_marker (id integer)"))

                command.upgrade(_config_with_connection(connection), "head")

        with pg_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT to_regclass(:qualified_name)"),
                    {"qualified_name": f"{schema}.caller_owned_marker"},
                )
                is None
            )
    finally:
        with pg_engine.begin() as connection:
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))


def test_fresh_legacy_runbook_loads_before_store_and_employee_backfills(pg_engine) -> None:
    """The documented S6 load order must produce linked real historical rows."""

    schema = f"legacy_runbook_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "089dead90284")
            connection.execute(text("""
                    INSERT INTO org_unit
                        (parent_id, type, name, code, city, status,
                         created_at, updated_at, created_by, is_deleted, deleted_at)
                    SELECT
                        NULL, 'GROUP', 'Runbook Group', 'RUNBOOK-GROUP', NULL, 'ACTIVE',
                        now(), now(), NULL, false, NULL
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM org_unit
                        WHERE type = 'GROUP' AND is_deleted = false
                    )
                    """))
            assert (
                connection.scalar(
                    text(
                        "SELECT count(*) FROM org_unit "
                        "WHERE type = 'GROUP' AND is_deleted = false"
                    )
                )
                == 1
            )
            connection.commit()

            with Session(bind=connection) as session:
                assert_legacy_load_revision(session)
                report = migrate_rows(
                    session,
                    [
                        {
                            "月份": "2026-06",
                            "姓名": "Runbook Person One",
                            "门店": "历史门店甲",
                            "标准门店": "历史门店甲",
                            "职位": "服务员",
                            "综合薪资": "4200.00",
                            "合计工资": "4200.00",
                        },
                        {
                            "月份": "2026-06",
                            "姓名": "Runbook Person Two",
                            "门店": "历史门店乙",
                            "标准门店": "历史门店乙",
                            "职位": "厨工",
                            "综合薪资": "5000.00",
                            "合计工资": "5000.00",
                        },
                    ],
                )
                assert report.written == 2
                session.commit()

            command.upgrade(config, "head")

            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE org_unit_id IS NOT NULL")
                )
                == 2
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE employee_id IS NOT NULL")
                )
                == 2
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM employee WHERE emp_no LIKE 'LEGACY-NAME-%'")
                )
                == 2
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_upgrade_head_from_empty_postgresql_schema(pg_engine) -> None:
    """A fresh deployment must be able to run every migration in order."""

    schema = f"alembic_smoke_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)

            command.upgrade(config, "head")

            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                ScriptDirectory.from_config(config).get_current_head()
            )
            assert connection.scalar(text("""
                    SELECT EXISTS (
                        SELECT 1
                        FROM pg_trigger
                        WHERE tgrelid = 'approval_action'::regclass
                          AND tgname = 'approval_action_no_update_delete'
                          AND NOT tgisinternal
                    )
                    """))
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_d15_backfills_and_protects_dispute_events(pg_engine) -> None:
    """Legacy disputes gain a minimal event trail that cannot be rewritten."""

    schema = f"alembic_dispute_events_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "c8k1f4h6j902")

            org_id = connection.scalar(text("""
                    INSERT INTO org_unit (code, name, type, city)
                    VALUES ('D15-ORG', 'D15 Store', 'STORE', 'Guangzhou')
                    RETURNING id
                    """))
            user_id = connection.scalar(text("""
                    INSERT INTO app_user (username, password_hash)
                    VALUES ('d15-dispute-user', 'not-a-real-login')
                    RETURNING id
                    """))
            employee_id = connection.scalar(
                text("""
                    INSERT INTO employee (
                        emp_no, name, org_unit_id, employment_type, status, hire_date
                    )
                    VALUES ('D15-E1', 'Legacy employee', :org_id, 'FULL_TIME', 'ACTIVE',
                            '2026-01-01')
                    RETURNING id
                    """),
                {"org_id": org_id},
            )
            batch_id = connection.scalar(text("""
                    INSERT INTO payroll_batch (
                        period, attendance_start, attendance_end, status, version
                    )
                    VALUES ('2026-05', '2026-05-01', '2026-05-31', 'PENDING_HR', 1)
                    RETURNING id
                    """))
            dispute_id = connection.scalar(
                text("""
                    INSERT INTO comp_dispute (
                        batch_id, batch_version, employee_id, salary_item, opinion,
                        raised_by, status, resolution, resolved_by, resolved_at
                    )
                    VALUES (
                        :batch_id, 1, :employee_id, 'ATTEND_WAGE', 'Legacy opinion',
                        :user_id, 'REJECTED', 'Legacy decision', :user_id, now()
                    )
                    RETURNING id
                    """),
                {
                    "batch_id": batch_id,
                    "employee_id": employee_id,
                    "user_id": user_id,
                },
            )
            connection.commit()

            command.upgrade(config, "head")

            events = connection.execute(
                text("""
                    SELECT event_type, note, actor_id
                    FROM dispute_event
                    WHERE dispute_id = :dispute_id
                    ORDER BY created_at, id
                    """),
                {"dispute_id": dispute_id},
            ).all()
            assert events == [
                ("RAISED", "Legacy opinion", user_id),
                ("REJECTED", "Legacy decision", user_id),
            ]
            event_id = connection.scalar(
                text("SELECT min(id) FROM dispute_event WHERE dispute_id = :dispute_id"),
                {"dispute_id": dispute_id},
            )
            with pytest.raises(DBAPIError, match="append-only"):
                with connection.begin_nested():
                    connection.execute(
                        text("UPDATE dispute_event SET note = 'changed' WHERE id = :id"),
                        {"id": event_id},
                    )
            with pytest.raises(DBAPIError, match="append-only"):
                with connection.begin_nested():
                    connection.execute(
                        text("DELETE FROM dispute_event WHERE id = :id"),
                        {"id": event_id},
                    )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_s8_approval_action_trigger_blocks_updates_and_deletes(pg_engine) -> None:
    """The database, not just ORM convention, keeps approval history immutable."""

    schema = f"alembic_approval_action_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "head")

            org_id = connection.scalar(text("""
                    INSERT INTO org_unit (code, name, type, city)
                    VALUES ('S8-TRIGGER-ORG', 'S8 Trigger Org', 'STORE', 'Guangzhou')
                    RETURNING id
                    """))
            user_id = connection.scalar(text("""
                    INSERT INTO app_user (username, password_hash)
                    VALUES ('s8-trigger-user', 'not-a-real-login')
                    RETURNING id
                    """))
            flow_id = connection.scalar(text("""
                    INSERT INTO approval_flow (code, name, business_type, is_active)
                    VALUES ('S8-TRIGGER-FLOW', 'S8 Trigger Flow', 'SALARY_ADJUSTMENT', true)
                    RETURNING id
                    """))
            instance_id = connection.scalar(
                text("""
                    INSERT INTO approval_instance (
                        flow_id, business_type, business_id, requester_id, org_unit_id,
                        amount, status, current_step_order, flow_snapshot, submitted_at
                    )
                    VALUES (
                        :flow_id, 'SALARY_ADJUSTMENT', 1, :user_id, :org_id,
                        100, 'PENDING', 1, '{"steps": []}'::jsonb, now()
                    )
                    RETURNING id
                    """),
                {"flow_id": flow_id, "user_id": user_id, "org_id": org_id},
            )
            action_id = connection.scalar(
                text("""
                    INSERT INTO approval_action (instance_id, step_order, action, actor_id, comment)
                    VALUES (:instance_id, 1, 'APPROVE', :user_id, 'immutable')
                    RETURNING id
                    """),
                {"instance_id": instance_id, "user_id": user_id},
            )

            with pytest.raises(DBAPIError, match="append-only"):
                with connection.begin_nested():
                    connection.execute(
                        text("UPDATE approval_action SET comment = 'changed' WHERE id = :id"),
                        {"id": action_id},
                    )
            with pytest.raises(DBAPIError, match="append-only"):
                with connection.begin_nested():
                    connection.execute(
                        text("DELETE FROM approval_action WHERE id = :id"), {"id": action_id}
                    )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_s8_upgrade_preserves_legacy_unclassified_allowances_but_enforces_new_rows(
    pg_engine,
) -> None:
    """A real pre-S8 database can upgrade without inventing allowance facts.

    The S13a column was nullable, so an existing allowance may have no known
    fixed/floating classification.  S8 must retain and visibly block that row,
    while the newly added constraint rejects any new invalid component.
    """

    schema = f"alembic_s8_legacy_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "m2d8e5c1a734")
            connection.execute(text("""
                    INSERT INTO salary_component_def
                        (code, name, component_type, taxable, in_social_base,
                         in_housing_base, allowance_kind, sort_order, is_deleted)
                    VALUES
                        ('LEGACY_ALLOWANCE', 'Legacy allowance', 'ALLOWANCE', true, false,
                         false, NULL, 0, false)
                    """))
            connection.commit()

            command.upgrade(config, "head")
            assert connection.scalar(text("""
                    SELECT COUNT(*)
                    FROM salary_component_def
                    WHERE code = 'LEGACY_ALLOWANCE'
                      AND allowance_kind IS NULL
                    """)) == 1
            assert connection.scalar(text("""
                    SELECT convalidated
                    FROM pg_constraint
                    WHERE conname = 'ck_salary_component_allowance_kind'
                      AND conrelid = 'salary_component_def'::regclass
                    """)) is False

            with pytest.raises(IntegrityError):
                connection.execute(text("""
                        INSERT INTO salary_component_def
                            (code, name, component_type, taxable, in_social_base,
                             in_housing_base, allowance_kind, sort_order, is_deleted)
                        VALUES
                            ('NEW_INVALID_ALLOWANCE', 'Invalid', 'ALLOWANCE', true, false,
                             false, NULL, 0, false)
                        """))
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_appeal_correction_work_item_migration_backfills_approved_appeals(pg_engine) -> None:
    """Existing approved appeals must not become unactionable after an upgrade."""

    schema = f"alembic_appeal_correction_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "o4d7e2f8a631")

            org_id = connection.scalar(text("""
                    INSERT INTO org_unit (code, name, type, city)
                    VALUES ('APPEAL-CORR-ORG', 'Appeal correction org', 'STORE', 'Guangzhou')
                    RETURNING id
                    """))
            user_id = connection.scalar(text("""
                    INSERT INTO app_user (username, password_hash)
                    VALUES ('appeal-correction-user', 'not-a-real-login')
                    RETURNING id
                    """))
            batch_id = connection.scalar(
                text("""
                    INSERT INTO payroll_batch
                        (period, attendance_start, attendance_end, status, version)
                    VALUES ('2026-07', '2026-06-26', '2026-07-25', 'PENDING_STORE_CONFIRM', 1)
                    RETURNING id
                    """),
            )
            delivery_id = connection.scalar(
                text("""
                    INSERT INTO dingtalk_delivery
                        (batch_id, batch_version, org_unit_id, department, recipient_user_id,
                         kind, status, error_code, attempt_count, dispatched_at, idempotency_key)
                    VALUES
                        (:batch_id, 1, :org_id, 'DINING', :user_id, 'PAYROLL_REVIEW',
                         'SANDBOXED', NULL, 1, now(), 'appeal-correction-backfill')
                    RETURNING id
                    """),
                {"batch_id": batch_id, "org_id": org_id, "user_id": user_id},
            )
            connection.execute(
                text("""
                    INSERT INTO comp_appeal
                        (delivery_id, batch_id, batch_version, org_unit_id, department,
                         employee_id, requester_id, dedupe_key, reason, status)
                    VALUES
                        (:delivery_id, :batch_id, 1, :org_id, 'DINING', NULL, :user_id,
                         'appeal-correction-backfill', 'Sensitive reason stays on appeal',
                         'CORRECTION_REQUIRED')
                    """),
                {
                    "delivery_id": delivery_id,
                    "batch_id": batch_id,
                    "org_id": org_id,
                    "user_id": user_id,
                },
            )
            connection.commit()

            command.upgrade(config, "p5e8f3a1b742")
            row = connection.execute(
                text("""
                    SELECT source_batch_version, status, employee_id, created_by
                    FROM comp_appeal_correction_work_item
                    """),
            ).one()
            assert row == (1, "PENDING_TRIAGE", None, None)

            connection.commit()
            command.downgrade(config, "o4d7e2f8a631")
            assert (
                connection.scalar(
                    text("SELECT to_regclass(:relation_name)"),
                    {"relation_name": f"{schema}.comp_appeal_correction_work_item"},
                )
                is None
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_historical_store_org_backfill_is_reversible(pg_engine) -> None:
    """Unmatched historical stores become inactive org nodes without inventing regions."""

    schema = f"alembic_historical_stores_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "p5e8f3a1b742")

            group_id = connection.scalar(text("""
                    INSERT INTO org_unit (code, name, type, status)
                    VALUES ('HIST-TEST-GROUP', 'Test Group', 'GROUP', 'ACTIVE')
                    RETURNING id
                    """))
            existing_store_id = connection.scalar(
                text("""
                    INSERT INTO org_unit (parent_id, code, name, type, status)
                    VALUES (:group_id, 'HIST-TEST-EXISTING', 'Existing Store', 'STORE', 'ACTIVE')
                    RETURNING id
                    """),
                {"group_id": group_id},
            )
            connection.execute(
                text("""
                    INSERT INTO salary_record
                        (period, name, store_name, org_unit_id, source, fields)
                    VALUES
                        ('2026-05', 'Existing Employee', 'Existing Store', :existing_id,
                         'HISTORICAL', '{}'::jsonb),
                        ('2026-05', 'Legacy Employee A', '历史一店', NULL,
                         'HISTORICAL', '{}'::jsonb),
                        ('2026-06', 'Legacy Employee A', '历史一店', NULL,
                         'HISTORICAL', '{}'::jsonb),
                        ('2026-06', 'Legacy Employee B', '历史二店', NULL,
                         'HISTORICAL', '{}'::jsonb)
                    """),
                {"existing_id": existing_store_id},
            )
            connection.commit()

            command.upgrade(config, "q6f9a2c8d753")

            region = connection.execute(text("""
                    SELECT id, parent_id, name, type, status
                    FROM org_unit
                    WHERE code = 'HIST-REGION-PENDING'
                    """)).one()
            assert region[1:] == (
                group_id,
                "历史门店（待归属）",
                "REGION",
                "HISTORICAL",
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM org_unit
                    WHERE parent_id = :region_id
                      AND type = 'STORE'
                      AND status = 'HISTORICAL'
                    """),
                    {"region_id": region.id},
                )
                == 2
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE org_unit_id IS NULL")
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM salary_record
                    WHERE store_name = 'Existing Store' AND org_unit_id = :existing_id
                    """),
                    {"existing_id": existing_store_id},
                )
                == 1
            )

            connection.commit()
            command.downgrade(config, "p5e8f3a1b742")

            assert (
                connection.scalar(
                    text("SELECT count(*) FROM org_unit WHERE code = 'HIST-REGION-PENDING'")
                )
                == 0
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE org_unit_id IS NULL")
                )
                == 3
            )
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM salary_record
                    WHERE store_name = 'Existing Store' AND org_unit_id = :existing_id
                    """),
                    {"existing_id": existing_store_id},
                )
                == 1
            )

            connection.commit()
            command.upgrade(config, "q6f9a2c8d753")
            assert (
                connection.scalar(
                    text("""
                    SELECT count(*)
                    FROM org_unit
                    WHERE parent_id = (
                        SELECT id FROM org_unit WHERE code = 'HIST-REGION-PENDING'
                    )
                    """),
                )
                == 2
            )
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE org_unit_id IS NULL")
                )
                == 0
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def test_latest_historical_names_create_reversible_provisional_employees(pg_engine) -> None:
    """The explicit name-based import creates marked employees and links their history."""

    schema = f"alembic_name_employees_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            connection.commit()
            config = _config_with_connection(connection)
            command.upgrade(config, "q6f9a2c8d753")

            group_id = connection.scalar(text("""
                    INSERT INTO org_unit (code, name, type, status)
                    VALUES ('NAME-IMPORT-GROUP', 'Name Import Group', 'GROUP', 'ACTIVE')
                    RETURNING id
                    """))
            first_store_id = connection.scalar(
                text("""
                    INSERT INTO org_unit (parent_id, code, name, type, city, status)
                    VALUES (:group_id, 'NAME-STORE-ONE', 'Store One', 'STORE',
                            'Guangzhou', 'ACTIVE')
                    RETURNING id
                    """),
                {"group_id": group_id},
            )
            second_store_id = connection.scalar(
                text("""
                    INSERT INTO org_unit (parent_id, code, name, type, city, status)
                    VALUES (:group_id, 'NAME-STORE-TWO', 'Store Two', 'STORE',
                            NULL, 'HISTORICAL')
                    RETURNING id
                    """),
                {"group_id": group_id},
            )
            connection.execute(
                text("""
                    INSERT INTO salary_record
                        (period, name, store_name, org_unit_id, source, fields)
                    VALUES
                        ('2026-05', 'Current Person', 'Store Two', :second_store_id,
                         'HISTORICAL', '{"入职日期":"2024-01-02","职位":"店员"}'::jsonb),
                        ('2026-05', 'Former Person', 'Store One', :first_store_id,
                         'HISTORICAL', '{"入职日期":"2023-03-04","职位":"店员"}'::jsonb),
                        ('2026-06', 'Current Person', 'Store One', :first_store_id,
                         'HISTORICAL', '{"入职日期":"2024-01-02","职位":"店员"}'::jsonb),
                        ('2026-06', 'Seasonal Person', 'Store Two', :second_store_id,
                         'HISTORICAL', '{"入职日期":"2026-06-01","职位":"暑假工"}'::jsonb)
                    """),
                {"first_store_id": first_store_id, "second_store_id": second_store_id},
            )
            connection.commit()

            command.upgrade(config, "r7a0b3d9e864")

            employees = {row.name: row for row in connection.execute(text("""
                    SELECT emp_no, name, org_unit_id, employment_type, status,
                           hire_date, department, position_title, is_special_position,
                           social_city
                    FROM employee
                    ORDER BY name
                    """))}
            assert set(employees) == {"Current Person", "Seasonal Person"}
            assert employees["Current Person"].emp_no.startswith("LEGACY-NAME-")
            assert employees["Current Person"].org_unit_id == first_store_id
            assert employees["Current Person"].employment_type == "FULL_TIME"
            assert employees["Current Person"].status == "ACTIVE"
            assert employees["Current Person"].hire_date == date(2024, 1, 2)
            assert employees["Current Person"].department == "OTHER"
            assert employees["Current Person"].position_title == "店员"
            assert employees["Current Person"].is_special_position is False
            assert employees["Current Person"].social_city == "Guangzhou"
            assert employees["Seasonal Person"].employment_type == "PART_TIME_HOURLY"
            assert employees["Seasonal Person"].is_special_position is True
            assert employees["Seasonal Person"].social_city is None
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE employee_id IS NOT NULL")
                )
                == 3
            )
            assert connection.scalar(text("""
                    SELECT count(*)
                    FROM salary_record
                    WHERE name = 'Former Person' AND employee_id IS NULL
                    """)) == 1

            connection.commit()
            command.downgrade(config, "q6f9a2c8d753")

            assert connection.scalar(text("SELECT count(*) FROM employee")) == 0
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE employee_id IS NOT NULL")
                )
                == 0
            )

            connection.commit()
            command.upgrade(config, "r7a0b3d9e864")
            assert connection.scalar(text("SELECT count(*) FROM employee")) == 2
            assert (
                connection.scalar(
                    text("SELECT count(*) FROM salary_record WHERE employee_id IS NOT NULL")
                )
                == 3
            )
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()
