"""Distinguish missing hourly input from recorded zero work hours.

Revision ID: g4e9a1d7c530
Revises: f8b3d12a6c44
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g4e9a1d7c530"
down_revision: str | None = "f8b3d12a6c44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # New omitted values persist as NULL so the engine can fail closed.
    op.alter_column(
        "attendance_record",
        "worked_hours",
        existing_type=sa.Numeric(precision=6, scale=2),
        nullable=True,
        server_default=None,
    )
    # Before this revision, hourly attendance was NOT NULL with a default of
    # zero.  For ordinary DINING/KITCHEN staff, historical zeroes therefore
    # cannot be distinguished from omitted source data.  Mark only those
    # ambiguous legacy values as missing; explicit zeroes for special
    # positions and non-hourly departments remain intact.
    op.execute(
        sa.text(
            """
            UPDATE attendance_record AS attendance
            SET worked_hours = NULL
            FROM employee AS employee
            WHERE attendance.employee_id = employee.id
              AND employee.department IN ('DINING', 'KITCHEN')
              AND employee.is_special_position IS FALSE
              AND attendance.worked_hours = 0
            """,
        )
    )


def downgrade() -> None:
    # NULL cannot safely become a payroll value.  Refuse an irreversible
    # downgrade rather than silently rewriting missing source data to zero.
    if (
        op.get_bind()
        .execute(sa.text("SELECT 1 FROM attendance_record WHERE worked_hours IS NULL LIMIT 1"))
        .first()
    ):
        raise RuntimeError(
            "Cannot downgrade while attendance records contain missing worked_hours; "
            "restore a pre-upgrade backup instead."
        )
    op.alter_column(
        "attendance_record",
        "worked_hours",
        existing_type=sa.Numeric(precision=6, scale=2),
        nullable=False,
        server_default=None,
    )
