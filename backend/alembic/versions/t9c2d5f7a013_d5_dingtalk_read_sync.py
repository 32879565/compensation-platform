"""Add privacy-minimizing DingTalk employee identity binding.

Revision ID: t9c2d5f7a013
Revises: s8b1c4e6f902
Create Date: 2026-07-21 05:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "t9c2d5f7a013"
down_revision: str | None = "s8b1c4e6f902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Only keyed SHA-256 digests are written; there is intentionally no
    # plaintext/ciphertext provider-identifier backfill.
    op.add_column(
        "employee",
        sa.Column("dingtalk_user_id_hash", sa.String(length=64), nullable=True),
    )
    op.create_unique_constraint(
        "uq_employee_dingtalk_user_id_hash",
        "employee",
        ["dingtalk_user_id_hash"],
    )


def downgrade() -> None:
    op.drop_constraint("uq_employee_dingtalk_user_id_hash", "employee", type_="unique")
    op.drop_column("employee", "dingtalk_user_id_hash")
