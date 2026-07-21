"""Make audited tax openings correctable by immutable supersession.

Revision ID: l1c7f4b0a925
Revises: k9b4d1e0f624
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "l1c7f4b0a925"
down_revision: str | None = "k9b4d1e0f624"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("uq_tax_opening_employee_year", "employee_tax_ytd_opening", type_="unique")
    op.add_column(
        "employee_tax_ytd_opening",
        sa.Column("revision", sa.Integer(), server_default=sa.text("1"), nullable=False),
    )
    op.add_column(
        "employee_tax_ytd_opening",
        sa.Column("supersedes_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "employee_tax_ytd_opening",
        sa.Column("superseded_by", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "employee_tax_ytd_opening",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_tax_opening_supersedes",
        "employee_tax_ytd_opening",
        "employee_tax_ytd_opening",
        ["supersedes_id"],
        ["id"],
    )
    op.create_foreign_key(
        "fk_tax_opening_superseded_by",
        "employee_tax_ytd_opening",
        "app_user",
        ["superseded_by"],
        ["id"],
    )
    op.create_index(
        "ix_employee_tax_ytd_opening_supersedes_id",
        "employee_tax_ytd_opening",
        ["supersedes_id"],
        unique=False,
    )
    op.create_unique_constraint(
        "uq_tax_opening_employee_year_revision",
        "employee_tax_ytd_opening",
        ["employee_id", "tax_year", "revision"],
    )
    op.create_index(
        "uq_tax_opening_active_employee_year",
        "employee_tax_ytd_opening",
        ["employee_id", "tax_year"],
        unique=True,
        postgresql_where=sa.text("is_finalized AND superseded_at IS NULL"),
    )


def downgrade() -> None:
    bind = op.get_bind()
    if (
        bind.scalar(
            sa.text(
                "SELECT 1 FROM employee_tax_ytd_opening "
                "WHERE revision <> 1 OR supersedes_id IS NOT NULL OR superseded_at IS NOT NULL "
                "LIMIT 1"
            )
        )
        is not None
    ):
        raise RuntimeError(
            "Cannot downgrade while revised tax openings exist; "
            "restore a pre-upgrade backup instead."
        )
    op.drop_index("uq_tax_opening_active_employee_year", table_name="employee_tax_ytd_opening")
    op.drop_constraint(
        "uq_tax_opening_employee_year_revision", "employee_tax_ytd_opening", type_="unique"
    )
    op.drop_index(
        "ix_employee_tax_ytd_opening_supersedes_id", table_name="employee_tax_ytd_opening"
    )
    op.drop_constraint(
        "fk_tax_opening_superseded_by", "employee_tax_ytd_opening", type_="foreignkey"
    )
    op.drop_constraint("fk_tax_opening_supersedes", "employee_tax_ytd_opening", type_="foreignkey")
    op.drop_column("employee_tax_ytd_opening", "superseded_at")
    op.drop_column("employee_tax_ytd_opening", "superseded_by")
    op.drop_column("employee_tax_ytd_opening", "supersedes_id")
    op.drop_column("employee_tax_ytd_opening", "revision")
    op.create_unique_constraint(
        "uq_tax_opening_employee_year", "employee_tax_ytd_opening", ["employee_id", "tax_year"]
    )
