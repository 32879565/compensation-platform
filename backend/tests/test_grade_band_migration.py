"""Database contracts for grade and effective-dated salary-band integrity."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from alembic import command


def _config_with_connection(connection) -> Config:
    backend_dir = Path(__file__).resolve().parents[1]
    config = Config(str(backend_dir / "alembic.ini"))
    config.set_main_option("script_location", str(backend_dir / "alembic"))
    config.attributes["connection"] = connection
    return config


@contextmanager
def _isolated_schema(pg_engine, prefix: str) -> Iterator[tuple[object, Config]]:
    schema = f"{prefix}_{uuid4().hex}"
    with pg_engine.connect() as connection:
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
        connection.commit()
        try:
            connection.execute(text(f'SET search_path TO "{schema}", public'))
            yield connection, _config_with_connection(connection)
        finally:
            connection.rollback()
            connection.execute(text("SET search_path TO public"))
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
            connection.commit()


def _assert_integrity_error(connection, statement: str, parameters: dict | None = None) -> None:
    with pytest.raises(IntegrityError):
        with connection.begin_nested():
            connection.execute(text(statement), parameters or {})


def test_grade_band_head_enforces_database_integrity(pg_engine) -> None:
    """The database must protect grade versions and valid, unique active bands."""

    with _isolated_schema(pg_engine, "grade_band_contract") as (connection, config):
        command.upgrade(config, "head")

        grade_id, version = connection.execute(text("""
                INSERT INTO job_grade (code, name, rank)
                VALUES ('G1', 'Grade 1', 1)
                RETURNING id, version
                """)).one()
        assert version == 1

        _assert_integrity_error(
            connection,
            "INSERT INTO job_grade (code, name, rank) VALUES ('   ', 'Blank code', 2)",
        )
        _assert_integrity_error(
            connection,
            "INSERT INTO job_grade (code, name, rank) VALUES ('G-BLANK', '  ', 2)",
        )
        _assert_integrity_error(
            connection,
            "UPDATE job_grade SET version = 0 WHERE id = :grade_id",
            {"grade_id": grade_id},
        )

        connection.execute(
            text("""
                INSERT INTO salary_band
                    (job_grade_id, band_min, band_mid, band_max, effective_from)
                VALUES (:grade_id, 3000, 4000, 5000, '2026-01-01')
                """),
            {"grade_id": grade_id},
        )
        connection.execute(
            text("""
                INSERT INTO salary_band
                    (job_grade_id, band_min, band_mid, band_max, effective_from, is_deleted)
                VALUES (:grade_id, 3000, 4000, 5000, '2026-01-01', true)
                """),
            {"grade_id": grade_id},
        )
        _assert_integrity_error(
            connection,
            """
            INSERT INTO salary_band
                (job_grade_id, band_min, band_mid, band_max, effective_from)
            VALUES (:grade_id, 3100, 4100, 5100, '2026-01-01')
            """,
            {"grade_id": grade_id},
        )
        _assert_integrity_error(
            connection,
            """
            INSERT INTO salary_band
                (job_grade_id, band_min, band_mid, band_max, effective_from)
            VALUES (:grade_id, -1, 4000, 5000, '2026-02-01')
            """,
            {"grade_id": grade_id},
        )
        _assert_integrity_error(
            connection,
            """
            INSERT INTO salary_band
                (job_grade_id, band_min, band_mid, band_max, effective_from)
            VALUES (:grade_id, 5000, 4000, 6000, '2026-03-01')
            """,
            {"grade_id": grade_id},
        )
        _assert_integrity_error(
            connection,
            """
            INSERT INTO salary_band
                (job_grade_id, band_min, band_mid, band_max, effective_from)
            VALUES (:grade_id, 3000, 7000, 6000, '2026-04-01')
            """,
            {"grade_id": grade_id},
        )


@pytest.mark.parametrize(
    ("dirty_data", "message"),
    [
        ("duplicate", r"(?i)salary_band.*duplicate"),
        ("invalid", r"(?i)salary_band.*invalid"),
    ],
)
def test_grade_band_upgrade_fails_closed_on_dirty_legacy_data(
    pg_engine, dirty_data: str, message: str
) -> None:
    """A migration must report dirty legacy bands instead of choosing a winner."""

    with _isolated_schema(pg_engine, f"grade_band_dirty_{dirty_data}") as (connection, config):
        command.upgrade(config, "d9l2g5i7k013")
        grade_id = connection.scalar(text("""
                INSERT INTO job_grade (code, name, rank)
                VALUES ('LEGACY-G1', 'Legacy grade', 1)
                RETURNING id
                """))
        connection.execute(
            text("""
                INSERT INTO salary_band
                    (job_grade_id, band_min, band_mid, band_max, effective_from)
                VALUES (:grade_id, 3000, 4000, 5000, '2026-01-01')
                """),
            {"grade_id": grade_id},
        )
        if dirty_data == "duplicate":
            connection.execute(
                text("""
                    INSERT INTO salary_band
                        (job_grade_id, band_min, band_mid, band_max, effective_from)
                    VALUES (:grade_id, 3100, 4100, 5100, '2026-01-01')
                    """),
                {"grade_id": grade_id},
            )
        else:
            connection.execute(
                text("""
                    UPDATE salary_band
                    SET band_min = -1, band_mid = 6000, band_max = 5000
                    WHERE job_grade_id = :grade_id
                    """),
                {"grade_id": grade_id},
            )
        connection.commit()

        with pytest.raises(RuntimeError, match=message):
            command.upgrade(config, "head")
