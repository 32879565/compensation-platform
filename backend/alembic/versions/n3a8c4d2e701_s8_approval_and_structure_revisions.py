"""Add S8 approval flow and immutable salary-structure revisions.

Revision ID: n3a8c4d2e701
Revises: m2d8e5c1a734
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "n3a8c4d2e701"
down_revision: str | None = "m2d8e5c1a734"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


approval_business_type = postgresql.ENUM(
    "SALARY_ADJUSTMENT", "COMP_APPEAL", name="approval_business_type", create_type=False
)
approval_instance_status = postgresql.ENUM(
    "PENDING",
    "APPROVED",
    "REJECTED",
    "CANCELLED",
    name="approval_instance_status",
    create_type=False,
)
approval_action_type = postgresql.ENUM(
    "APPROVE", "REJECT", "CANCEL", name="approval_action_type", create_type=False
)
salary_adjustment_status = postgresql.ENUM(
    "DRAFT",
    "PENDING",
    "APPROVED",
    "REJECTED",
    "CANCELLED",
    name="salary_adjustment_status",
    create_type=False,
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
    # ``allowance_kind`` was nullable before this revision.  Clearing a kind
    # from a non-allowance component is semantics-preserving; choosing fixed
    # versus floating for a legacy allowance is not.  Keep those rows visible
    # and fail payroll for them until HR classifies them through the component
    # API, while a NOT VALID check enforces the invariant for every new row.
    op.execute(sa.text("""
            UPDATE salary_component_def
            SET allowance_kind = NULL
            WHERE component_type::text <> 'ALLOWANCE'
              AND allowance_kind IS NOT NULL
            """))
    op.execute(sa.text("""
            ALTER TABLE salary_component_def
            ADD CONSTRAINT ck_salary_component_allowance_kind
            CHECK (
                (component_type = 'ALLOWANCE' AND allowance_kind IS NOT NULL)
                OR (component_type <> 'ALLOWANCE' AND allowance_kind IS NULL)
            ) NOT VALID
            """))

    # Preserve every same-day correction instead of mutating the original
    # value.  Existing rows are the first revision in their effective interval.
    op.add_column(
        "employee_salary_structure",
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
    )
    op.drop_constraint("uq_ess_emp_comp_from", "employee_salary_structure", type_="unique")
    op.create_unique_constraint(
        "uq_ess_emp_comp_from_revision",
        "employee_salary_structure",
        ["employee_id", "component_id", "effective_from", "revision"],
    )
    op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM employee_salary_structure
                    WHERE effective_to IS NULL
                    GROUP BY employee_id, component_id
                    HAVING COUNT(*) > 1
                ) THEN
                    RAISE EXCEPTION
                        'Cannot enforce one open salary structure interval: '
                        'repair duplicate open employee/component rows with source evidence first';
                END IF;
            END $$;
            """))
    op.create_index(
        "uq_ess_open_employee_component",
        "employee_salary_structure",
        ["employee_id", "component_id"],
        unique=True,
        postgresql_where=sa.text("effective_to IS NULL"),
    )

    for enum in (
        approval_business_type,
        approval_instance_status,
        approval_action_type,
        salary_adjustment_status,
    ):
        enum.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "approval_flow",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("business_type", approval_business_type, nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=True),
        sa.Column("min_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("max_amount", sa.Numeric(14, 2), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        *_timestamp_columns(),
        sa.Column("is_deleted", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "min_amount IS NULL OR min_amount >= 0", name="ck_approval_flow_min_amount"
        ),
        sa.CheckConstraint(
            "max_amount IS NULL OR max_amount >= 0", name="ck_approval_flow_max_amount"
        ),
        sa.CheckConstraint(
            "min_amount IS NULL OR max_amount IS NULL OR max_amount >= min_amount",
            name="ck_approval_flow_amount_range",
        ),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index(
        "ix_approval_flow_routing",
        "approval_flow",
        ["business_type", "org_unit_id", "is_active"],
        unique=False,
    )
    op.create_index(
        "ix_approval_flow_business_type", "approval_flow", ["business_type"], unique=False
    )
    op.create_index("ix_approval_flow_org_unit_id", "approval_flow", ["org_unit_id"], unique=False)

    op.create_table(
        "approval_step",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("flow_id", sa.BigInteger(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("role_code", sa.String(length=32), nullable=False),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["flow_id"], ["approval_flow.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("flow_id", "step_order", name="uq_approval_step_flow_order"),
    )
    op.create_index("ix_approval_step_flow_id", "approval_step", ["flow_id"], unique=False)

    op.create_table(
        "approval_instance",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("flow_id", sa.BigInteger(), nullable=False),
        sa.Column("business_type", approval_business_type, nullable=False),
        sa.Column("business_id", sa.BigInteger(), nullable=False),
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("status", approval_instance_status, nullable=False, server_default="PENDING"),
        sa.Column("current_step_order", sa.Integer(), nullable=True),
        sa.Column("flow_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["flow_id"], ["approval_flow.id"]),
        sa.ForeignKeyConstraint(["requester_id"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("business_type", "business_id", name="uq_approval_instance_business"),
    )
    op.create_index(
        "ix_approval_instance_todo",
        "approval_instance",
        ["status", "current_step_order", "org_unit_id"],
        unique=False,
    )
    op.create_index("ix_approval_instance_flow_id", "approval_instance", ["flow_id"], unique=False)
    op.create_index(
        "ix_approval_instance_business_type", "approval_instance", ["business_type"], unique=False
    )
    op.create_index(
        "ix_approval_instance_requester_id", "approval_instance", ["requester_id"], unique=False
    )
    op.create_index(
        "ix_approval_instance_org_unit_id", "approval_instance", ["org_unit_id"], unique=False
    )
    op.create_index("ix_approval_instance_status", "approval_instance", ["status"], unique=False)

    op.create_table(
        "approval_action",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("instance_id", sa.BigInteger(), nullable=False),
        sa.Column("step_order", sa.Integer(), nullable=False),
        sa.Column("action", approval_action_type, nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=False),
        sa.Column("comment", sa.String(length=2000), nullable=True),
        *_timestamp_columns(),
        sa.ForeignKeyConstraint(["instance_id"], ["approval_instance.id"]),
        sa.ForeignKeyConstraint(["actor_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instance_id", "step_order", name="uq_approval_action_step"),
    )
    op.create_index("ix_approval_action_actor_id", "approval_action", ["actor_id"], unique=False)
    op.create_index(
        "ix_approval_action_instance",
        "approval_action",
        ["instance_id", "created_at"],
        unique=False,
    )
    op.execute(sa.text("""
            CREATE OR REPLACE FUNCTION approval_action_block_modify() RETURNS trigger AS $$
            BEGIN
                RAISE EXCEPTION 'approval_action is append-only: % not allowed', TG_OP;
            END;
            $$ LANGUAGE plpgsql;
            CREATE TRIGGER approval_action_no_update_delete
            BEFORE UPDATE OR DELETE ON approval_action
            FOR EACH ROW EXECUTE FUNCTION approval_action_block_modify();
            """))

    op.create_table(
        "salary_adjustment",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("employee_id", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("component_id", sa.BigInteger(), nullable=False),
        sa.Column("amount", sa.Numeric(14, 2), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("reason", sa.String(length=2000), nullable=False),
        sa.Column("attachment_url", sa.String(length=512), nullable=False),
        sa.Column("requester_id", sa.BigInteger(), nullable=False),
        sa.Column("status", salary_adjustment_status, nullable=False, server_default="DRAFT"),
        sa.Column("before_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("approval_instance_id", sa.BigInteger(), nullable=True),
        sa.Column("applied_structure_id", sa.BigInteger(), nullable=True),
        *_timestamp_columns(),
        sa.CheckConstraint("amount >= 0", name="ck_salary_adjustment_amount"),
        sa.ForeignKeyConstraint(["employee_id"], ["employee.id"]),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.ForeignKeyConstraint(["component_id"], ["salary_component_def.id"]),
        sa.ForeignKeyConstraint(["requester_id"], ["app_user.id"]),
        sa.ForeignKeyConstraint(["approval_instance_id"], ["approval_instance.id"]),
        sa.ForeignKeyConstraint(["applied_structure_id"], ["employee_salary_structure.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("approval_instance_id"),
        sa.UniqueConstraint("applied_structure_id"),
    )
    op.create_index(
        "ix_salary_adjustment_org_status",
        "salary_adjustment",
        ["org_unit_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_salary_adjustment_employee_id", "salary_adjustment", ["employee_id"], unique=False
    )
    op.create_index(
        "ix_salary_adjustment_org_unit_id", "salary_adjustment", ["org_unit_id"], unique=False
    )
    op.create_index(
        "ix_salary_adjustment_component_id", "salary_adjustment", ["component_id"], unique=False
    )
    op.create_index(
        "ix_salary_adjustment_requester_id", "salary_adjustment", ["requester_id"], unique=False
    )
    op.create_index("ix_salary_adjustment_status", "salary_adjustment", ["status"], unique=False)
    op.create_index(
        "ix_salary_adjustment_approval_instance_id",
        "salary_adjustment",
        ["approval_instance_id"],
        unique=True,
    )

    op.add_column(
        "employee_salary_structure",
        sa.Column("source_adjustment_id", sa.BigInteger(), nullable=True),
    )
    op.create_foreign_key(
        "fk_ess_source_adjustment",
        "employee_salary_structure",
        "salary_adjustment",
        ["source_adjustment_id"],
        ["id"],
    )
    op.create_index(
        "ix_employee_salary_structure_source_adjustment_id",
        "employee_salary_structure",
        ["source_adjustment_id"],
        unique=False,
    )
    op.add_column(
        "employee_salary_structure",
        sa.Column("source_reason", sa.String(length=2000), nullable=True),
    )
    op.add_column(
        "employee_salary_structure",
        sa.Column("source_attachment_url", sa.String(length=512), nullable=True),
    )

    # Apply the complete approval RBAC surface idempotently.  This makes a
    # database upgraded from a pre-seeding deployment operational without a
    # separate, undocumented bootstrap command.
    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES
                ('adjustment:read', '查看调薪'),
                ('adjustment:create', '发起调薪'),
                ('adjustment:approve', '审批调薪'),
                ('approval_flow:manage', '维护审批流程')
            ON CONFLICT (code) DO NOTHING;

            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM (
                VALUES
                    ('SUPER_ADMIN', 'adjustment:read'),
                    ('SUPER_ADMIN', 'adjustment:create'),
                    ('SUPER_ADMIN', 'adjustment:approve'),
                    ('SUPER_ADMIN', 'approval_flow:manage'),
                    ('GROUP_HR', 'adjustment:read'),
                    ('GROUP_HR', 'adjustment:create'),
                    ('GROUP_HR', 'adjustment:approve'),
                    ('GROUP_HR', 'approval_flow:manage'),
                    ('REGION_MANAGER', 'adjustment:read'),
                    ('REGION_MANAGER', 'adjustment:create'),
                    ('REGION_MANAGER', 'adjustment:approve'),
                    ('STORE_MANAGER', 'adjustment:create')
            ) AS mapping(role_code, permission_code)
            JOIN "role" AS role ON role.code = mapping.role_code
            JOIN permission ON permission.code = mapping.permission_code
            ON CONFLICT (role_id, permission_id) DO NOTHING;
            """))

    # A safe group-wide fallback makes the new workflow usable immediately;
    # administrators can add narrower organization/amount routes afterwards.
    op.execute(sa.text("""
            INSERT INTO approval_flow (code, name, business_type, min_amount, is_active)
            VALUES ('SALARY_ADJUSTMENT_V1', '默认调薪审批', 'SALARY_ADJUSTMENT', 0, true)
            ON CONFLICT (code) DO NOTHING;

            INSERT INTO approval_step (flow_id, step_order, name, role_code)
            SELECT flow.id, 1, '集团HR审批', 'GROUP_HR'
            FROM approval_flow AS flow
            WHERE flow.code = 'SALARY_ADJUSTMENT_V1'
            ON CONFLICT (flow_id, step_order) DO NOTHING;
            """))


def downgrade() -> None:
    raise RuntimeError(
        "S8 approval and immutable salary-structure revisions are forward-only; "
        "restore a pre-upgrade database backup instead of deleting approval history."
    )
