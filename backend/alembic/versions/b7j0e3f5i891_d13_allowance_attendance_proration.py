"""D13: configure attendance-prorated allowance components.

Revision ID: b7j0e3f5i891
Revises: a6i9d2e4h780
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "b7j0e3f5i891"
down_revision: str | None = "a6i9d2e4h780"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "salary_component_def",
        sa.Column(
            "prorate_by_attendance",
            sa.Boolean(),
            server_default=sa.false(),
            nullable=False,
        ),
    )
    op.create_check_constraint(
        "ck_salary_component_attendance_proration_allowance_only",
        "salary_component_def",
        "component_type = 'ALLOWANCE' OR prorate_by_attendance = false",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_salary_component_attendance_proration_allowance_only",
        "salary_component_def",
        type_="check",
    )
    op.drop_column("salary_component_def", "prorate_by_attendance")
