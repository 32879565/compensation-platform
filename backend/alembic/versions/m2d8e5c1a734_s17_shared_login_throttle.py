"""Persist shared, privacy-preserving login throttle buckets.

Revision ID: m2d8e5c1a734
Revises: l1c7f4b0a925
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "m2d8e5c1a734"
down_revision: str | None = "l1c7f4b0a925"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "login_throttle_bucket",
        sa.Column("scope", sa.String(length=16), nullable=False),
        sa.Column("key_digest", sa.String(length=64), nullable=False),
        sa.Column("failure_count", sa.Integer(), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.CheckConstraint("failure_count > 0", name="ck_login_throttle_bucket_failure_count"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("scope", "key_digest", name="uq_login_throttle_bucket_scope_digest"),
    )
    op.create_index(
        "ix_login_throttle_bucket_expires_at",
        "login_throttle_bucket",
        ["expires_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_login_throttle_bucket_expires_at", table_name="login_throttle_bucket")
    op.drop_table("login_throttle_bucket")
