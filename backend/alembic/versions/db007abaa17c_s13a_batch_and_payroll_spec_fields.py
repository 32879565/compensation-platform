"""S13a batch and payroll spec fields

Revision ID: db007abaa17c
Revises: dcccc3935a7e
Create Date: 2026-07-19 23:20:41.758157

"""
from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'db007abaa17c'
down_revision: str | None = 'dcccc3935a7e'
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# add_column 不会自动 CREATE TYPE，需显式创建（create_table 里的 batch_status 会自动创建）
department_enum = postgresql.ENUM('DINING', 'KITCHEN', 'OTHER', name='department', create_type=False)
allowance_kind_enum = postgresql.ENUM('FIXED', 'FLOATING', name='allowance_kind', create_type=False)


def upgrade() -> None:
    # 向既有 component_type 枚举追加值（alembic 不自动检测枚举加值）。
    # PG 12+ 允许在事务内 ADD VALUE；IF NOT EXISTS 保证可重复 upgrade。
    op.execute("ALTER TYPE component_type ADD VALUE IF NOT EXISTS 'COMPREHENSIVE'")
    op.execute("ALTER TYPE component_type ADD VALUE IF NOT EXISTS 'HOUSING'")

    bind = op.get_bind()
    department_enum.create(bind, checkfirst=True)
    allowance_kind_enum.create(bind, checkfirst=True)

    op.create_table(
        'payroll_batch',
        sa.Column('period', sa.String(length=7), nullable=False),
        sa.Column('attendance_start', sa.Date(), nullable=False),
        sa.Column('attendance_end', sa.Date(), nullable=False),
        sa.Column(
            'status',
            sa.Enum(
                'DRAFT', 'CALCULATING', 'PENDING_STORE_CONFIRM', 'HAS_DISPUTE',
                'PENDING_HR', 'CONFIRMED', 'LOCKED', name='batch_status',
            ),
            nullable=False,
        ),
        sa.Column('calculated_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('locked_by', sa.BigInteger(), nullable=True),
        sa.Column('version', sa.BigInteger(), nullable=False),
        sa.Column('id', sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('period', name='uq_payroll_batch_period'),
    )
    op.create_index(op.f('ix_payroll_batch_period'), 'payroll_batch', ['period'], unique=False)
    op.create_index(op.f('ix_payroll_batch_status'), 'payroll_batch', ['status'], unique=False)

    op.add_column(
        'attendance_record',
        sa.Column('expected_days_adjust_reason', sa.String(length=255), nullable=True),
    )
    # NOT NULL 数值列补 server_default，兼容既有行
    for col in ('worked_hours', 'rest_days', 'holiday_worked_days'):
        op.add_column(
            'attendance_record',
            sa.Column(col, sa.Numeric(precision=6, scale=2), nullable=False, server_default='0'),
        )
    op.add_column(
        'employee',
        sa.Column('department', department_enum, server_default='OTHER', nullable=False),
    )
    op.add_column('employee', sa.Column('position_title', sa.String(length=64), nullable=True))
    op.add_column(
        'employee',
        sa.Column('is_special_position', sa.Boolean(), server_default='false', nullable=False),
    )
    op.add_column(
        'salary_component_def',
        sa.Column('allowance_kind', allowance_kind_enum, nullable=True),
    )


def downgrade() -> None:
    op.drop_column('salary_component_def', 'allowance_kind')
    op.drop_column('employee', 'is_special_position')
    op.drop_column('employee', 'position_title')
    op.drop_column('employee', 'department')
    op.drop_column('attendance_record', 'holiday_worked_days')
    op.drop_column('attendance_record', 'rest_days')
    op.drop_column('attendance_record', 'worked_hours')
    op.drop_column('attendance_record', 'expected_days_adjust_reason')
    op.drop_index(op.f('ix_payroll_batch_status'), table_name='payroll_batch')
    op.drop_index(op.f('ix_payroll_batch_period'), table_name='payroll_batch')
    op.drop_table('payroll_batch')
    bind = op.get_bind()
    for enum_name in ('allowance_kind', 'department', 'batch_status'):
        sa.Enum(name=enum_name).drop(bind, checkfirst=True)
    # 注：component_type 已追加值 COMPREHENSIVE/HOUSING，PG 无法删除枚举值，不回退。
