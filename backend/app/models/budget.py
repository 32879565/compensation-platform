"""Labor-cost budget records maintained per organization and accounting month."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.grade import MONEY


class LaborBudget(Base, TimestampMixin):
    """An approved planning envelope for one organization in one calendar month.

    Actual cost remains derived from locked payroll results; this model holds
    the editable plan only.  The unique key makes accidental duplicate budget
    rows impossible while preserving a separate audit event for each change.
    """

    __tablename__ = "labor_budget"
    __table_args__ = (UniqueConstraint("org_unit_id", "period", name="uq_labor_budget_org_period"),)

    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    # Stored as the first day of the accounting month, e.g. 2026-07-01.
    period: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    headcount_budget: Mapped[int] = mapped_column(Integer, nullable=False)
    labor_cost_budget: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    # Optimistic concurrency token.  Writers must submit the version they
    # originally read; a stale write is rejected rather than overwriting a
    # colleague's budget revision.
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
