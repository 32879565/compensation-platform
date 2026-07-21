"""Read-only, organization-scoped payroll analytics for the management dashboard."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.db.session import get_session
from app.models.budget import LaborBudget
from app.models.org import OrgType, OrgUnit
from app.models.payroll_batch import BatchStatus, PayrollBatch
from app.models.payroll_result import PayrollResult

router = APIRouter(prefix="/api/dashboard", tags=["dashboard"])


class DashboardMetrics(BaseModel):
    employee_count: int
    actual_gross: Decimal
    actual_net: Decimal
    average_gross: Decimal
    budget_headcount: int | None
    budget_cost: Decimal | None
    headcount_variance: int | None
    cost_variance: Decimal | None


class DashboardTrend(BaseModel):
    period: str
    employee_count: int
    actual_gross: Decimal
    budget_cost: Decimal | None


class StoreRank(BaseModel):
    org_unit_id: int
    org_code: str
    org_name: str
    employee_count: int
    actual_gross: Decimal
    average_gross: Decimal
    budget_cost: Decimal | None
    cost_variance: Decimal | None


class DashboardOut(BaseModel):
    period: str
    metrics: DashboardMetrics
    trend: list[DashboardTrend]
    store_ranking: list[StoreRank]


def _period_start_or_422(period: str) -> date:
    try:
        year, month = (int(value) for value in period.split("-"))
        return date(year, month, 1)
    except (TypeError, ValueError):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail="period must be a valid YYYY-MM month",
        ) from None


def _latest_result_version():
    latest = aliased(PayrollResult)
    return (
        select(func.max(latest.version))
        .where(
            latest.batch_id == PayrollResult.batch_id,
            latest.batch_version == PayrollResult.batch_version,
            latest.employee_id == PayrollResult.employee_id,
        )
        .correlate(PayrollResult)
        .scalar_subquery()
    )


def _result_conditions(period: str | None, org_scope: frozenset[int] | None):
    conditions = [
        PayrollBatch.status == BatchStatus.LOCKED,
        PayrollResult.batch_version == PayrollBatch.version,
        PayrollResult.version == _latest_result_version(),
    ]
    if period is not None:
        conditions.append(PayrollBatch.period == period)
    if org_scope is not None:
        # An empty constrained scope intentionally produces no rows.
        conditions.append(PayrollResult.org_unit_id.in_(list(org_scope)))
    return conditions


def _budget_by_store(
    session: Session, *, periods: set[str], org_scope: frozenset[int] | None
) -> dict[tuple[str, int], Decimal]:
    if not periods:
        return {}
    starts = [_period_start_or_422(period) for period in periods]
    statement = (
        select(LaborBudget.period, LaborBudget.org_unit_id, LaborBudget.labor_cost_budget)
        .join(OrgUnit, OrgUnit.id == LaborBudget.org_unit_id)
        .where(
            LaborBudget.period.in_(starts),
            OrgUnit.type == OrgType.STORE,
            OrgUnit.is_deleted.is_(False),
        )
    )
    if org_scope is not None:
        statement = statement.where(LaborBudget.org_unit_id.in_(list(org_scope)))
    return {
        (period.strftime("%Y-%m"), org_unit_id): cost
        for period, org_unit_id, cost in session.execute(statement).all()
    }


def _dashboard_metrics(
    session: Session,
    *,
    conditions,
    period_start: date,
    org_scope: frozenset[int] | None,
) -> DashboardMetrics:
    employee_count, actual_gross, actual_net, average_gross = session.execute(
        select(
            func.count(PayrollResult.id),
            func.coalesce(func.sum(PayrollResult.gross), Decimal("0")),
            func.coalesce(func.sum(PayrollResult.net), Decimal("0")),
            func.coalesce(func.avg(PayrollResult.gross), Decimal("0")),
        )
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .where(*conditions)
    ).one()
    budget_statement = (
        select(
            func.sum(LaborBudget.headcount_budget),
            func.sum(LaborBudget.labor_cost_budget),
        )
        .join(OrgUnit, OrgUnit.id == LaborBudget.org_unit_id)
        .where(
            LaborBudget.period == period_start,
            OrgUnit.type == OrgType.STORE,
            OrgUnit.is_deleted.is_(False),
        )
    )
    if org_scope is not None:
        budget_statement = budget_statement.where(LaborBudget.org_unit_id.in_(list(org_scope)))
    budget_headcount, budget_cost = session.execute(budget_statement).one()
    return DashboardMetrics(
        employee_count=employee_count,
        actual_gross=actual_gross,
        actual_net=actual_net,
        average_gross=average_gross,
        budget_headcount=budget_headcount,
        budget_cost=budget_cost,
        headcount_variance=(
            (employee_count - budget_headcount) if budget_headcount is not None else None
        ),
        cost_variance=(actual_gross - budget_cost) if budget_cost is not None else None,
    )


def _ranking_rows(session: Session, conditions):
    return session.execute(
        select(
            OrgUnit.id,
            OrgUnit.code,
            OrgUnit.name,
            func.count(PayrollResult.id),
            func.coalesce(func.sum(PayrollResult.gross), Decimal("0")),
            func.coalesce(func.avg(PayrollResult.gross), Decimal("0")),
        )
        .join(PayrollResult, PayrollResult.org_unit_id == OrgUnit.id)
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .where(*conditions)
        .group_by(OrgUnit.id, OrgUnit.code, OrgUnit.name)
        .order_by(func.sum(PayrollResult.gross).desc(), OrgUnit.id)
    ).all()


def _trend_rows(session: Session, org_scope: frozenset[int] | None):
    return session.execute(
        select(
            PayrollBatch.period,
            func.count(PayrollResult.id),
            func.coalesce(func.sum(PayrollResult.gross), Decimal("0")),
        )
        .join(PayrollBatch, PayrollBatch.id == PayrollResult.batch_id)
        .where(*_result_conditions(None, org_scope))
        .group_by(PayrollBatch.period)
        .order_by(PayrollBatch.period.desc())
        .limit(6)
    ).all()


def _build_trend(
    trend_rows, budget_by_store: dict[tuple[str, int], Decimal]
) -> list[DashboardTrend]:
    trend: list[DashboardTrend] = []
    for trend_period, count, gross in reversed(trend_rows):
        period_budget_costs = [
            cost
            for (budget_period, _org_unit_id), cost in budget_by_store.items()
            if budget_period == trend_period
        ]
        trend.append(
            DashboardTrend(
                period=trend_period,
                employee_count=count,
                actual_gross=gross,
                budget_cost=sum(period_budget_costs, Decimal("0")) if period_budget_costs else None,
            )
        )
    return trend


def _build_store_ranking(
    ranking_rows, current_budget_by_store: dict[int, Decimal]
) -> list[StoreRank]:
    return [
        StoreRank(
            org_unit_id=org_unit_id,
            org_code=org_code,
            org_name=org_name,
            employee_count=count,
            actual_gross=gross,
            average_gross=average,
            budget_cost=current_budget_by_store.get(org_unit_id),
            cost_variance=(
                (gross - current_budget_by_store[org_unit_id])
                if org_unit_id in current_budget_by_store
                else None
            ),
        )
        for org_unit_id, org_code, org_name, count, gross, average in ranking_rows
    ]


@router.get("", response_model=DashboardOut)
def get_dashboard(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    principal: Principal = Depends(require_permission(Perm.DASHBOARD_READ)),
    session: Session = Depends(get_session),
) -> DashboardOut:
    period_start = _period_start_or_422(period)
    org_scope = resolve_permission_org_scope(session, principal, Perm.DASHBOARD_READ)
    conditions = _result_conditions(period, org_scope)
    metrics = _dashboard_metrics(
        session,
        conditions=conditions,
        period_start=period_start,
        org_scope=org_scope,
    )
    ranking_rows = _ranking_rows(session, conditions)
    trend_rows = _trend_rows(session, org_scope)
    visible_periods = {row[0] for row in trend_rows}
    visible_periods.add(period)
    budget_by_store = _budget_by_store(session, periods=visible_periods, org_scope=org_scope)
    current_budget_by_store = {
        org_unit_id: cost
        for (budget_period, org_unit_id), cost in budget_by_store.items()
        if budget_period == period
    }
    response = DashboardOut(
        period=period,
        metrics=metrics,
        trend=_build_trend(trend_rows, budget_by_store),
        store_ranking=_build_store_ranking(ranking_rows, current_budget_by_store),
    )
    audit.record(
        session,
        action="dashboard.view",
        actor=(principal.user_id, principal.username),
        target_type="payroll_dashboard",
        detail={"period": period, "employee_count": metrics.employee_count},
    )
    session.commit()
    return response
