"""Persist immutable employee and payment identity on payroll results.

Revision ID: y4g7b0c2f568
Revises: x3f6a9b1e457
Create Date: 2026-07-21 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "y4g7b0c2f568"
down_revision: str | None = "x3f6a9b1e457"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Do not backfill from mutable employee master data: it cannot prove what
    # was present when an old payroll result was calculated.  Legacy regulated
    # exports fail closed until an authorized remediation establishes values.
    op.add_column("payroll_result", sa.Column("emp_no_snapshot", sa.String(32), nullable=True))
    op.add_column(
        "payroll_result",
        sa.Column("employee_name_snapshot", sa.String(64), nullable=True),
    )
    op.add_column("payroll_result", sa.Column("id_card_snapshot", sa.String(512), nullable=True))
    op.add_column(
        "payroll_result",
        sa.Column("bank_account_snapshot", sa.String(512), nullable=True),
    )
    op.add_column(
        "payroll_result",
        sa.Column("social_city_snapshot", sa.String(32), nullable=True),
    )


def downgrade() -> None:
    raise RuntimeError(
        "D10 is forward-only: dropping locked payment identity would make bank "
        "exports depend on mutable employee master data again."
    )
