"""法定节假日历与逐日出勤来源数据。"""

from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class HolidayCalendarPeriod(Base, TimestampMixin):
    """HR 对一个计薪月的法定日历完成确认后，批次才可安全核算。"""

    __tablename__ = "holiday_calendar_period"

    period: Mapped[str] = mapped_column(String(7), nullable=False, unique=True, index=True)
    is_finalized: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    finalized_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class StatutoryHolidayDate(Base, TimestampMixin):
    """一个实际法定假日及可适用的用工类型。"""

    __tablename__ = "statutory_holiday_date"

    holiday_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    # JSON 值是 EmploymentType 枚举字符串；由 API 验证为非空、无重复的
    # 受支持类型。空集意味着不适用任何员工，避免猜测法规口径。
    eligible_employment_types: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list
    )


class HolidayWorkRecord(Base, TimestampMixin):
    """员工在某个法定日的出勤来源；缺行只代表“未出勤”，绝不代表未知日期。"""

    __tablename__ = "holiday_work_record"
    __table_args__ = (
        UniqueConstraint("employee_id", "holiday_date", name="uq_holiday_work_employee_date"),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    # Historical authorization boundary captured when the source record is
    # created. Legacy rows may be null and are visible only to global HR until
    # a trustworthy historical organization can be established.
    org_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_unit.id"), nullable=True, index=True
    )
    holiday_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    worked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    reason: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    evidence_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
    recorded_by: Mapped[int | None] = mapped_column(ForeignKey("app_user.id"), nullable=True)
    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
