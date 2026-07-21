"""Persist non-blocking payroll warnings and align actor identifier widths.

Revision ID: b61e4a9037f2
Revises: a3f6c9d5b1e7
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "b61e4a9037f2"
down_revision: str | None = "a3f6c9d5b1e7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "payroll_result",
        sa.Column(
            "warnings",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
    )
    op.alter_column("payroll_result", "warnings", server_default=None)

    for table_name, column_name in (
        ("batch_confirmation", "confirmed_by"),
        ("comp_dispute", "raised_by"),
        ("comp_dispute", "resolved_by"),
        ("adjustment_record", "applicant_id"),
        ("adjustment_record", "approver_id"),
    ):
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            postgresql_using=f"{column_name}::bigint",
        )


def downgrade() -> None:
    op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM (
                        SELECT confirmed_by AS actor_id FROM batch_confirmation
                        UNION ALL SELECT raised_by FROM comp_dispute
                        UNION ALL SELECT resolved_by FROM comp_dispute
                        UNION ALL SELECT applicant_id FROM adjustment_record
                        UNION ALL SELECT approver_id FROM adjustment_record
                    ) AS actor_ids
                    WHERE actor_id > 2147483647 OR actor_id < -2147483648
                ) THEN
                    RAISE EXCEPTION
                        'Cannot downgrade S13e: actor ids exceed the legacy integer range';
                END IF;
            END $$;
            """))
    for table_name, column_name in (
        ("adjustment_record", "approver_id"),
        ("adjustment_record", "applicant_id"),
        ("comp_dispute", "resolved_by"),
        ("comp_dispute", "raised_by"),
        ("batch_confirmation", "confirmed_by"),
    ):
        op.alter_column(
            table_name,
            column_name,
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            postgresql_using=f"{column_name}::integer",
        )

    op.drop_column("payroll_result", "warnings")
