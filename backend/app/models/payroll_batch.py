import enum
from datetime import date, datetime

from sqlalchemy import BigInteger, Date, DateTime, Enum, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class BatchStatus(enum.StrEnum):
    """薪资批次状态机（规格第 1 节）。"""

    DRAFT = "DRAFT"  # 建立/待核算
    CALCULATING = "CALCULATING"  # 核算中
    PENDING_STORE_CONFIRM = "PENDING_STORE_CONFIRM"  # 待门店确认
    HAS_DISPUTE = "HAS_DISPUTE"  # 存在异议
    PENDING_HR = "PENDING_HR"  # 待人事处理
    CONFIRMED = "CONFIRMED"  # 已确认
    LOCKED = "LOCKED"  # 已锁定


class CalculationStatus(enum.StrEnum):
    """Read-model projection of calculation progress from ``BatchStatus``."""

    PENDING = "PENDING"
    CALCULATING = "CALCULATING"
    CALCULATED = "CALCULATED"


class StoreConfirmationStatus(enum.StrEnum):
    """Read-model projection of the store review dimension."""

    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    HAS_DISPUTE = "HAS_DISPUTE"
    CONFIRMED = "CONFIRMED"


class HrReviewStatus(enum.StrEnum):
    """Read-model projection of final HR review progress."""

    NOT_STARTED = "NOT_STARTED"
    PENDING = "PENDING"
    APPROVED = "APPROVED"


class LockStatus(enum.StrEnum):
    """Read-model projection of the final lock dimension."""

    UNLOCKED = "UNLOCKED"
    LOCKED = "LOCKED"


class PayrollBatch(Base, TimestampMixin):
    """月度薪资计算批次。一个薪资月份一个批次。"""

    __tablename__ = "payroll_batch"
    __table_args__ = (UniqueConstraint("period", name="uq_payroll_batch_period"),)

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    attendance_start: Mapped[date] = mapped_column(Date, nullable=False)
    attendance_end: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[BatchStatus] = mapped_column(
        Enum(BatchStatus, name="batch_status"),
        nullable=False,
        default=BatchStatus.DRAFT,
        index=True,
    )
    calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hr_reviewed_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hr_reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    locked_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # 解锁保留版本号（每次解锁+1，不覆盖历史 payroll_result）
    version: Mapped[int] = mapped_column(BigInteger, nullable=False, default=1)

    @property
    def calculation_status(self) -> CalculationStatus:
        if self.status == BatchStatus.DRAFT:
            return CalculationStatus.PENDING
        if self.status == BatchStatus.CALCULATING:
            return CalculationStatus.CALCULATING
        return CalculationStatus.CALCULATED

    @property
    def store_confirmation_status(self) -> StoreConfirmationStatus:
        if self.status in {BatchStatus.DRAFT, BatchStatus.CALCULATING}:
            return StoreConfirmationStatus.NOT_STARTED
        if self.status == BatchStatus.PENDING_STORE_CONFIRM:
            return StoreConfirmationStatus.PENDING
        if self.status == BatchStatus.HAS_DISPUTE:
            return StoreConfirmationStatus.HAS_DISPUTE
        return StoreConfirmationStatus.CONFIRMED

    @property
    def hr_review_status(self) -> HrReviewStatus:
        if self.status == BatchStatus.PENDING_HR:
            return HrReviewStatus.PENDING
        if self.status in {BatchStatus.CONFIRMED, BatchStatus.LOCKED}:
            return HrReviewStatus.APPROVED
        return HrReviewStatus.NOT_STARTED

    @property
    def lock_status(self) -> LockStatus:
        if self.status == BatchStatus.LOCKED:
            return LockStatus.LOCKED
        return LockStatus.UNLOCKED
