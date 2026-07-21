"""Add an auditable statutory-holiday calendar and result detail.

Revision ID: f8b3d12a6c44
Revises: e7c2a84d9f10
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "f8b3d12a6c44"
down_revision: str | None = "e7c2a84d9f10"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _timestamp_columns() -> list[sa.Column[object]]:
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
    op.add_column(
        "payroll_result",
        sa.Column(
            "statutory_holiday_days",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.add_column(
        "payroll_result",
        sa.Column(
            "statutory_holiday_worked_days",
            sa.Numeric(precision=14, scale=2),
            nullable=False,
            server_default="0",
        ),
    )
    op.alter_column("payroll_result", "statutory_holiday_days", server_default=None)
    op.alter_column("payroll_result", "statutory_holiday_worked_days", server_default=None)

    op.create_table(
        "holiday_calendar_period",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("is_finalized", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("finalized_by", sa.BigInteger(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["finalized_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("period"),
    )
    op.create_index(
        "ix_holiday_calendar_period_period", "holiday_calendar_period", ["period"], unique=False
    )

    op.create_table(
        "statutory_holiday_date",
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "eligible_employment_types", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        *_timestamp_columns(),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("holiday_date"),
    )
    op.create_index(
        "ix_statutory_holiday_date_holiday_date",
        "statutory_holiday_date",
        ["holiday_date"],
        unique=False,
    )

    op.create_table(
        "holiday_work_record",
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("holiday_date", sa.Date(), nullable=False),
        sa.Column("worked", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reason", sa.String(length=1000), nullable=True),
        sa.Column("evidence_url", sa.String(length=512), nullable=True),
        sa.Column("recorded_by", sa.BigInteger(), nullable=True),
        sa.Column(
            "recorded_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["recorded_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("employee_id", "holiday_date", name="uq_holiday_work_employee_date"),
    )
    op.create_index(
        "ix_holiday_work_record_employee_id", "holiday_work_record", ["employee_id"], unique=False
    )
    op.create_index(
        "ix_holiday_work_record_holiday_date", "holiday_work_record", ["holiday_date"], unique=False
    )

    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES
                ('holiday_calendar:read', '查看法定节假日日历'),
                ('holiday_calendar:write', '维护法定节假日日历')
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM (
                VALUES
                    ('SUPER_ADMIN', 'holiday_calendar:read'),
                    ('SUPER_ADMIN', 'holiday_calendar:write'),
                    ('GROUP_HR', 'holiday_calendar:read'),
                    ('GROUP_HR', 'holiday_calendar:write'),
                    ('AUDITOR', 'holiday_calendar:read')
            ) AS mapping(role_code, permission_code)
            JOIN "role" AS role ON role.code = mapping.role_code
            JOIN permission ON permission.code = mapping.permission_code
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    # Calendar rows and daily work records are source-of-truth audit data;
    # result detail is required to explain already calculated statutory pay.
    # A rollback must not silently erase either category.
    bind = op.get_bind()
    source_tables = (
        "holiday_calendar_period",
        "statutory_holiday_date",
        "holiday_work_record",
    )
    if any(bind.scalar(sa.text(f"SELECT 1 FROM {table} LIMIT 1")) for table in source_tables):
        raise RuntimeError(
            "Cannot downgrade while statutory-holiday source data exists; "
            "restore a pre-upgrade backup instead."
        )
    has_result_detail = bind.scalar(sa.text("""
            SELECT 1
            FROM payroll_result
            WHERE statutory_holiday_days <> 0
               OR statutory_holiday_worked_days <> 0
               OR statutory_holiday_days IS NULL
               OR statutory_holiday_worked_days IS NULL
            LIMIT 1
            """))
    if has_result_detail:
        raise RuntimeError(
            "Cannot downgrade while payroll results contain statutory-holiday detail; "
            "restore a pre-upgrade backup instead."
        )
    op.drop_index("ix_holiday_work_record_holiday_date", table_name="holiday_work_record")
    op.drop_index("ix_holiday_work_record_employee_id", table_name="holiday_work_record")
    op.drop_table("holiday_work_record")
    op.drop_index("ix_statutory_holiday_date_holiday_date", table_name="statutory_holiday_date")
    op.drop_table("statutory_holiday_date")
    op.drop_index("ix_holiday_calendar_period_period", table_name="holiday_calendar_period")
    op.drop_table("holiday_calendar_period")
    op.drop_column("payroll_result", "statutory_holiday_worked_days")
    op.drop_column("payroll_result", "statutory_holiday_days")
