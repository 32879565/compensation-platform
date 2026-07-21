"""Add audited employee tax opening balances for mid-year migrations.

Revision ID: j8a5c2e1f903
Revises: i7e3d9a4f862
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "j8a5c2e1f903"
down_revision: str | None = "i7e3d9a4f862"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "employee_tax_ytd_opening",
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("through_period", sa.String(length=7), nullable=False),
        sa.Column("employment_months_to_date", sa.Integer(), nullable=False),
        sa.Column("taxable_income", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("employee_contribution", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("special_deduction", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("tax_withheld", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("evidence_ref", sa.String(length=512), nullable=False),
        sa.Column("is_finalized", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("finalized_by", sa.BigInteger(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.CheckConstraint("tax_year BETWEEN 2000 AND 9999", name="ck_tax_opening_year"),
        sa.CheckConstraint(
            "through_period ~ '^[0-9]{4}-(0[1-9]|1[0-2])$'",
            name="ck_tax_opening_period",
        ),
        sa.CheckConstraint(
            "employment_months_to_date BETWEEN 0 AND 12",
            name="ck_tax_opening_employment_months",
        ),
        sa.CheckConstraint("taxable_income >= 0", name="ck_tax_opening_taxable_income"),
        sa.CheckConstraint(
            "employee_contribution >= 0", name="ck_tax_opening_employee_contribution"
        ),
        sa.CheckConstraint("special_deduction >= 0", name="ck_tax_opening_special_deduction"),
        sa.CheckConstraint("tax_withheld >= 0", name="ck_tax_opening_tax_withheld"),
        sa.CheckConstraint("btrim(evidence_ref) <> ''", name="ck_tax_opening_evidence_ref"),
        sa.CheckConstraint(
            "(is_finalized = false AND finalized_by IS NULL AND finalized_at IS NULL) "
            "OR (is_finalized = true AND finalized_by IS NOT NULL AND finalized_at IS NOT NULL)",
            name="ck_tax_opening_finalization",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["finalized_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "tax_year", name="uq_tax_opening_employee_year"),
    )
    op.create_index(
        "ix_employee_tax_ytd_opening_employee_id",
        "employee_tax_ytd_opening",
        ["employee_id"],
        unique=False,
    )
    op.create_index(
        "ix_employee_tax_ytd_opening_tax_year",
        "employee_tax_ytd_opening",
        ["tax_year"],
        unique=False,
    )
    op.create_index(
        "ix_employee_tax_ytd_opening_finalized_by",
        "employee_tax_ytd_opening",
        ["finalized_by"],
        unique=False,
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT 1 FROM employee_tax_ytd_opening LIMIT 1")) is not None:
        raise RuntimeError(
            "Cannot downgrade while employee tax opening data exists; "
            "restore a pre-upgrade backup instead."
        )
    op.drop_index("ix_employee_tax_ytd_opening_finalized_by", table_name="employee_tax_ytd_opening")
    op.drop_index("ix_employee_tax_ytd_opening_tax_year", table_name="employee_tax_ytd_opening")
    op.drop_index("ix_employee_tax_ytd_opening_employee_id", table_name="employee_tax_ytd_opening")
    op.drop_table("employee_tax_ytd_opening")
