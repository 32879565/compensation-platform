"""Add batch lifecycle actors and adjustment review-round provenance.

Revision ID: v1e4f7a9c235
Revises: u0d3e6f8b124
Create Date: 2026-07-21 09:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "v1e4f7a9c235"
down_revision: str | None = "u0d3e6f8b124"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("payroll_batch", sa.Column("hr_reviewed_by", sa.BigInteger(), nullable=True))
    op.add_column(
        "payroll_batch",
        sa.Column("hr_reviewed_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column(
        "adjustment_record",
        sa.Column("batch_version", sa.BigInteger(), nullable=True),
    )
    # Approved disputes already carry an authoritative review round.
    op.execute(sa.text("""
            UPDATE adjustment_record AS adjustment
            SET batch_version = dispute.batch_version
            FROM comp_dispute AS dispute
            WHERE adjustment.batch_version IS NULL
              AND adjustment.dispute_id = dispute.id
            """))
    # Direct corrections persist the pending/recomputed round in their JSON
    # result.  Accept only a positive integer representation before casting.
    op.execute(sa.text("""
            UPDATE adjustment_record
            SET batch_version = (recompute_result ->> 'batch_version')::bigint
            WHERE batch_version IS NULL
              AND jsonb_typeof(recompute_result) = 'object'
              AND (recompute_result ->> 'batch_version') ~ '^[1-9][0-9]*$'
            """))
    # Very old records may predate both provenance fields.  A single distinct
    # persisted result round is unambiguous; multiple/no rounds are not.
    op.execute(sa.text("""
            UPDATE adjustment_record AS adjustment
            SET batch_version = resolved.batch_version
            FROM (
                SELECT candidate.id, MIN(result.batch_version) AS batch_version
                FROM adjustment_record AS candidate
                JOIN payroll_result AS result
                  ON result.batch_id = candidate.batch_id
                 AND result.employee_id = candidate.employee_id
                WHERE candidate.batch_version IS NULL
                GROUP BY candidate.id
                HAVING COUNT(DISTINCT result.batch_version) = 1
            ) AS resolved
            WHERE adjustment.id = resolved.id
            """))
    op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM adjustment_record
                    WHERE batch_version IS NULL
                ) THEN
                    RAISE EXCEPTION
                        'D7 cannot safely assign legacy adjustment records to payroll rounds; '
                        'map the remaining records before upgrading';
                END IF;
            END $$;
            """))
    op.alter_column(
        "adjustment_record",
        "batch_version",
        existing_type=sa.BigInteger(),
        nullable=False,
    )
    op.create_index(
        op.f("ix_adjustment_record_batch_version"),
        "adjustment_record",
        ["batch_version"],
        unique=False,
    )


def downgrade() -> None:
    raise RuntimeError(
        "D7 is forward-only: dropping lifecycle actors or adjustment round provenance "
        "would destroy payroll audit history. Restore a pre-upgrade backup instead."
    )
