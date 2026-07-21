"""Audited manual sources for prior-period payroll makeup and deductions."""

from __future__ import annotations

import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.grade import MONEY


class PayrollAdjustmentType(enum.StrEnum):
    PREV_MAKEUP = "PREV_MAKEUP"
    PREV_DEDUCT = "PREV_DEDUCT"


class MonthlyPayrollAdjustment(Base, TimestampMixin):
    """One employee/month/type source consumed by the payroll input snapshot."""

    __tablename__ = "monthly_payroll_adjustment"
    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "period",
            "adjustment_type",
            name="uq_monthly_payroll_adjustment_employee_period_type",
        ),
        CheckConstraint("amount > 0", name="ck_monthly_payroll_adjustment_positive_amount"),
        CheckConstraint(
            "btrim(reason) <> ''", name="ck_monthly_payroll_adjustment_reason_not_blank"
        ),
        CheckConstraint(
            "btrim(attachment_url) <> ''",
            name="ck_monthly_payroll_adjustment_attachment_not_blank",
        ),
        CheckConstraint(
            "(taxable IS NULL AND in_social_base IS NULL AND in_housing_base IS NULL) "
            "OR (taxable IS NOT NULL AND in_social_base IS NOT NULL "
            "AND in_housing_base IS NOT NULL)",
            name="ck_monthly_payroll_adjustment_classification_complete",
        ),
        Index(
            "ix_monthly_payroll_adjustment_period_org",
            "period",
            "org_unit_id",
        ),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    # Preserve the organization used for authorization when the source was
    # recorded; a later employee transfer must not silently widen visibility.
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    adjustment_type: Mapped[PayrollAdjustmentType] = mapped_column(
        Enum(PayrollAdjustmentType, name="payroll_adjustment_type"),
        nullable=False,
        index=True,
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    attachment_url: Mapped[str] = mapped_column(String(512), nullable=False)
    # Legacy rows remain nullable until HR explicitly classifies them.  The
    # engine fails closed for an unclassified non-zero source under a formal
    # payroll policy; all API-created rows require all three flags.
    taxable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    in_social_base: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    in_housing_base: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Override TimestampMixin's nullable actor with the mandatory source
    # creator required for payroll evidence.
    created_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    updated_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)


class MonthlyPayrollAdjustmentRevision(Base):
    """Append-only snapshot for every accepted source create or update."""

    __tablename__ = "monthly_payroll_adjustment_revision"
    __table_args__ = (
        UniqueConstraint(
            "adjustment_id",
            "revision",
            name="uq_monthly_payroll_adjustment_revision_number",
        ),
        CheckConstraint(
            "revision > 0",
            name="ck_monthly_payroll_adjustment_revision_positive_revision",
        ),
        CheckConstraint(
            "amount > 0",
            name="ck_monthly_payroll_adjustment_revision_positive_amount",
        ),
        CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_monthly_payroll_adjustment_revision_reason_not_blank",
        ),
        CheckConstraint(
            "btrim(attachment_url) <> ''",
            name="ck_monthly_payroll_adjustment_revision_attachment_not_blank",
        ),
        CheckConstraint(
            "(taxable IS NULL AND in_social_base IS NULL AND in_housing_base IS NULL) "
            "OR (taxable IS NOT NULL AND in_social_base IS NOT NULL "
            "AND in_housing_base IS NOT NULL)",
            name="ck_monthly_payroll_adjustment_revision_classification_complete",
        ),
        Index(
            "ix_monthly_payroll_adjustment_revision_lookup",
            "employee_id",
            "period",
            "adjustment_type",
            "revision",
        ),
        Index(
            "ix_monthly_payroll_adjustment_revision_period_org",
            "period",
            "org_unit_id",
        ),
    )

    adjustment_id: Mapped[int] = mapped_column(
        ForeignKey("monthly_payroll_adjustment.id"), nullable=False, index=True
    )
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    adjustment_type: Mapped[PayrollAdjustmentType] = mapped_column(
        Enum(PayrollAdjustmentType, name="payroll_adjustment_type"),
        nullable=False,
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    reason: Mapped[str] = mapped_column(String(2000), nullable=False)
    attachment_url: Mapped[str] = mapped_column(String(512), nullable=False)
    taxable: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    in_social_base: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    in_housing_base: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    changed_by: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
