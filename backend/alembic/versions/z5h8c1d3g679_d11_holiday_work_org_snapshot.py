"""D11: capture the historical organization for holiday-work evidence.

Revision ID: z5h8c1d3g679
Revises: y4g7b0c2f568
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "z5h8c1d3g679"
down_revision: str | None = "y4g7b0c2f568"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Do not infer legacy historical ownership from the employee's current
    # store. Null legacy rows fail closed for scoped users and remain available
    # to global HR for controlled remediation.
    op.add_column("holiday_work_record", sa.Column("org_unit_id", sa.BigInteger(), nullable=True))
    op.create_foreign_key(
        "fk_holiday_work_record_org_unit_id_org_unit",
        "holiday_work_record",
        "org_unit",
        ["org_unit_id"],
        ["id"],
    )
    op.create_index(
        "ix_holiday_work_record_org_unit_id",
        "holiday_work_record",
        ["org_unit_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_holiday_work_record_org_unit_id", table_name="holiday_work_record")
    op.drop_constraint(
        "fk_holiday_work_record_org_unit_id_org_unit",
        "holiday_work_record",
        type_="foreignkey",
    )
    op.drop_column("holiday_work_record", "org_unit_id")
