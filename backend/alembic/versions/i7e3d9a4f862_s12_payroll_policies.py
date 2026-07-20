"""Add effective-dated social-insurance and cumulative-tax policy records.

Revision ID: i7e3d9a4f862
Revises: h6f2c8b9e451
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "i7e3d9a4f862"
down_revision: str | None = "h6f2c8b9e451"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "payroll_policy",
        sa.Column("city", sa.String(length=32), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("social_rules", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("monthly_basic_deduction", sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column("tax_brackets", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("is_finalized", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("finalized_by", sa.BigInteger(), nullable=True),
        sa.Column("finalized_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["finalized_by"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("city", "effective_from", name="uq_payroll_policy_city_effective_from"),
    )
    op.create_index("ix_payroll_policy_city", "payroll_policy", ["city"], unique=False)
    op.create_index(
        "ix_payroll_policy_effective_from", "payroll_policy", ["effective_from"], unique=False
    )

    op.create_table(
        "employee_tax_deduction",
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("monthly_special_deduction", sa.Numeric(precision=14, scale=2), nullable=False),
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
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "employee_id",
            "effective_from",
            name="uq_employee_tax_deduction_employee_effective_from",
        ),
    )
    op.create_index(
        "ix_employee_tax_deduction_employee_id",
        "employee_tax_deduction",
        ["employee_id"],
        unique=False,
    )
    op.create_index(
        "ix_employee_tax_deduction_effective_from",
        "employee_tax_deduction",
        ["effective_from"],
        unique=False,
    )

    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES
                ('policy:read', 'View payroll policy'),
                ('policy:write', 'Manage payroll policy')
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM (
                VALUES
                    ('SUPER_ADMIN', 'policy:read'),
                    ('SUPER_ADMIN', 'policy:write'),
                    ('GROUP_HR', 'policy:read'),
                    ('GROUP_HR', 'policy:write'),
                    ('FINANCE', 'policy:read'),
                    ('AUDITOR', 'policy:read')
            ) AS mapping(role_code, permission_code)
            JOIN "role" AS role ON role.code = mapping.role_code
            JOIN permission ON permission.code = mapping.permission_code
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT 1 FROM payroll_policy LIMIT 1")) is not None:
        raise RuntimeError(
            "Cannot downgrade while payroll policy data exists; "
            "restore a pre-upgrade backup instead."
        )
    if bind.scalar(sa.text("SELECT 1 FROM employee_tax_deduction LIMIT 1")) is not None:
        raise RuntimeError(
            "Cannot downgrade while employee tax-deduction data exists; "
            "restore a pre-upgrade backup instead."
        )
    # RBAC entries may have existed before this migration because application
    # bootstrap is intentionally idempotent.  Leave them intact on downgrade.
    op.drop_index("ix_employee_tax_deduction_effective_from", table_name="employee_tax_deduction")
    op.drop_index("ix_employee_tax_deduction_employee_id", table_name="employee_tax_deduction")
    op.drop_table("employee_tax_deduction")
    op.drop_index("ix_payroll_policy_effective_from", table_name="payroll_policy")
    op.drop_index("ix_payroll_policy_city", table_name="payroll_policy")
    op.drop_table("payroll_policy")
