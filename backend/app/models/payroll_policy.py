"""Effective-dated policy records for social insurance, housing and IIT.

Rates and brackets intentionally live as validated JSON payloads.  A finalized
row is immutable at the API boundary and payroll stores its full payload in
the result input snapshot, preserving reproducibility when a later policy is
introduced for the same city.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, false
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.grade import MONEY


class PayrollPolicy(Base, TimestampMixin):
    __tablename__ = "payroll_policy"
    __table_args__ = (
        UniqueConstraint("city", "effective_from", name="uq_payroll_policy_city_effective_from"),
    )

    city: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    social_rules: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    monthly_basic_deduction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_brackets: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    is_finalized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    finalized_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EmployeeTaxDeduction(Base, TimestampMixin):
    """Employee-declared monthly special deduction, effective dated and auditable."""

    __tablename__ = "employee_tax_deduction"
    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "effective_from",
            name="uq_employee_tax_deduction_employee_effective_from",
        ),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    monthly_special_deduction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
