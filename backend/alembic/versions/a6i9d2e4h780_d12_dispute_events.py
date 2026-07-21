"""D12: append-only payroll-dispute evidence and decision events.

Revision ID: a6i9d2e4h780
Revises: z5h8c1d3g679
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a6i9d2e4h780"
down_revision: str | None = "z5h8c1d3g679"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "dispute_event",
        sa.Column("dispute_id", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("note", sa.String(length=1000), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("attachment_url", sa.String(length=512), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["actor_id"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["dispute_id"], ["comp_dispute.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_dispute_event_actor_id", "dispute_event", ["actor_id"])
    op.create_index("ix_dispute_event_dispute_id", "dispute_event", ["dispute_id"])


def downgrade() -> None:
    op.drop_index("ix_dispute_event_dispute_id", table_name="dispute_event")
    op.drop_index("ix_dispute_event_actor_id", table_name="dispute_event")
    op.drop_table("dispute_event")
