"""D15: backfill and protect append-only payroll-dispute events.

Revision ID: d9l2g5i7k013
Revises: c8k1f4h6j902
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "d9l2g5i7k013"
down_revision: str | None = "c8k1f4h6j902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # D12 introduced the event ledger after disputes already existed.  Preserve
    # those pre-ledger disputes by materializing the evidence still available
    # on the legacy row.  A second event captures a persisted decision when one
    # exists; the migration deliberately does not invent intermediate history.
    op.execute(sa.text("""
        INSERT INTO dispute_event (
            dispute_id, event_type, note, actor_id, attachment_url, created_at
        )
        SELECT
            dispute.id,
            'RAISED',
            dispute.opinion,
            dispute.raised_by,
            NULL,
            dispute.created_at
        FROM comp_dispute AS dispute
        WHERE NOT EXISTS (
            SELECT 1
            FROM dispute_event AS event
            WHERE event.dispute_id = dispute.id
        )
        """))
    op.execute(sa.text("""
        INSERT INTO dispute_event (
            dispute_id, event_type, note, actor_id, attachment_url, created_at
        )
        SELECT
            dispute.id,
            dispute.status::text,
            COALESCE(NULLIF(btrim(dispute.resolution), ''), 'Migrated legacy decision'),
            COALESCE(dispute.resolved_by, dispute.raised_by),
            NULL,
            COALESCE(dispute.resolved_at, dispute.updated_at)
        FROM comp_dispute AS dispute
        WHERE dispute.status::text <> 'OPEN'
          AND NOT EXISTS (
              SELECT 1
              FROM dispute_event AS event
              WHERE event.dispute_id = dispute.id
                AND event.event_type = dispute.status::text
          )
        """))

    op.execute("""
        CREATE OR REPLACE FUNCTION dispute_event_block_modify()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'dispute event is append-only: % not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """)
    op.execute("""
        CREATE TRIGGER dispute_event_no_update_delete
        BEFORE UPDATE OR DELETE ON dispute_event
        FOR EACH ROW
        EXECUTE FUNCTION dispute_event_block_modify();
        """)


def downgrade() -> None:
    raise RuntimeError(
        "D15 is forward-only: removing dispute-event immutability would weaken "
        "the payroll evidence ledger"
    )
