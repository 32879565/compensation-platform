"""Effective-dated policy records for social insurance, housing and IIT.

Rates and brackets intentionally live as validated JSON payloads.  A finalized
row is immutable at the API boundary and payroll stores its full payload in
the result input snapshot, preserving reproducibility when a later policy is
introduced for the same city.
"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
)
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
    derived_income_rules: Mapped[list[dict]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
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


class EmployeeTaxYtdOpening(Base, TimestampMixin):
    """Audited tax facts carried into the system after a mid-year migration.

    An opening is an explicitly approved replacement for unavailable earlier
    locked payroll snapshots.  It contains monthly *facts* through a named
    period; later locked results are added on top by the payroll service.
    """

    __tablename__ = "employee_tax_ytd_opening"
    __table_args__ = (
        CheckConstraint("tax_year BETWEEN 2000 AND 9999", name="ck_tax_opening_year"),
        CheckConstraint(
            "through_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name="ck_tax_opening_period",
        ),
        CheckConstraint(
            "employment_months_to_date BETWEEN 0 AND 12",
            name="ck_tax_opening_employment_months",
        ),
        CheckConstraint("taxable_income >= 0", name="ck_tax_opening_taxable_income"),
        CheckConstraint("employee_contribution >= 0", name="ck_tax_opening_employee_contribution"),
        CheckConstraint("special_deduction >= 0", name="ck_tax_opening_special_deduction"),
        CheckConstraint("tax_withheld >= 0", name="ck_tax_opening_tax_withheld"),
        CheckConstraint("btrim(evidence_ref) <> ''", name="ck_tax_opening_evidence_ref"),
        CheckConstraint(
            "(is_finalized = false AND finalized_by IS NULL AND finalized_at IS NULL) "
            "OR (is_finalized = true AND finalized_by IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_tax_opening_finalization",
        ),
        UniqueConstraint(
            "employee_id",
            "tax_year",
            "revision",
            name="uq_tax_opening_employee_year_revision",
        ),
        Index(
            "uq_tax_opening_active_employee_year",
            "employee_id",
            "tax_year",
            unique=True,
            postgresql_where=text("is_finalized AND superseded_at IS NULL"),
        ),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    revision: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default=text("1")
    )
    through_period: Mapped[str] = mapped_column(String(7), nullable=False)
    employment_months_to_date: Mapped[int] = mapped_column(Integer, nullable=False)
    taxable_income: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    employee_contribution: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    special_deduction: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    tax_withheld: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    evidence_ref: Mapped[str] = mapped_column(String(512), nullable=False)
    is_finalized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    finalized_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee_tax_ytd_opening.id"), nullable=True, index=True
    )
    superseded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    superseded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
