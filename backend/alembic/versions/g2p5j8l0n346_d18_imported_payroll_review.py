"""D18: link confirmed Excel imports to immutable payroll review rounds.

Revision ID: g2p5j8l0n346
Revises: f1n4i7k9m235
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "g2p5j8l0n346"
down_revision: str | None = "f1n4i7k9m235"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "dingtalk_delivery", sa.Column("period_snapshot", sa.String(length=7), nullable=True)
    )
    op.add_column(
        "dingtalk_delivery",
        sa.Column("org_unit_name_snapshot", sa.String(length=128), nullable=True),
    )
    op.execute(sa.text("""
            UPDATE dingtalk_delivery AS delivery
            SET period_snapshot = batch.period,
                org_unit_name_snapshot = organization.name
            FROM payroll_batch AS batch, org_unit AS organization
            WHERE batch.id = delivery.batch_id
              AND organization.id = delivery.org_unit_id
            """))
    op.alter_column("dingtalk_delivery", "period_snapshot", nullable=False)
    op.alter_column("dingtalk_delivery", "org_unit_name_snapshot", nullable=False)
    # Old deployments cannot prove that a PENDING notification was never sent
    # before a crash, nor that an attempted FAILED notification was rejected
    # before a later error code overwrote its original outcome.
    op.execute(sa.text("""
            UPDATE dingtalk_delivery
            SET status = 'FAILED',
                error_code = 'PROVIDER_SEND_OUTCOME_UNKNOWN',
                attempt_count = GREATEST(attempt_count, 1)
            WHERE provider_task_id IS NULL
              AND (
                    status = 'PENDING'
                    OR (
                        status = 'FAILED'
                        AND attempt_count > 0
                        AND dispatched_at IS NOT NULL
                    )
                  )
            """))

    op.add_column("import_batch", sa.Column("file_sha256", sa.String(length=64), nullable=True))
    op.create_unique_constraint(
        "uq_import_batch_period_source_file_sha",
        "import_batch",
        ["period", "source", "file_sha256"],
    )
    op.add_column("import_batch", sa.Column("published_batch_id", sa.BigInteger(), nullable=True))
    op.add_column(
        "import_batch", sa.Column("published_batch_version", sa.BigInteger(), nullable=True)
    )
    op.add_column(
        "import_batch", sa.Column("published_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.create_check_constraint(
        "ck_import_batch_published_link_complete",
        "import_batch",
        "(published_batch_id IS NULL AND published_batch_version IS NULL AND published_at IS NULL) "
        "OR (published_batch_id IS NOT NULL AND published_batch_version IS NOT NULL "
        "AND published_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_import_batch_published_version_positive",
        "import_batch",
        "published_batch_version IS NULL OR published_batch_version > 0",
    )
    op.create_unique_constraint(
        "uq_import_batch_published_round",
        "import_batch",
        ["published_batch_id", "published_batch_version"],
    )
    op.create_foreign_key(
        "fk_import_batch_published_batch_id_payroll_batch",
        "import_batch",
        "payroll_batch",
        ["published_batch_id"],
        ["id"],
    )
    op.create_index("ix_import_batch_published_batch_id", "import_batch", ["published_batch_id"])

    op.add_column(
        "payroll_result", sa.Column("source_import_batch_id", sa.BigInteger(), nullable=True)
    )
    op.create_foreign_key(
        "fk_payroll_result_source_import_batch_id_import_batch",
        "payroll_result",
        "import_batch",
        ["source_import_batch_id"],
        ["id"],
    )
    # Keep D18 atomic: an index failure must roll back the preceding columns
    # and constraints so Alembic can retry without a stranded partial schema.
    op.create_index(
        "uq_result_import_batch_employee",
        "payroll_result",
        ["source_import_batch_id", "employee_id"],
        unique=True,
        postgresql_where=sa.text("source_import_batch_id IS NOT NULL"),
    )
    op.create_index(
        "ix_payroll_result_batch_version",
        "payroll_result",
        ["batch_id", "batch_version"],
    )


def downgrade() -> None:
    connection = op.get_bind()
    # Freeze provenance writers before checking whether a destructive downgrade
    # is safe.  Without this lock, a publish could commit between the count and
    # DROP COLUMN and silently lose its import linkage.
    connection.execute(
        sa.text(
            "LOCK TABLE dingtalk_delivery, import_batch, payroll_result " "IN ACCESS EXCLUSIVE MODE"
        )
    )
    deliveries = connection.scalar(sa.text("SELECT count(*) FROM dingtalk_delivery"))
    published_imports = connection.scalar(
        sa.text("SELECT count(*) FROM import_batch WHERE published_batch_id IS NOT NULL")
    )
    imported_results = connection.scalar(
        sa.text("SELECT count(*) FROM payroll_result WHERE source_import_batch_id IS NOT NULL")
    )
    if deliveries or published_imports or imported_results:
        raise RuntimeError(
            "D18 cannot be downgraded after a notification was staged or imported payroll "
            "was published; "
            "use a forward migration that preserves audit provenance."
        )

    op.drop_index("ix_payroll_result_batch_version", table_name="payroll_result")
    op.drop_index("uq_result_import_batch_employee", table_name="payroll_result")
    op.drop_constraint(
        "fk_payroll_result_source_import_batch_id_import_batch",
        "payroll_result",
        type_="foreignkey",
    )
    op.drop_column("payroll_result", "source_import_batch_id")

    op.drop_index("ix_import_batch_published_batch_id", table_name="import_batch")
    op.drop_constraint(
        "fk_import_batch_published_batch_id_payroll_batch",
        "import_batch",
        type_="foreignkey",
    )
    op.drop_constraint("uq_import_batch_published_round", "import_batch", type_="unique")
    op.drop_constraint("ck_import_batch_published_version_positive", "import_batch", type_="check")
    op.drop_constraint("ck_import_batch_published_link_complete", "import_batch", type_="check")
    op.drop_column("import_batch", "published_at")
    op.drop_column("import_batch", "published_batch_version")
    op.drop_column("import_batch", "published_batch_id")
    op.drop_constraint("uq_import_batch_period_source_file_sha", "import_batch", type_="unique")
    op.drop_column("import_batch", "file_sha256")
    op.drop_column("dingtalk_delivery", "org_unit_name_snapshot")
    op.drop_column("dingtalk_delivery", "period_snapshot")
