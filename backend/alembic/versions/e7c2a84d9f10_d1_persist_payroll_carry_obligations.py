"""Persist deferred payroll obligations across locked periods.

Revision ID: e7c2a84d9f10
Revises: d3417a6c2e59
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "e7c2a84d9f10"
down_revision: str | None = "d3417a6c2e59"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Existing rows only stored the unpaid wage.  Preserve them with explicit
    # zero obligations; runtime has a narrow, audited legacy reconstruction
    # path for a new hire's historic first-month carry-over.
    op.add_column(
        "payroll_result",
        sa.Column(
            "deferred_deductions",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "payroll_result",
        sa.Column(
            "deferred_deposit",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("payroll_result", "deferred_deductions", server_default=None)
    op.alter_column("payroll_result", "deferred_deposit", server_default=None)


def downgrade() -> None:
    # Dropping these columns would otherwise erase obligations that must flow
    # into a later payroll period.  Treat both a non-zero value and an
    # unexpected NULL as unsafe: a downgrade must never silently discard an
    # obligation whose value cannot be proven to be zero.
    has_deferred_obligations = op.get_bind().scalar(
        sa.text(
            """
            SELECT 1
            FROM payroll_result
            WHERE deferred_deductions <> 0
               OR deferred_deposit <> 0
               OR deferred_deductions IS NULL
               OR deferred_deposit IS NULL
            LIMIT 1
            """,
        )
    )
    if has_deferred_obligations:
        raise RuntimeError(
            "Cannot downgrade while payroll results contain deferred obligations; "
            "restore a pre-upgrade backup instead."
        )
    op.drop_column("payroll_result", "deferred_deposit")
    op.drop_column("payroll_result", "deferred_deductions")
