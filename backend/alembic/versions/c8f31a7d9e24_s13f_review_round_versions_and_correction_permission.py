"""Preserve payroll review rounds and add the HR correction permission.

Revision ID: c8f31a7d9e24
Revises: b61e4a9037f2
Create Date: 2026-07-20 00:00:00.000000

"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "c8f31a7d9e24"
down_revision: str | None = "b61e4a9037f2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Some S13c databases were stamped before the payroll-result audit columns
    # were included in that revision.  Repair that deployed shape before any
    # review-round query references ``batch_version``.  Existing pre-round
    # results all belong to round 1; the later recovery logic deliberately
    # handles batch headers whose version had already advanced.
    op.execute(sa.text("""
            ALTER TABLE payroll_result
                ADD COLUMN IF NOT EXISTS batch_version BIGINT;
            ALTER TABLE payroll_result
                ADD COLUMN IF NOT EXISTS rule_version VARCHAR(32);
            ALTER TABLE payroll_result
                ADD COLUMN IF NOT EXISTS input_snapshot JSONB;

            UPDATE payroll_result
            SET batch_version = 1
            WHERE batch_version IS NULL;
            UPDATE payroll_result
            SET rule_version = 'legacy-pre-s13c'
            WHERE rule_version IS NULL;
            UPDATE payroll_result
            SET input_snapshot = '{}'::jsonb
            WHERE input_snapshot IS NULL;

            ALTER TABLE payroll_result ALTER COLUMN batch_version SET NOT NULL;
            ALTER TABLE payroll_result ALTER COLUMN rule_version SET NOT NULL;
            ALTER TABLE payroll_result ALTER COLUMN input_snapshot SET NOT NULL;
            """))
    # ``comp_dispute`` historically had no review-round discriminator.  When
    # one employee already has results from several batch rounds, assigning a
    # dispute to the newest round would rewrite audit history and can make an
    # old open dispute block a new round.  Refuse that genuinely ambiguous
    # legacy state so an operator can map it with source evidence first.
    op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM comp_dispute AS dispute
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM payroll_result AS result
                        WHERE result.batch_id = dispute.batch_id
                          AND result.employee_id = dispute.employee_id
                    )
                ) THEN
                    RAISE EXCEPTION
                        'S13f cannot safely assign legacy disputes to review rounds; '
                        'map disputes without matching result evidence before upgrading';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM comp_dispute AS dispute
                    JOIN payroll_result AS result
                      ON result.batch_id = dispute.batch_id
                     AND result.employee_id = dispute.employee_id
                    GROUP BY dispute.id
                    HAVING COUNT(DISTINCT result.batch_version) > 1
                ) THEN
                    RAISE EXCEPTION
                        'S13f cannot safely assign legacy disputes to review rounds; '
                        'map affected disputes before upgrading';
                END IF;
            END $$;
            """))
    # A legacy confirmation is scoped to one store and department.  Mapping it
    # to a batch-wide newest round can fabricate an active confirmation when
    # that scope only existed in an older round (for example after a transfer).
    # Refuse confirmations without any matching persisted result: there is no
    # evidence from which to recover their round safely.
    op.execute(sa.text("""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1
                    FROM batch_confirmation AS confirmation
                    JOIN payroll_result AS result
                      ON result.batch_id = confirmation.batch_id
                     AND result.org_unit_id = confirmation.org_unit_id
                     AND result.department = confirmation.department
                    GROUP BY confirmation.batch_id, confirmation.org_unit_id, confirmation.department
                    HAVING COUNT(DISTINCT result.batch_version) > 1
                ) THEN
                    RAISE EXCEPTION
                        'S13f cannot safely assign legacy confirmations to review rounds; '
                        'map multi-round confirmation scopes before upgrading';
                END IF;
                IF EXISTS (
                    SELECT 1
                    FROM batch_confirmation AS confirmation
                    WHERE NOT EXISTS (
                        SELECT 1
                        FROM payroll_result AS result
                        WHERE result.batch_id = confirmation.batch_id
                          AND result.org_unit_id = confirmation.org_unit_id
                          AND result.department = confirmation.department
                    )
                ) THEN
                    RAISE EXCEPTION
                        'S13f cannot safely assign legacy confirmations to review rounds; '
                        'map confirmations without matching result evidence before upgrading';
                END IF;
            END $$;
            """))
    # Retain enough metadata to make the legacy-round recovery reversible if a
    # deployment must be investigated.  This table is intentionally retained:
    # the migration is forward-only once review-round audit data exists.
    op.create_table(
        "payroll_batch_s13f_recovery",
        sa.Column("batch_id", sa.BigInteger(), primary_key=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("locked_by", sa.BigInteger(), nullable=True),
        sa.Column("version", sa.BigInteger(), nullable=False),
    )
    # ``batch_confirmation`` was a single mutable row per scope before S13f;
    # it cannot represent historic confirmations.  Attach it to the last
    # persisted result round for that same (store, department) scope.  This
    # avoids a phantom active confirmation when the batch-wide newest round no
    # longer contains that scope.
    op.add_column("batch_confirmation", sa.Column("batch_version", sa.BigInteger(), nullable=True))
    op.execute(sa.text("""
            UPDATE batch_confirmation AS confirmation
            SET batch_version = (
                SELECT MAX(result.batch_version)
                FROM payroll_result AS result
                WHERE result.batch_id = confirmation.batch_id
                  AND result.org_unit_id = confirmation.org_unit_id
                  AND result.department = confirmation.department
            )
            FROM payroll_batch AS batch
            WHERE batch.id = confirmation.batch_id
            """))
    op.alter_column("batch_confirmation", "batch_version", nullable=False)
    op.drop_constraint("uq_confirm_scope", "batch_confirmation", type_="unique")
    op.create_unique_constraint(
        "uq_confirm_scope",
        "batch_confirmation",
        ["batch_id", "batch_version", "org_unit_id", "department"],
    )

    op.add_column("comp_dispute", sa.Column("batch_version", sa.BigInteger(), nullable=True))
    op.execute(sa.text("""
            UPDATE comp_dispute AS dispute
            SET batch_version = (
                SELECT MAX(result.batch_version)
                FROM payroll_result AS result
                WHERE result.batch_id = dispute.batch_id
                  AND result.employee_id = dispute.employee_id
            )
            FROM payroll_batch AS batch
            WHERE batch.id = dispute.batch_id
            """))
    op.alter_column("comp_dispute", "batch_version", nullable=False)
    op.create_index("ix_comp_dispute_batch_version", "comp_dispute", ["batch_version"])

    op.execute(sa.text("""
            INSERT INTO payroll_batch_s13f_recovery
                (batch_id, status, locked_at, locked_by, version)
            SELECT
                batch.id,
                batch.status::text,
                batch.locked_at,
                batch.locked_by,
                batch.version
            FROM payroll_batch AS batch
            WHERE batch.version <> COALESCE(
                (
                    SELECT MAX(result.batch_version)
                    FROM payroll_result AS result
                    WHERE result.batch_id = batch.id
                ),
                batch.version
            )
            """))

    # A locked batch must remain locked.  If an early implementation left its
    # header version ahead of the persisted results, restore the header to the
    # last real result round instead of silently unlocking it.
    op.execute(sa.text("""
            UPDATE payroll_batch AS batch
            SET version = (
                SELECT MAX(result.batch_version)
                FROM payroll_result AS result
                WHERE result.batch_id = batch.id
            )
            WHERE batch.status = 'LOCKED'
              AND batch.version <> (
                SELECT MAX(result.batch_version)
                FROM payroll_result AS result
                WHERE result.batch_id = batch.id
            )
            """))

    # Earlier S13c builds incremented ``payroll_batch.version`` on unlock but
    # left results/confirmations in the prior round.  Such a batch cannot be
    # rerun from its old pending state once active-round filtering is enabled.
    # Preserve the historical rows under their actual result round and recover
    # the empty current round as a controlled DRAFT for HR correction/re-run.
    op.execute(sa.text("""
            UPDATE payroll_batch AS batch
            SET status = 'DRAFT', locked_at = NULL, locked_by = NULL
            WHERE batch.status <> 'LOCKED'
              AND batch.version <> COALESCE(
                (
                    SELECT MAX(result.batch_version)
                    FROM payroll_result AS result
                    WHERE result.batch_id = batch.id
                ),
                batch.version
            )
            """))

    # Group HR has global payroll-read/final-approval authority, but store
    # review is always an explicit store+department assignment.  Remove the
    # legacy broad review grants before seeding the narrowed role mappings.
    op.execute(sa.text("""
            DELETE FROM role_permission
            USING "role" AS role, permission
            WHERE role_permission.role_id = role.id
              AND role_permission.permission_id = permission.id
              AND role.code = 'FINANCE'
              AND permission.code = 'payroll:approve'
            """))
    op.execute(sa.text("""
            DELETE FROM role_permission
            USING "role" AS role, permission
            WHERE role_permission.role_id = role.id
              AND role_permission.permission_id = permission.id
              AND role.code IN ('SUPER_ADMIN', 'GROUP_HR')
              AND permission.code = 'payroll:review'
            """))
    op.execute(sa.text("""
            INSERT INTO permission (code, name)
            VALUES
                ('payroll:read', '查看核算'),
                ('payroll:run', '执行核算'),
                ('payroll:approve', '复核核算'),
                ('payroll:correct', '解锁后更正工资源数据')
            ON CONFLICT (code) DO NOTHING
            """))
    op.execute(sa.text("""
            INSERT INTO role_permission (role_id, permission_id)
            SELECT role.id, permission.id
            FROM (
                VALUES
                    ('SUPER_ADMIN', 'payroll:read'),
                    ('SUPER_ADMIN', 'payroll:run'),
                    ('SUPER_ADMIN', 'payroll:approve'),
                    ('SUPER_ADMIN', 'payroll:correct'),
                    ('GROUP_HR', 'payroll:read'),
                    ('GROUP_HR', 'payroll:run'),
                    ('GROUP_HR', 'payroll:approve'),
                    ('GROUP_HR', 'payroll:correct'),
                    ('FINANCE', 'payroll:read'),
                    ('FINANCE', 'payroll:run'),
                    ('REGION_MANAGER', 'payroll:read'),
                    ('REGION_MANAGER', 'payroll:review'),
                    ('STORE_MANAGER', 'payroll:read'),
                    ('STORE_MANAGER', 'payroll:review'),
                    ('AUDITOR', 'payroll:read')
            ) AS mapping(role_code, permission_code)
            JOIN "role" AS role ON role.code = mapping.role_code
            JOIN permission ON permission.code = mapping.permission_code
            ON CONFLICT (role_id, permission_id) DO NOTHING
            """))


def downgrade() -> None:
    raise RuntimeError(
        "S13f is forward-only: dropping review-round columns would destroy "
        "payroll audit history. Restore a pre-upgrade database backup instead."
    )
