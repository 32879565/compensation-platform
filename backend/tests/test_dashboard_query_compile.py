"""Exercise dashboard SQL construction without requiring a local Postgres daemon."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy.dialects import postgresql

from app.auth.service import Principal
from app.routers.dashboard import get_dashboard


class _Result:
    def __init__(self, value):
        self.value = value

    def one(self):
        return self.value

    def all(self):
        return self.value


class _CompilingSession:
    def __init__(self) -> None:
        self.sql: list[str] = []
        self._responses = [
            (2, Decimal("320"), Decimal("288"), Decimal("160")),
            (2, Decimal("330")),
            [(10, "STORE", "Store", 2, Decimal("320"), Decimal("160"))],
            [("2026-07", 2, Decimal("320"))],
            [(date(2026, 7, 1), 10, Decimal("330"))],
        ]
        self.added: list[object] = []

    def execute(self, statement):
        self.sql.append(str(statement.compile(dialect=postgresql.dialect())))
        return _Result(self._responses.pop(0))

    def add(self, entry: object) -> None:
        self.added.append(entry)

    def flush(self) -> None:
        pass

    def commit(self) -> None:
        pass


def test_dashboard_builds_postgresql_aggregates_and_current_result_filters(monkeypatch) -> None:
    # This focused unit test validates aggregate SQL only; permission-scope
    # resolution has its own integration coverage and performs extra queries.
    monkeypatch.setattr(
        "app.routers.dashboard.resolve_permission_org_scope",
        lambda _session, _principal, _permission: None,
    )
    session = _CompilingSession()
    response = get_dashboard(
        period="2026-07",
        principal=Principal(user_id=1, username="analyst", permissions=frozenset(), org_scope=None),
        session=session,  # type: ignore[arg-type]
    )

    assert response.metrics.actual_gross == Decimal("320")
    assert response.store_ranking[0].cost_variance == Decimal("-10")
    assert len(session.sql) == 5
    aggregate_sql = session.sql[0]
    assert "payroll_batch.status = %(status_1)s" in aggregate_sql
    assert "payroll_result.batch_version = payroll_batch.version" in aggregate_sql
    assert "max(payroll_result_1.version)" in aggregate_sql
    assert session.added  # dashboard reads are auditable too
