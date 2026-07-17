import enum

from sqlalchemy import Enum, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin


class PeriodStatus(enum.StrEnum):
    OPEN = "OPEN"  # 开放录入
    CALCULATING = "CALCULATING"  # 核算中
    CLOSED = "CLOSED"  # 已封存（禁改考勤/结构）
    PAID = "PAID"  # 已发放


class PayPeriod(Base, TimestampMixin, SoftDeleteMixin):
    """计薪周期。year_month 形如 '2026-05'，全局唯一。状态机由 S13 驱动。"""

    __tablename__ = "pay_period"

    year_month: Mapped[str] = mapped_column(String(7), nullable=False, unique=True, index=True)
    status: Mapped[PeriodStatus] = mapped_column(
        Enum(PeriodStatus, name="period_status"),
        nullable=False,
        default=PeriodStatus.OPEN,
    )
