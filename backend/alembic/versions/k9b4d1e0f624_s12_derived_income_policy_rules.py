"""Add explicit policy treatment for engine-derived payroll income.

Revision ID: k9b4d1e0f624
Revises: j8a5c2e1f903
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "k9b4d1e0f624"
down_revision: str | None = "j8a5c2e1f903"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_policy",
        sa.Column(
            "derived_income_rules",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.scalar(
            sa.text(
                "SELECT 1 FROM payroll_policy "
                "WHERE jsonb_array_length(derived_income_rules) > 0 LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "Cannot downgrade while derived-income policy rules exist; "
            "restore a pre-upgrade backup instead."
        )
    op.drop_column("payroll_policy", "derived_income_rules")
