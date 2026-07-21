"""Add encrypted DingTalk recipient routing and provider task metadata.

Revision ID: s8b1c4e6f902
Revises: r7a0b3d9e864
Create Date: 2026-07-21 10:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "s8b1c4e6f902"
down_revision: str | None = "r7a0b3d9e864"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Values written to dingtalk_user_id are Fernet ciphertext produced by the
    # EncryptedString model type.  The migration intentionally has no plaintext
    # data backfill.
    op.add_column(
        "app_user",
        sa.Column("dingtalk_user_id", sa.String(length=512), nullable=True),
    )
    op.add_column(
        "dingtalk_delivery",
        sa.Column("provider_task_id", sa.BigInteger(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("dingtalk_delivery", "provider_task_id")
    op.drop_column("app_user", "dingtalk_user_id")
