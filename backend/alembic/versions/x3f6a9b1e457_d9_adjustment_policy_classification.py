"""Classify prior-period payroll adjustments for tax and contributions.

Revision ID: x3f6a9b1e457
Revises: w2f5a8b0d346
Create Date: 2026-07-21 10:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "x3f6a9b1e457"
down_revision: str | None = "w2f5a8b0d346"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows cannot be classified safely from their amount or label.
    # Keep them NULL so formal-policy payroll fails closed until HR reviews
    # and updates all three flags through the audited source API.
    op.add_column(
        "monthly_payroll_adjustment",
        sa.Column("taxable", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "monthly_payroll_adjustment",
        sa.Column("in_social_base", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "monthly_payroll_adjustment",
        sa.Column("in_housing_base", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "monthly_payroll_adjustment",
        sa.Column("updated_by", sa.BigInteger(), nullable=True),
    )
    op.execute(
        sa.text(
            "UPDATE monthly_payroll_adjustment SET updated_by = created_by "
            "WHERE updated_by IS NULL"
        )
    )
    op.alter_column(
        "monthly_payroll_adjustment",
        "updated_by",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_foreign_key(
        "fk_monthly_payroll_adjustment_updated_by_app_user",
        "monthly_payroll_adjustment",
        "app_user",
        ["updated_by"],
        ["id"],
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_updated_by"),
        "monthly_payroll_adjustment",
        ["updated_by"],
        unique=False,
    )
    op.create_check_constraint(
        "ck_monthly_payroll_adjustment_classification_complete",
        "monthly_payroll_adjustment",
        "(taxable IS NULL AND in_social_base IS NULL AND in_housing_base IS NULL) "
        "OR (taxable IS NOT NULL AND in_social_base IS NOT NULL "
        "AND in_housing_base IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_monthly_payroll_adjustment_classification_complete",
        "monthly_payroll_adjustment",
        type_="check",
    )
    op.drop_index(
        op.f("ix_monthly_payroll_adjustment_updated_by"),
        table_name="monthly_payroll_adjustment",
    )
    op.drop_constraint(
        "fk_monthly_payroll_adjustment_updated_by_app_user",
        "monthly_payroll_adjustment",
        type_="foreignkey",
    )
    op.drop_column("monthly_payroll_adjustment", "updated_by")
    op.drop_column("monthly_payroll_adjustment", "in_housing_base")
    op.drop_column("monthly_payroll_adjustment", "in_social_base")
    op.drop_column("monthly_payroll_adjustment", "taxable")
