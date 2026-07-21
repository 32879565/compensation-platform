"""D17: optimistic concurrency for employee grade assignments.

Revision ID: f1n4i7k9m235
Revises: e0m3h6j8l124
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f1n4i7k9m235"
down_revision: str | None = "e0m3h6j8l124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "employee",
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.create_check_constraint(
        "ck_employee_version_positive",
        "employee",
        "version > 0",
    )


def downgrade() -> None:
    op.drop_constraint("ck_employee_version_positive", "employee", type_="check")
    op.drop_column("employee", "version")
