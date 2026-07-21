"""Add sandbox DingTalk routing records and approval-backed compensation appeals.

Revision ID: o4d7e2f8a631
Revises: n3a8c4d2e701
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "o4d7e2f8a631"
down_revision: str | None = "n3a8c4d2e701"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


department_enum = postgresql.ENUM(
    "DINING", "KITCHEN", "OTHER", name="department", create_type=False
)
dingtalk_delivery_kind = postgresql.ENUM(
    "PAYROLL_REVIEW", "APPEAL_STATUS", name="dingtalk_delivery_kind", create_type=False
)
dingtalk_delivery_status = postgresql.ENUM(
    "PENDING", "SANDBOXED", "SENT", "FAILED", name="dingtalk_delivery_status", create_type=False
)
appeal_status = postgresql.ENUM(
    "PENDING", "UPHELD", "CORRECTION_REQUIRED", name="appeal_status", create_type=False
)


def _timestamp_columns() -> list[sa.Column]:
    return [
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
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for enum in (dingtalk_delivery_kind, dingtalk_delivery_status, appeal_status):
        enum.create(bind, checkfirst=True)

    op.create_table(
        "dingtalk_delivery",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("payroll_batch.id"), nullable=False),
        sa.Column("batch_version", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), sa.ForeignKey("org_unit.id"), nullable=False),
        sa.Column("department", department_enum, nullable=False),
        sa.Column(
            "recipient_user_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=True
        ),
        sa.Column("kind", dingtalk_delivery_kind, nullable=False),
        sa.Column("status", dingtalk_delivery_status, nullable=False),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("idempotency_key", sa.String(length=160), nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint("idempotency_key", name="uq_dingtalk_delivery_idempotency"),
    )
    for column in (
        "batch_id",
        "batch_version",
        "org_unit_id",
        "recipient_user_id",
        "kind",
        "status",
    ):
        op.create_index(f"ix_dingtalk_delivery_{column}", "dingtalk_delivery", [column])

    op.create_table(
        "comp_appeal",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "delivery_id", sa.BigInteger(), sa.ForeignKey("dingtalk_delivery.id"), nullable=False
        ),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("payroll_batch.id"), nullable=False),
        sa.Column("batch_version", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), sa.ForeignKey("org_unit.id"), nullable=False),
        sa.Column("department", department_enum, nullable=False),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("employee.id"), nullable=True),
        sa.Column("requester_id", sa.BigInteger(), sa.ForeignKey("app_user.id"), nullable=False),
        sa.Column("dedupe_key", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("status", appeal_status, nullable=False, server_default="PENDING"),
        sa.Column("resolution", sa.String(length=2000), nullable=True),
        sa.Column(
            "approval_instance_id",
            sa.BigInteger(),
            sa.ForeignKey("approval_instance.id"),
            nullable=True,
        ),
        *_timestamp_columns(),
        sa.UniqueConstraint("dedupe_key", name="uq_comp_appeal_dedupe"),
        sa.UniqueConstraint("approval_instance_id", name="uq_comp_appeal_approval_instance_id"),
    )
    for column in (
        "delivery_id",
        "batch_id",
        "batch_version",
        "org_unit_id",
        "employee_id",
        "requester_id",
        "status",
        "approval_instance_id",
    ):
        op.create_index(f"ix_comp_appeal_{column}", "comp_appeal", [column])

    op.execute(sa.text("""
            INSERT INTO permission (code, name, created_at, updated_at)
            VALUES ('notification:manage', '管理薪酬通知', now(), now())
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM role JOIN permission ON permission.code = 'notification:manage'
            WHERE role.code IN ('GROUP_HR', 'SUPER_ADMIN')
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    op.execute(sa.text("""
            DELETE FROM role_permission
            WHERE permission_id = (SELECT id FROM permission WHERE code = 'notification:manage')
            """))
    op.execute(sa.text("DELETE FROM permission WHERE code = 'notification:manage'"))

    for column in (
        "approval_instance_id",
        "status",
        "requester_id",
        "employee_id",
        "org_unit_id",
        "batch_version",
        "batch_id",
        "delivery_id",
    ):
        op.drop_index(f"ix_comp_appeal_{column}", table_name="comp_appeal")
    op.drop_table("comp_appeal")

    for column in (
        "status",
        "kind",
        "recipient_user_id",
        "org_unit_id",
        "batch_version",
        "batch_id",
    ):
        op.drop_index(f"ix_dingtalk_delivery_{column}", table_name="dingtalk_delivery")
    op.drop_table("dingtalk_delivery")

    bind = op.get_bind()
    for enum in (appeal_status, dingtalk_delivery_status, dingtalk_delivery_kind):
        enum.drop(bind, checkfirst=True)
