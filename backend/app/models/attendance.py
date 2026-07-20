from decimal import Decimal

from sqlalchemy import ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin

# 天数/工时精度：允许半天等小数
DAYS = Numeric(6, 2)
COEFF = Numeric(5, 3)


class AttendanceRecord(Base, TimestampMixin):
    """员工某计薪周期的考勤。天数/工时用 Decimal（不变量1），一员工一周期一条。"""

    __tablename__ = "attendance_record"
    __table_args__ = (UniqueConstraint("employee_id", "period", name="uq_attendance_emp_period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    expected_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False)
    # 应出勤天数经人事调整时填写原因（前后值走审计/修改记录）
    expected_days_adjust_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False)
    # 出勤工时：非特殊岗位按 厅面÷9/厨房÷9.5 折算实际出勤天数（允许小数，无最低工时门槛）
    worked_hours: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    # 休息天数：特殊岗位实际出勤 = 应出勤 − 休息天数
    rest_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    overtime_hours: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    holiday_worked_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    leave_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    late_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    early_leave_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class PerformanceRecord(Base, TimestampMixin):
    """员工某计薪周期的绩效系数/得分，一员工一周期一条。"""

    __tablename__ = "performance_record"
    __table_args__ = (UniqueConstraint("employee_id", "period", name="uq_performance_emp_period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    coefficient: Mapped[Decimal] = mapped_column(COEFF, nullable=False, default=Decimal("1.000"))
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
