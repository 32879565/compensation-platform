from __future__ import annotations

import importlib.util
from pathlib import Path

from app.auth.service import Principal, resolve_payroll_read_scope, resolve_review_scope
from app.models.employee import Department


class _Result:
    def __init__(self, rows: list[tuple[int, Department]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[int, Department]]:
        return self._rows

    def first(self) -> tuple[int, Department] | None:
        return self._rows[0] if self._rows else None


class _Session:
    def __init__(
        self, rows: list[tuple[int, Department]], *, has_global_payroll_read: bool = False
    ) -> None:
        self._rows = rows
        self._has_global_payroll_read = has_global_payroll_read
        self.execute_calls = 0

    def execute(self, statement: object) -> _Result:
        self.execute_calls += 1
        if "role_permission" in str(statement):
            return _Result([(1,)] if self._has_global_payroll_read else [])
        return _Result(self._rows)


def test_resolve_review_scope_requires_assignments_for_global_principal() -> None:
    session = _Session([])
    principal = Principal(1, "global", frozenset(), None)

    assert resolve_review_scope(session, principal) == frozenset()
    assert session.execute_calls == 1


def test_resolve_review_scope_does_not_inherit_other_global_role_scope() -> None:
    """A global Finance/Auditor role must not bypass Store Manager assignments."""
    session = _Session([(10, Department.DINING)])
    principal = Principal(1, "finance-plus-store", frozenset(), None)

    assert resolve_review_scope(session, principal) == frozenset({(10, Department.DINING)})
    assert session.execute_calls == 1


def test_resolve_payroll_read_scope_remains_global_for_finance() -> None:
    session = _Session([], has_global_payroll_read=True)
    principal = Principal(1, "finance", frozenset(), None)

    assert resolve_payroll_read_scope(session, principal) is None
    assert session.execute_calls == 1


def test_resolve_review_scope_returns_only_explicit_assignments() -> None:
    session = _Session([(10, Department.DINING), (10, Department.DINING), (11, Department.KITCHEN)])
    principal = Principal(2, "reviewer", frozenset(), frozenset({10, 11, 12}))

    assert resolve_review_scope(session, principal) == frozenset(
        {(10, Department.DINING), (11, Department.KITCHEN)}
    )


def test_resolve_review_scope_fails_closed_without_assignments() -> None:
    session = _Session([])
    principal = Principal(3, "unassigned", frozenset(), frozenset({10}))

    assert resolve_review_scope(session, principal) == frozenset()


class _MigrationOp:
    def __init__(self) -> None:
        self.executed: list[object] = []

    def create_table(self, *_args: object, **_kwargs: object) -> None:
        pass

    def create_index(self, *_args: object, **_kwargs: object) -> None:
        pass

    def execute(self, statement: object) -> None:
        self.executed.append(statement)


def _load_migration_module() -> object:
    migration_path = (
        Path(__file__).resolve().parents[1]
        / "alembic"
        / "versions"
        / "a3f6c9d5b1e7_s13d_reviewer_scope_rbac.py"
    )
    spec = importlib.util.spec_from_file_location("reviewer_scope_migration", migration_path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reviewer_scope_migration_data_inserts_are_idempotent(monkeypatch) -> None:
    migration = _load_migration_module()
    op = _MigrationOp()
    monkeypatch.setattr(migration, "op", op)

    migration.upgrade()

    statements = [str(statement) for statement in op.executed]
    assert len(statements) == 2
    assert "INSERT INTO permission" in statements[0]
    assert "ON CONFLICT (code)" in statements[0]
    assert "INSERT INTO role_permission" in statements[1]
    assert "ON CONFLICT (role_id, permission_id) DO NOTHING" in statements[1]
    for role_code in ("REGION_MANAGER", "STORE_MANAGER"):
        assert role_code in statements[1]
    assert "SUPER_ADMIN" not in statements[1]
    assert "GROUP_HR" not in statements[1]
