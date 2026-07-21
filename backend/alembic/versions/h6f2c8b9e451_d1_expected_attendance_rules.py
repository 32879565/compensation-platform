"""Generate expected attendance from auditable schedule rules.

Revision ID: h6f2c8b9e451
Revises: g4e9a1d7c530
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "h6f2c8b9e451"
down_revision: str | None = "g4e9a1d7c530"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_EMPLOYMENT_TYPE = postgresql.ENUM(name="employment_type", create_type=False)
_DEPARTMENT = postgresql.ENUM(name="department", create_type=False)


def upgrade() -> None:
    op.create_table(
        "expected_attendance_rule",
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=True),
        sa.Column("employment_type", _EMPLOYMENT_TYPE, nullable=True),
        sa.Column("department", _DEPARTMENT, nullable=True),
        sa.Column("position_title", sa.String(length=64), nullable=True),
        sa.Column("is_special_position", sa.Boolean(), nullable=True),
        sa.Column("weekly_rest_days", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("monthly_expected_days", sa.Numeric(precision=6, scale=2), nullable=True),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("effective_to", sa.Date(), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
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
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_expected_attendance_rule_org_unit_id",
        "expected_attendance_rule",
        ["org_unit_id"],
        unique=False,
    )
    op.add_column(
        "attendance_record",
        sa.Column("generated_expected_days", sa.Numeric(precision=6, scale=2), nullable=True),
    )
    op.add_column(
        "attendance_record",
        sa.Column("expected_days_rule_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_attendance_expected_days_rule",
        "attendance_record",
        "expected_attendance_rule",
        ["expected_days_rule_id"],
        ["id"],
    )
    op.create_index(
        "ix_attendance_record_expected_days_rule_id",
        "attendance_record",
        ["expected_days_rule_id"],
        unique=False,
    )
    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES
                ('attendance_schedule:read', '查看应出勤规则'),
                ('attendance_schedule:write', '维护应出勤规则'),
                ('attendance:expected_days:adjust', '调整应出勤天数')
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM (
                VALUES
                    ('SUPER_ADMIN', 'attendance_schedule:read'),
                    ('SUPER_ADMIN', 'attendance_schedule:write'),
                    ('SUPER_ADMIN', 'attendance:expected_days:adjust'),
                    ('GROUP_HR', 'attendance_schedule:read'),
                    ('GROUP_HR', 'attendance_schedule:write'),
                    ('GROUP_HR', 'attendance:expected_days:adjust'),
                    ('AUDITOR', 'attendance_schedule:read')
            ) AS mapping(role_code, permission_code)
            JOIN "role" AS role ON role.code = mapping.role_code
            JOIN permission ON permission.code = mapping.permission_code
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    # The rule table and persisted provenance make historical payroll bases
    # explainable.  Refuse an irreversible schema rollback once either holds
    # business data instead of silently converting auditable records to legacy
    # opaque values.
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT 1 FROM expected_attendance_rule LIMIT 1")):
        raise RuntimeError(
            "Cannot downgrade while expected-attendance rules exist; "
            "restore a pre-upgrade backup instead."
        )
    if bind.scalar(sa.text("""
            SELECT 1
            FROM attendance_record
            WHERE generated_expected_days IS NOT NULL
               OR expected_days_rule_id IS NOT NULL
            LIMIT 1
            """)):
        raise RuntimeError(
            "Cannot downgrade while attendance schedule provenance exists; "
            "restore a pre-upgrade backup instead."
        )
    op.drop_index("ix_attendance_record_expected_days_rule_id", table_name="attendance_record")
    op.drop_constraint("fk_attendance_expected_days_rule", "attendance_record", type_="foreignkey")
    op.drop_column("attendance_record", "expected_days_rule_id")
    op.drop_column("attendance_record", "generated_expected_days")
    op.drop_index("ix_expected_attendance_rule_org_unit_id", table_name="expected_attendance_rule")
    op.drop_table("expected_attendance_rule")
    # Permission rows are intentionally retained: removing mappings during a
    # schema rollback can destructively alter an operator's custom RBAC setup.
