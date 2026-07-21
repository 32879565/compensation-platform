"""Regression coverage for the D1 data-safety migrations."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
import sqlalchemy as sa


class _RecordingOp:
    """Small Alembic operation double that executes data SQL against SQLite."""

    def __init__(self, bind: sa.Connection) -> None:
        self._bind = bind
        self.actions: list[tuple[str, tuple[object, ...], dict[str, object]]] = []

    def get_bind(self) -> sa.Connection:
        return self._bind

    def alter_column(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("alter_column", args, kwargs))

    def drop_column(self, *args: object, **kwargs: object) -> None:
        self.actions.append(("drop_column", args, kwargs))

    def execute(self, statement: Any) -> Any:
        self.actions.append(("execute", (statement,), {}))
        return self._bind.execute(statement)


def _migration(filename: str) -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "alembic" / "versions" / filename
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("deferred_deductions", "deferred_deposit"),
    [(25, 0), (0, 600), (None, 0)],
    ids=["deductions", "deposit", "unknown-obligation"],
)
def test_carry_obligation_downgrade_refuses_to_drop_nonzero_or_unknown_values(
    monkeypatch, deferred_deductions: int | None, deferred_deposit: int
) -> None:
    migration = _migration("e7c2a84d9f10_d1_persist_payroll_carry_obligations.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("""
                    CREATE TABLE payroll_result (
                        deferred_deductions NUMERIC,
                        deferred_deposit NUMERIC
                    )
                    """))
            connection.execute(
                sa.text("""
                    INSERT INTO payroll_result (deferred_deductions, deferred_deposit)
                    VALUES (:deferred_deductions, :deferred_deposit)
                    """),
                {
                    "deferred_deductions": deferred_deductions,
                    "deferred_deposit": deferred_deposit,
                },
            )
            op = _RecordingOp(connection)
            monkeypatch.setattr(migration, "op", op)

            with pytest.raises(RuntimeError, match="deferred obligations"):
                migration.downgrade()

            assert op.actions == []
    finally:
        engine.dispose()


def test_carry_obligation_downgrade_allows_proven_zero_values(monkeypatch) -> None:
    migration = _migration("e7c2a84d9f10_d1_persist_payroll_carry_obligations.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("""
                    CREATE TABLE payroll_result (
                        deferred_deductions NUMERIC NOT NULL,
                        deferred_deposit NUMERIC NOT NULL
                    )
                    """))
            connection.execute(sa.text("""
                    INSERT INTO payroll_result (deferred_deductions, deferred_deposit)
                    VALUES (0, 0)
                    """))
            op = _RecordingOp(connection)
            monkeypatch.setattr(migration, "op", op)

            migration.downgrade()

            assert op.actions == [
                ("drop_column", ("payroll_result", "deferred_deposit"), {}),
                ("drop_column", ("payroll_result", "deferred_deductions"), {}),
            ]
    finally:
        engine.dispose()


def test_hourly_attendance_upgrade_reclassifies_only_ambiguous_legacy_zeroes(monkeypatch) -> None:
    migration = _migration("g4e9a1d7c530_d1_distinguish_missing_hourly_input.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("""
                    CREATE TABLE employee (
                        id INTEGER PRIMARY KEY,
                        department VARCHAR(32) NOT NULL,
                        is_special_position BOOLEAN NOT NULL
                    )
                    """))
            connection.execute(sa.text("""
                    CREATE TABLE attendance_record (
                        id INTEGER PRIMARY KEY,
                        employee_id INTEGER NOT NULL,
                        worked_hours NUMERIC
                    )
                    """))
            connection.execute(sa.text("""
                    INSERT INTO employee (id, department, is_special_position)
                    VALUES
                        (1, 'DINING', FALSE),
                        (2, 'KITCHEN', FALSE),
                        (3, 'OTHER', FALSE),
                        (4, 'DINING', TRUE),
                        (5, 'KITCHEN', FALSE)
                    """))
            connection.execute(sa.text("""
                    INSERT INTO attendance_record (id, employee_id, worked_hours)
                    VALUES
                        (1, 1, 0),
                        (2, 2, 0),
                        (3, 3, 0),
                        (4, 4, 0),
                        (5, 5, 12.5)
                    """))
            op = _RecordingOp(connection)
            monkeypatch.setattr(migration, "op", op)

            migration.upgrade()

            assert dict(
                connection.execute(
                    sa.text("SELECT id, worked_hours FROM attendance_record ORDER BY id")
                ).all()
            ) == {1: None, 2: None, 3: 0, 4: 0, 5: 12.5}
            action, args, kwargs = op.actions[0]
            assert action == "alter_column"
            assert args == ("attendance_record", "worked_hours")
            assert isinstance(kwargs["existing_type"], sa.Numeric)
            assert kwargs["existing_type"].precision == 6
            assert kwargs["existing_type"].scale == 2
            assert kwargs["nullable"] is True
            assert kwargs["server_default"] is None
    finally:
        engine.dispose()


@pytest.mark.parametrize(
    "unsafe_source",
    ["calendar", "holiday_date", "holiday_work", "result_detail"],
)
def test_holiday_downgrade_refuses_to_erase_source_or_result_detail(
    monkeypatch, unsafe_source: str
) -> None:
    migration = _migration("f8b3d12a6c44_d1_holiday_calendar_and_result_detail.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("""
                    CREATE TABLE payroll_result (
                        statutory_holiday_days NUMERIC,
                        statutory_holiday_worked_days NUMERIC
                    )
                    """))
            connection.execute(sa.text("CREATE TABLE holiday_calendar_period (id INTEGER)"))
            connection.execute(sa.text("CREATE TABLE statutory_holiday_date (id INTEGER)"))
            connection.execute(sa.text("CREATE TABLE holiday_work_record (id INTEGER)"))
            if unsafe_source == "calendar":
                connection.execute(sa.text("INSERT INTO holiday_calendar_period VALUES (1)"))
            elif unsafe_source == "holiday_date":
                connection.execute(sa.text("INSERT INTO statutory_holiday_date VALUES (1)"))
            elif unsafe_source == "holiday_work":
                connection.execute(sa.text("INSERT INTO holiday_work_record VALUES (1)"))
            else:
                connection.execute(sa.text("INSERT INTO payroll_result VALUES (1, 0)"))
            op = _RecordingOp(connection)
            monkeypatch.setattr(migration, "op", op)

            with pytest.raises(RuntimeError, match="statutory-holiday"):
                migration.downgrade()

            assert op.actions == []
    finally:
        engine.dispose()


@pytest.mark.parametrize("unsafe_source", ["rule", "provenance"])
def test_schedule_downgrade_refuses_to_erase_rules_or_provenance(
    monkeypatch, unsafe_source: str
) -> None:
    migration = _migration("h6f2c8b9e451_d1_expected_attendance_rules.py")
    engine = sa.create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(sa.text("CREATE TABLE expected_attendance_rule (id INTEGER)"))
            connection.execute(sa.text("""
                    CREATE TABLE attendance_record (
                        generated_expected_days NUMERIC,
                        expected_days_rule_id INTEGER
                    )
                    """))
            if unsafe_source == "rule":
                connection.execute(sa.text("INSERT INTO expected_attendance_rule VALUES (1)"))
            else:
                connection.execute(sa.text("INSERT INTO attendance_record VALUES (22, NULL)"))
            op = _RecordingOp(connection)
            monkeypatch.setattr(migration, "op", op)

            with pytest.raises(RuntimeError, match="attendance"):
                migration.downgrade()

            assert op.actions == []
    finally:
        engine.dispose()
