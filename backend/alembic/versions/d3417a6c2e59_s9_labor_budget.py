"""Add organization-month labor budgets.

Revision ID: d3417a6c2e59
Revises: c8f31a7d9e24
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d3417a6c2e59"
down_revision: str | None = "c8f31a7d9e24"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "labor_budget",
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.Date(), nullable=False),
        sa.Column("headcount_budget", sa.Integer(), nullable=False),
        sa.Column("labor_cost_budget", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("note", sa.String(length=500), nullable=True),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
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
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_unit_id", "period", name="uq_labor_budget_org_period"),
    )
    op.create_index("ix_labor_budget_org_unit_id", "labor_budget", ["org_unit_id"], unique=False)
    op.create_index("ix_labor_budget_period", "labor_budget", ["period"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_labor_budget_period", table_name="labor_budget")
    op.drop_index("ix_labor_budget_org_unit_id", table_name="labor_budget")
    op.drop_table("labor_budget")
