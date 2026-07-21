"""S6 salary import and records

Revision ID: 089dead90284
Revises: 09e48d07db83
Create Date: 2026-07-19 22:04:39.653464

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "089dead90284"
down_revision: str | None = "09e48d07db83"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# salary_source 被 import_batch 与 salary_record 共用，显式创建一次（create_type=False），
# 避免第二张表重复 CREATE TYPE 报错。
salary_source = postgresql.ENUM(
    "HISTORICAL", "IMPORT", "PAYROLL_RUN", name="salary_source", create_type=False
)
import_status = postgresql.ENUM(
    "PARSED", "CONFIRMED", "FAILED", name="import_status", create_type=False
)
row_status = postgresql.ENUM("OK", "ERROR", name="row_status", create_type=False)


def upgrade() -> None:
    bind = op.get_bind()
    salary_source.create(bind, checkfirst=True)
    import_status.create(bind, checkfirst=True)
    row_status.create(bind, checkfirst=True)

    op.create_table(
        "import_batch",
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("period", sa.String(length=7), nullable=True),
        sa.Column("source", salary_source, nullable=False),
        sa.Column("status", import_status, nullable=False),
        sa.Column("total_rows", sa.Integer(), nullable=False),
        sa.Column("error_rows", sa.Integer(), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "import_staging_row",
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("sheet", sa.String(length=128), nullable=True),
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("emp_no", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("store_name", sa.String(length=128), nullable=False),
        sa.Column("parsed_fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("errors", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", row_status, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("salary_record_id", sa.BigInteger(), nullable=True),
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.ForeignKeyConstraint(["batch_id"], ["import_batch.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_import_staging_row_batch_id"), "import_staging_row", ["batch_id"], unique=False
    )
    op.create_table(
        "salary_record",
        sa.Column("period", sa.String(length=7), nullable=False),
        sa.Column("emp_no", sa.String(length=32), nullable=True),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column("store_name", sa.String(length=128), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), nullable=True),
        sa.Column("employee_id", sa.BigInteger(), nullable=True),
        sa.Column("source", salary_source, nullable=False),
        sa.Column("fields", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("import_batch_id", sa.BigInteger(), nullable=True),
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
        sa.ForeignKeyConstraint(["import_batch_id"], ["import_batch.id"]),
        sa.ForeignKeyConstraint(["org_unit_id"], ["org_unit.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    for col in (
        "emp_no",
        "employee_id",
        "import_batch_id",
        "name",
        "org_unit_id",
        "period",
        "source",
        "store_name",
    ):
        op.create_index(op.f(f"ix_salary_record_{col}"), "salary_record", [col], unique=False)


def downgrade() -> None:
    for col in (
        "store_name",
        "source",
        "period",
        "org_unit_id",
        "name",
        "import_batch_id",
        "employee_id",
        "emp_no",
    ):
        op.drop_index(op.f(f"ix_salary_record_{col}"), table_name="salary_record")
    op.drop_table("salary_record")
    op.drop_index(op.f("ix_import_staging_row_batch_id"), table_name="import_staging_row")
    op.drop_table("import_staging_row")
    op.drop_table("import_batch")
    bind = op.get_bind()
    for enum in (salary_source, import_status, row_status):
        enum.drop(bind, checkfirst=True)
