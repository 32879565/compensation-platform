"""S4 audit log and PII encryption

Revision ID: 09e48d07db83
Revises: f0be15d055dc
Create Date: 2026-07-19 21:33:29.749459

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = '09e48d07db83'
down_revision: str | None = 'f0be15d055dc'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        'audit_log',
        sa.Column('ts', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('actor_user_id', sa.BigInteger(), nullable=True),
        sa.Column('actor_username', sa.String(length=64), nullable=True),
        sa.Column('action', sa.String(length=64), nullable=False),
        sa.Column('result', sa.String(length=16), server_default='SUCCESS', nullable=False),
        sa.Column('target_type', sa.String(length=48), nullable=True),
        sa.Column('target_id', sa.BigInteger(), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('detail', postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_action'), 'audit_log', ['action'], unique=False)
    op.create_index(op.f('ix_audit_log_actor_user_id'), 'audit_log', ['actor_user_id'], unique=False)
    op.create_index(op.f('ix_audit_log_ts'), 'audit_log', ['ts'], unique=False)

    # append-only：DB 层触发器阻止任何 UPDATE/DELETE（不变量5，防篡改）
    op.execute(
        """
        CREATE OR REPLACE FUNCTION audit_log_block_modify() RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'audit_log is append-only: % not allowed', TG_OP;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER audit_log_no_update_delete
        BEFORE UPDATE OR DELETE ON audit_log
        FOR EACH ROW EXECUTE FUNCTION audit_log_block_modify();
        """
    )

    # PII 列改用 EncryptedString（DB 类型仍为 VARCHAR，仅重命名，空表安全）
    op.alter_column('employee', 'id_card_enc', new_column_name='id_card')
    op.alter_column('employee', 'bank_account_enc', new_column_name='bank_account')


def downgrade() -> None:
    op.alter_column('employee', 'bank_account', new_column_name='bank_account_enc')
    op.alter_column('employee', 'id_card', new_column_name='id_card_enc')
    op.execute("DROP TRIGGER IF EXISTS audit_log_no_update_delete ON audit_log")
    op.execute("DROP FUNCTION IF EXISTS audit_log_block_modify()")
    op.drop_index(op.f('ix_audit_log_ts'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_actor_user_id'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_action'), table_name='audit_log')
    op.drop_table('audit_log')
