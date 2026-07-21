import enum
from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedString
from app.db.base import Base, TimestampMixin
from app.models.employee import Department
from app.models.grade import MONEY


class ConfirmStatus(enum.StrEnum):
    PENDING = "PENDING"  # 待确认
    CONFIRMED = "CONFIRMED"  # 已确认
    DISPUTED = "DISPUTED"  # 存在异议


class DisputeStatus(enum.StrEnum):
    OPEN = "OPEN"  # 待人事处理
    APPROVED = "APPROVED"  # 同意修改（已重算）
    REJECTED = "REJECTED"  # 驳回
    NEED_MORE = "NEED_MORE"  # 要求补充材料


class DisputeEventType(enum.StrEnum):
    RAISED = "RAISED"
    NEED_MORE = "NEED_MORE"
    SUPPLEMENTED = "SUPPLEMENTED"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class PayrollResult(Base):
    """批次内某员工的核算结果（引擎输出持久化，按 version 保留历史，不覆盖）。"""

    __tablename__ = "payroll_result"
    __table_args__ = (
        UniqueConstraint("batch_id", "employee_id", "version", name="uq_result_batch_emp_ver"),
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    # 批次解锁后的新复核轮次。与员工级 version 共同定位不可覆盖的历史结果。
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    org_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_unit.id"), nullable=True, index=True
    )
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"), nullable=False
    )
    # Immutable employee/payment identity captured with the calculation.  PII
    # remains encrypted at rest and bank/regulatory exports read only these
    # fields, never mutable employee master data.
    emp_no_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    employee_name_snapshot: Mapped[str | None] = mapped_column(String(64), nullable=True)
    id_card_snapshot: Mapped[str | None] = mapped_column(EncryptedString(512), nullable=True)
    bank_account_snapshot: Mapped[str | None] = mapped_column(EncryptedString(512), nullable=True)
    social_city_snapshot: Mapped[str | None] = mapped_column(String(32), nullable=True)
    actual_attendance_days: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    statutory_holiday_days: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )
    statutory_holiday_worked_days: Mapped[Decimal] = mapped_column(
        MONEY, nullable=False, default=Decimal(0)
    )
    gross: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    deposit: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    net: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    carry_forward: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    # 与 ``carry_forward`` 一起构成跨期结转的完整状态：工资未发时，其他
    # 扣款和新员工押金也必须延后到下一期处理，不能在内存中丢失。
    deferred_deductions: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    deferred_deposit: Mapped[Decimal] = mapped_column(MONEY, nullable=False, default=Decimal(0))
    # 结果必须携带其规则版本和入参快照，才能在规则或主数据变化后仍可追溯复算。
    rule_version: Mapped[str] = mapped_column(String(32), nullable=False)
    input_snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    lines: Mapped[list] = mapped_column(JSONB, nullable=False)
    exceptions: Mapped[list] = mapped_column(JSONB, nullable=False)
    warnings: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    has_error: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class BatchConfirmation(Base):
    """批次内某(门店,部门)的复核确认状态。负责人只见本范围。"""

    __tablename__ = "batch_confirmation"
    __table_args__ = (
        UniqueConstraint(
            "batch_id",
            "batch_version",
            "org_unit_id",
            "department",
            name="uq_confirm_scope",
        ),
    )

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    # A reopened batch starts an independent review round.  Keeping the batch
    # version here prevents old confirmations from satisfying the new round.
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"), nullable=False
    )
    status: Mapped[ConfirmStatus] = mapped_column(
        Enum(ConfirmStatus, name="confirm_status"),
        nullable=False,
        default=ConfirmStatus.PENDING,
    )
    confirmed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CompDispute(Base, TimestampMixin):
    """薪酬异议单：复核负责人对某员工某工资项提出，人事处理。"""

    __tablename__ = "comp_dispute"

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    # Disputes remain auditable after a batch is reopened, but only disputes in
    # the active batch version may block or alter the active review round.
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1, index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    salary_item: Mapped[str] = mapped_column(String(64), nullable=False)  # 具体工资项
    opinion: Mapped[str] = mapped_column(String(1000), nullable=False)  # 修改意见
    raised_by: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[DisputeStatus] = mapped_column(
        Enum(DisputeStatus, name="dispute_status"),
        nullable=False,
        default=DisputeStatus.OPEN,
    )
    resolution: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    resolved_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DisputeEvent(Base):
    """Append-only evidence and decision trail for a payroll dispute."""

    __tablename__ = "dispute_event"

    dispute_id: Mapped[int] = mapped_column(
        ForeignKey("comp_dispute.id"), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    note: Mapped[str] = mapped_column(String(1000), nullable=False)
    actor_id: Mapped[int] = mapped_column(ForeignKey("app_user.id"), nullable=False, index=True)
    attachment_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AdjustmentRecord(Base):
    """修改记录（规格8.3）：改源数据的前后值、原因、审批人、重算结果、附件。"""

    __tablename__ = "adjustment_record"

    batch_id: Mapped[int] = mapped_column(
        ForeignKey("payroll_batch.id"), nullable=False, index=True
    )
    batch_version: Mapped[int] = mapped_column(BigInteger, nullable=False, index=True)
    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    dispute_id: Mapped[int | None] = mapped_column(ForeignKey("comp_dispute.id"), nullable=True)
    item: Mapped[str] = mapped_column(String(64), nullable=False)
    before_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    after_value: Mapped[dict] = mapped_column(JSONB, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    applicant_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    approver_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    attachment_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    recompute_result: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
