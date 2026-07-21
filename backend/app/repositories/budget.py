from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.models.budget import LaborBudget
from app.repositories.base import BaseRepository, Page


class LaborBudgetRepository(BaseRepository[LaborBudget]):
    """Budget reads with the same fail-closed organization scoping as HR data."""

    model = LaborBudget

    def _apply_org_scope(self, stmt):
        if self._org_scope is None:
            return stmt
        return stmt.where(LaborBudget.org_unit_id.in_(self._org_scope))

    def list_filtered(
        self,
        *,
        org_unit_id: int | None,
        period: date | None,
        page: int,
        page_size: int,
    ) -> Page[LaborBudget]:
        page = max(1, page)
        page_size = min(500, max(1, page_size))
        statement = self._base_query()
        if org_unit_id is not None:
            statement = statement.where(LaborBudget.org_unit_id == org_unit_id)
        if period is not None:
            statement = statement.where(LaborBudget.period == period)
        statement = statement.order_by(
            LaborBudget.period.desc(), LaborBudget.org_unit_id, LaborBudget.id
        )
        total = self.session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        items = self.session.scalars(
            statement.limit(page_size).offset((page - 1) * page_size)
        ).all()
        return Page(items=items, total=total, page=page, page_size=page_size)

    def get_for_update(self, budget_id: int) -> LaborBudget | None:
        return self.session.scalars(
            self._base_query().where(LaborBudget.id == budget_id).with_for_update()
        ).first()
