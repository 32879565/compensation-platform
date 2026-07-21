"""D14: add immutable monthly payroll adjustment revision snapshots.

Revision ID: c8k1f4h6j902
Revises: b7j0e3f5i891
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "c8k1f4h6j902"
down_revision: str | None = "b7j0e3f5i891"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    adjustment_type = postgresql.ENUM(
        "PREV_MAKEUP",
        "PREV_DEDUCT",
        name="payroll_adjustment_type",
        create_type=False,
    )
    op.create_table(
        "monthly_payroll_adjustment_revision",
        sa.Column("adjustment_id", sa.BigInteger(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("adjustment_type", adjustment_type, nullable=False),
        sa.Column("amount", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("attachment_url", sa.String(length=512), nullable=False),
        sa.Column("taxable", sa.Boolean(), nullable=True),
        sa.Column("in_social_base", sa.Boolean(), nullable=True),
        sa.Column("in_housing_base", sa.Boolean(), nullable=True),
        sa.Column("changed_by", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.CheckConstraint(
            "revision > 0",
            name="ck_monthly_payroll_adjustment_revision_positive_revision",
        ),
        sa.CheckConstraint(
            "amount > 0",
            name="ck_monthly_payroll_adjustment_revision_positive_amount",
        ),
        sa.CheckConstraint(
            "btrim(reason) <> ''",
            name="ck_monthly_payroll_adjustment_revision_reason_not_blank",
        ),
        sa.CheckConstraint(
            "btrim(attachment_url) <> ''",
            name="ck_monthly_payroll_adjustment_revision_attachment_not_blank",
        ),
        sa.CheckConstraint(
            "(taxable IS NULL AND in_social_base IS NULL AND in_housing_base IS NULL) "
            "OR (taxable IS NOT NULL AND in_social_base IS NOT NULL "
            "AND in_housing_base IS NOT NULL)",
            name="ck_monthly_payroll_adjustment_revision_classification_complete",
        ),
        sa.ForeignKeyConstraint(
            ["adjustment_id"],
            ["monthly_payroll_adjustment.id"],
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.ForeignKeyConstraint(["changed_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "adjustment_id",
            "revision",
            name="uq_monthly_payroll_adjustment_revision_number",
        ),
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_revision_adjustment_id"),
        "monthly_payroll_adjustment_revision",
        ["adjustment_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_monthly_payroll_adjustment_revision_changed_by"),
        "monthly_payroll_adjustment_revision",
        ["changed_by"],
        unique=False,
    )
    op.create_index(
        "ix_monthly_payroll_adjustment_revision_lookup",
        "monthly_payroll_adjustment_revision",
        ["employee_id", "period", "adjustment_type", "revision"],
        unique=False,
    )
    op.create_index(
        "ix_monthly_payroll_adjustment_revision_period_org",
        "monthly_payroll_adjustment_revision",
        ["period", "org_unit_id"],
        unique=False,
    )
    op.execute("""
        CREATE OR REPLACE FUNCTION monthly_payroll_adjustment_revision_block_modify()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'monthly payroll adjustment revision is append-only: % not allowed',
                TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """)
    op.execute("""
        CREATE TRIGGER monthly_payroll_adjustment_revision_no_update_delete
        BEFORE UPDATE OR DELETE ON monthly_payroll_adjustment_revision
        FOR EACH ROW
        EXECUTE FUNCTION monthly_payroll_adjustment_revision_block_modify();
        """)

    # Existing natural-key rows predate this revision table.  Their current
    # state becomes revision 1 without inventing a historical sequence.
    op.execute(sa.text("""
            INSERT INTO monthly_payroll_adjustment_revision (
                adjustment_id, revision, employee_id, org_unit_id, period,
                adjustment_type, amount, reason, attachment_url, taxable,
                in_social_base, in_housing_base, changed_by, created_at
            )
            SELECT
                id, 1, employee_id, org_unit_id, period, adjustment_type,
                amount, reason, attachment_url, taxable, in_social_base,
                in_housing_base, updated_by, updated_at
            FROM monthly_payroll_adjustment
            """))


def downgrade() -> None:
    raise RuntimeError(
        "D14 is forward-only: dropping immutable monthly payroll adjustment "
        "revisions would erase payroll evidence history."
    )
