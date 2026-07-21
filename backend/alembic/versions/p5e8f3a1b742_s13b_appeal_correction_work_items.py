"""Queue approved compensation appeals for controlled source correction.

Revision ID: p5e8f3a1b742
Revises: o4d7e2f8a631
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "p5e8f3a1b742"
down_revision: str | None = "o4d7e2f8a631"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


department_enum = postgresql.ENUM(
    "DINING", "KITCHEN", "OTHER", name="department", create_type=False
)
appeal_correction_work_status = postgresql.ENUM(
    "PENDING_TRIAGE",
    "PENDING_REOPEN",
    "HISTORICAL_SETTLEMENT_REQUIRED",
    name="appeal_correction_work_status",
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
    bind = op.get_bind()
    appeal_correction_work_status.create(bind, checkfirst=True)
    op.create_table(
        "comp_appeal_correction_work_item",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("appeal_id", sa.BigInteger(), sa.ForeignKey("comp_appeal.id"), nullable=False),
        sa.Column("batch_id", sa.BigInteger(), sa.ForeignKey("payroll_batch.id"), nullable=False),
        sa.Column("source_batch_version", sa.BigInteger(), nullable=False),
        sa.Column("org_unit_id", sa.BigInteger(), sa.ForeignKey("org_unit.id"), nullable=False),
        sa.Column("department", department_enum, nullable=False),
        sa.Column("employee_id", sa.BigInteger(), sa.ForeignKey("employee.id"), nullable=True),
        sa.Column("status", appeal_correction_work_status, nullable=False),
        *_timestamp_columns(),
        sa.UniqueConstraint("appeal_id", name="uq_appeal_correction_work_item"),
    )
    for column in (
        "appeal_id",
        "batch_id",
        "source_batch_version",
        "org_unit_id",
        "employee_id",
        "status",
    ):
        op.create_index(
            f"ix_comp_appeal_correction_work_item_{column}",
            "comp_appeal_correction_work_item",
            [column],
        )

    # Earlier deployments may already contain an appeal that was approved by
    # the generic approval engine before this explicit correction queue existed.
    # Backfill it rather than leaving a terminal-looking but unactionable state.
    op.execute(sa.text("""
            INSERT INTO comp_appeal_correction_work_item
                (appeal_id, batch_id, source_batch_version, org_unit_id, department,
                 employee_id, status, created_at, updated_at, created_by)
            SELECT appeal.id,
                   appeal.batch_id,
                   appeal.batch_version,
                   appeal.org_unit_id,
                   appeal.department,
                   appeal.employee_id,
                   CASE
                       WHEN batch.version <> appeal.batch_version
                           THEN 'HISTORICAL_SETTLEMENT_REQUIRED'
                       WHEN batch.status = 'LOCKED' THEN 'PENDING_REOPEN'
                       ELSE 'PENDING_TRIAGE'
                   END::appeal_correction_work_status,
                   now(), now(), NULL
            FROM comp_appeal AS appeal
            JOIN payroll_batch AS batch ON batch.id = appeal.batch_id
            WHERE appeal.status = 'CORRECTION_REQUIRED'
            ON CONFLICT (appeal_id) DO NOTHING
            """))


def downgrade() -> None:
    for column in (
        "status",
        "employee_id",
        "org_unit_id",
        "source_batch_version",
        "batch_id",
        "appeal_id",
    ):
        op.drop_index(
            f"ix_comp_appeal_correction_work_item_{column}",
            table_name="comp_appeal_correction_work_item",
        )
    # Keep the named enum after downgrade.  PostgreSQL named enums can outlive
    # a table (and SQLAlchemy's drop hook may try to remove it before the table
    # in a transactional downgrade).  The next upgrade uses ``checkfirst`` and
    # safely reuses it; the application-visible table and data are removed.
    op.execute(sa.text("DROP TABLE IF EXISTS comp_appeal_correction_work_item CASCADE"))
