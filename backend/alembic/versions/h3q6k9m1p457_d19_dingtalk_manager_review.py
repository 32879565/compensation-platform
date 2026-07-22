"""D19: purpose-bound DingTalk manager review links and login controls.

Revision ID: h3q6k9m1p457
Revises: g2p5j8l0n346
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "h3q6k9m1p457"
down_revision: str | None = "g2p5j8l0n346"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "app_user",
        sa.Column(
            "login_enabled",
            sa.Boolean(),
            server_default=sa.true(),
            nullable=False,
        ),
    )
    op.add_column(
        "dingtalk_delivery",
        sa.Column("review_public_id", sa.String(length=32), nullable=True),
    )
    connection = op.get_bind()
    delivery_ids = connection.execute(sa.text("SELECT id FROM dingtalk_delivery")).scalars()
    for delivery_id in delivery_ids:
        connection.execute(
            sa.text(
                "UPDATE dingtalk_delivery "
                "SET review_public_id = :review_public_id WHERE id = :delivery_id"
            ),
            {"review_public_id": uuid.uuid4().hex, "delivery_id": delivery_id},
        )
    op.alter_column("dingtalk_delivery", "review_public_id", nullable=False)
    op.create_index(
        "ix_dingtalk_delivery_review_public_id",
        "dingtalk_delivery",
        ["review_public_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_dingtalk_delivery_review_public_id",
        table_name="dingtalk_delivery",
    )
    op.drop_column("dingtalk_delivery", "review_public_id")
    op.drop_column("app_user", "login_enabled")
