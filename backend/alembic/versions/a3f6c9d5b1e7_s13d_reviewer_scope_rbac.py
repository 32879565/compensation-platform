"""S13d reviewer scope and RBAC permission

Revision ID: a3f6c9d5b1e7
Revises: ede872b8c568
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a3f6c9d5b1e7"
down_revision: str | None = "ede872b8c568"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


department_enum = postgresql.ENUM(
    "DINING", "KITCHEN", "OTHER", name="department", create_type=False
)


def upgrade() -> None:
    op.create_table(
        "user_review_scope",
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=False),
        sa.Column("department", department_enum, nullable=False),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["app_user.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "org_unit_id", "department", name="uq_user_review_scope"),
    )
    op.create_index("ix_user_review_scope_org_unit_id", "user_review_scope", ["org_unit_id"])
    op.create_index("ix_user_review_scope_user_id", "user_review_scope", ["user_id"])

    # Existing databases may already have been seeded by newer application code.
    # Each statement is therefore safe to rerun and does not replace any user data.
    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES ('payroll:review', '门店复核确认/提异议')
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT r.id, permission.id
            FROM "role" AS r
            CROSS JOIN permission
            WHERE r.code IN ('REGION_MANAGER', 'STORE_MANAGER')
              AND permission.code = 'payroll:review'
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    # The permission and mappings may have predated this migration (upgrade is
    # deliberately idempotent), so removing them here could delete RBAC data
    # this migration does not own.  Leave the harmless catalog entries intact.
    op.drop_index("ix_user_review_scope_user_id", table_name="user_review_scope")
    op.drop_index("ix_user_review_scope_org_unit_id", table_name="user_review_scope")
    op.drop_table("user_review_scope")
