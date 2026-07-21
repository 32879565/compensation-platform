"""Add cached, read-only DingTalk attendance snapshots.

Revision ID: u0d3e6f8b124
Revises: t9c2d5f7a013
Create Date: 2026-07-21 08:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "u0d3e6f8b124"
down_revision: str | None = "t9c2d5f7a013"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

attendance_sync_status = postgresql.ENUM(
    "QUEUED",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    name="dingtalk_attendance_sync_status",
)


def upgrade() -> None:
    bind = op.get_bind()
    attendance_sync_status.create(bind, checkfirst=True)
    status_type = postgresql.ENUM(
        "QUEUED",
        "RUNNING",
        "COMPLETED",
        "FAILED",
        name="dingtalk_attendance_sync_status",
        create_type=False,
    )

    op.create_table(
        "dingtalk_attendance_sync",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("status", status_type, nullable=False),
        sa.Column("requested_by_user_id", sa.BigInteger(), nullable=True),
        sa.Column("matched_employees", sa.Integer(), server_default="0", nullable=False),
        sa.Column("employees_with_records", sa.Integer(), server_default="0", nullable=False),
        sa.Column("total_records", sa.Integer(), server_default="0", nullable=False),
        sa.Column("ambiguous_directory_users", sa.Integer(), server_default="0", nullable=False),
        sa.Column("unmatched_directory_users", sa.Integer(), server_default="0", nullable=False),
        sa.Column("source_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("source_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_code", sa.String(length=64), nullable=True),
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
        sa.CheckConstraint(
            "matched_employees >= 0 AND employees_with_records >= 0 "
            "AND total_records >= 0 AND ambiguous_directory_users >= 0 "
            "AND unmatched_directory_users >= 0",
            name="ck_dingtalk_attendance_sync_nonnegative_counts",
        ),
        sa.ForeignKeyConstraint(["requested_by_user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period", name="uq_dingtalk_attendance_sync_period"),
    )
    op.create_index(
        op.f("ix_dingtalk_attendance_sync_period"),
        "dingtalk_attendance_sync",
        ["period"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dingtalk_attendance_sync_requested_by_user_id"),
        "dingtalk_attendance_sync",
        ["requested_by_user_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dingtalk_attendance_sync_status"),
        "dingtalk_attendance_sync",
        ["status"],
        unique=False,
    )

    op.create_table(
        "dingtalk_attendance_snapshot",
        sa.Column("sync_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("record_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("normal_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("late_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("early_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("absent_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("not_signed_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("other_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("refreshed_at", sa.DateTime(timezone=True), nullable=False),
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
        sa.CheckConstraint(
            "record_count >= 0 AND normal_count >= 0 AND late_count >= 0 "
            "AND early_count >= 0 AND absent_count >= 0 "
            "AND not_signed_count >= 0 AND other_count >= 0",
            name="ck_dingtalk_attendance_snapshot_nonnegative_counts",
        ),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["sync_id"], ["dingtalk_attendance_sync.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id",
            "period",
            name="uq_dingtalk_attendance_snapshot_employee_period",
        ),
    )
    op.create_index(
        op.f("ix_dingtalk_attendance_snapshot_employee_id"),
        "dingtalk_attendance_snapshot",
        ["employee_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dingtalk_attendance_snapshot_period"),
        "dingtalk_attendance_snapshot",
        ["period"],
        unique=False,
    )
    op.create_index(
        op.f("ix_dingtalk_attendance_snapshot_sync_id"),
        "dingtalk_attendance_snapshot",
        ["sync_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("dingtalk_attendance_snapshot")
    op.drop_table("dingtalk_attendance_sync")
    attendance_sync_status.drop(op.get_bind(), checkfirst=True)
