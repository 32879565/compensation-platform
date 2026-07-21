"""S13c payroll result confirm dispute

Revision ID: ede872b8c568
Revises: db007abaa17c
Create Date: 2026-07-19 23:56:51.112627

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "ede872b8c568"
down_revision: str | None = "db007abaa17c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# department 枚举由 S13a 创建，此处复用（不再 CREATE TYPE）
department_enum = postgresql.ENUM(
    "DINING", "KITCHEN", "OTHER", name="department", create_type=False
)
# 本迁移新增的两个枚举，downgrade 时需显式 DROP TYPE
confirm_status_enum = postgresql.ENUM("PENDING", "CONFIRMED", "DISPUTED", name="confirm_status")
dispute_status_enum = postgresql.ENUM(
    "OPEN", "APPROVED", "REJECTED", "NEED_MORE", name="dispute_status"
)


def upgrade() -> None:
    bind = op.get_bind()
    confirm_status_enum.create(bind, checkfirst=True)
    dispute_status_enum.create(bind, checkfirst=True)
    op.create_table(
        "batch_confirmation",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("department", department_enum, nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "PENDING", "CONFIRMED", "DISPUTED", name="confirm_status", create_type=False
            ),
            nullable=False,
        ),
        sa.Column("confirmed_by", sa.Integer(), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["payroll_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["org_unit_id"],
            ["org_unit.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "org_unit_id", "department", name="uq_confirm_scope"),
    )
    op.create_index(
        op.f("ix_batch_confirmation_batch_id"), "batch_confirmation", ["batch_id"], unique=False
    )
    op.create_index(
        op.f("ix_batch_confirmation_org_unit_id"),
        "batch_confirmation",
        ["org_unit_id"],
        unique=False,
    )
    op.create_table(
        "comp_dispute",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("salary_item", sa.String(length=64), nullable=False),
        sa.Column("opinion", sa.String(length=1000), nullable=False),
        sa.Column("raised_by", sa.Integer(), nullable=False),
        sa.Column(
            "status",
            postgresql.ENUM(
                "OPEN",
                "APPROVED",
                "REJECTED",
                "NEED_MORE",
                name="dispute_status",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("resolution", sa.String(length=1000), nullable=True),
        sa.Column("resolved_by", sa.Integer(), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["payroll_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_comp_dispute_batch_id"), "comp_dispute", ["batch_id"], unique=False)
    op.create_index(
        op.f("ix_comp_dispute_employee_id"), "comp_dispute", ["employee_id"], unique=False
    )
    op.create_table(
        "payroll_result",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("batch_version", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=True),
        sa.Column("department", department_enum, nullable=False),
        sa.Column("actual_attendance_days", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("gross", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("deposit", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("net", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("carry_forward", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("rule_version", sa.String(length=32), nullable=False),
        sa.Column("input_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("lines", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exceptions", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("has_error", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["payroll_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
        ),
        sa.ForeignKeyConstraint(
            ["org_unit_id"],
            ["org_unit.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("batch_id", "employee_id", "version", name="uq_result_batch_emp_ver"),
    )
    op.create_index(
        op.f("ix_payroll_result_batch_id"), "payroll_result", ["batch_id"], unique=False
    )
    op.create_index(
        op.f("ix_payroll_result_employee_id"), "payroll_result", ["employee_id"], unique=False
    )
    op.create_index(
        op.f("ix_payroll_result_org_unit_id"), "payroll_result", ["org_unit_id"], unique=False
    )
    op.create_table(
        "adjustment_record",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("dispute_id", sa.BigInteger(), nullable=True),
        sa.Column("item", sa.String(length=64), nullable=False),
        sa.Column("before_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("after_value", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("reason", sa.String(length=1000), nullable=False),
        sa.Column("applicant_id", sa.Integer(), nullable=True),
        sa.Column("approver_id", sa.Integer(), nullable=False),
        sa.Column("attachment_url", sa.String(length=512), nullable=True),
        sa.Column("recompute_result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(
            ["batch_id"],
            ["payroll_batch.id"],
        ),
        sa.ForeignKeyConstraint(
            ["dispute_id"],
            ["comp_dispute.id"],
        ),
        sa.ForeignKeyConstraint(
            ["employee_id"],
            ["employee.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_adjustment_record_batch_id"), "adjustment_record", ["batch_id"], unique=False
    )
    op.create_index(
        op.f("ix_adjustment_record_employee_id"), "adjustment_record", ["employee_id"], unique=False
    )
    # ### end Alembic commands ###


def downgrade() -> None:
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_index(op.f("ix_adjustment_record_employee_id"), table_name="adjustment_record")
    op.drop_index(op.f("ix_adjustment_record_batch_id"), table_name="adjustment_record")
    op.drop_table("adjustment_record")
    op.drop_index(op.f("ix_payroll_result_org_unit_id"), table_name="payroll_result")
    op.drop_index(op.f("ix_payroll_result_employee_id"), table_name="payroll_result")
    op.drop_index(op.f("ix_payroll_result_batch_id"), table_name="payroll_result")
    op.drop_table("payroll_result")
    op.drop_index(op.f("ix_comp_dispute_employee_id"), table_name="comp_dispute")
    op.drop_index(op.f("ix_comp_dispute_batch_id"), table_name="comp_dispute")
    op.drop_table("comp_dispute")
    op.drop_index(op.f("ix_batch_confirmation_org_unit_id"), table_name="batch_confirmation")
    op.drop_index(op.f("ix_batch_confirmation_batch_id"), table_name="batch_confirmation")
    op.drop_table("batch_confirmation")
    # 仅删除本迁移新增的枚举；department 由 S13a 拥有，不在此删除
    bind = op.get_bind()
    dispute_status_enum.drop(bind, checkfirst=True)
    confirm_status_enum.drop(bind, checkfirst=True)
