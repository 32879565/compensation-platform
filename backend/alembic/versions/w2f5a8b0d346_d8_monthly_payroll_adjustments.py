"""Add auditable monthly makeup and deduction payroll sources.

Revision ID: w2f5a8b0d346
Revises: v1e4f7a9c235
Create Date: 2026-07-21 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "w2f5a8b0d346"
down_revision: str | None = "v1e4f7a9c235"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

payroll_adjustment_type = postgresql.ENUM(
    "PREV_MAKEUP",
    "PREV_DEDUCT",
    name="payroll_adjustment_type",
)


def upgrade() -> None:
    bind = op.get_bind()
    payroll_adjustment_type.create(bind, checkfirst=True)
    adjustment_type = postgresql.ENUM(
        "PREV_MAKEUP",
        "PREV_DEDUCT",
        name="payroll_adjustment_type",
        create_type=False,
    )
    op.create_table(
        "monthly_payroll_adjustment",
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("adjustment_type", adjustment_type, nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("attachment_url", sa.String(length=512), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("created_by", sa.BigInteger(), nullable=False),
        sa.CheckConstraint("amount > 0", name="ck_monthly_payroll_adjustment_positive_amount"),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_monthly_payroll_adjustment_reason_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(attachment_url) <> ''",
            name="ck_monthly_payroll_adjustment_attachment_not_blank",
        ),
        sa.ForeignKeyConstraint(["created_by"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id",
            "period",
            "adjustment_type",
            name="uq_monthly_payroll_adjustment_employee_period_type",
        ),
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_adjustment_type"),
        "monthly_payroll_adjustment",
        ["adjustment_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_created_by"),
        "monthly_payroll_adjustment",
        ["created_by"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_employee_id"),
        "monthly_payroll_adjustment",
        ["employee_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_org_unit_id"),
        "monthly_payroll_adjustment",
        ["org_unit_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_period"),
        "monthly_payroll_adjustment",
        ["period"],
        unique=False,
    )
    op.create_index(
        "ix_monthly_payroll_adjustment_period_org",
        "monthly_payroll_adjustment",
        ["period", "org_unit_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("monthly_payroll_adjustment")
    payroll_adjustment_type.drop(op.get_bind(), checkfirst=True)
