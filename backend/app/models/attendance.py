from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin
from app.models.employee import Department, EmploymentType

# 天数/工时精度：允许半天等小数
DAYS = Numeric(6, 2)
COEFF = Numeric(5, 3)


class AttendanceRecord(Base, TimestampMixin):
    """员工某计薪周期的考勤。天数/工时用 Decimal（不变量1），一员工一周期一条。"""

    __tablename__ = "attendance_record"
    __table_args__ = (UniqueConstraint("employee_id", "period", name="uq_attendance_emp_period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    # 系统按排班规则生成的原始值；``expected_days`` 仅在 HR 留下调整原因
    # 时允许与它不同，保留两者即可追溯规则和人工例外。
    generated_expected_days: Mapped[Decimal | None] = mapped_column(DAYS, nullable=True)
    expected_days_rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("expected_attendance_rule.id"), nullable=True, index=True
    )
    expected_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False)
    # 应出勤天数经人事调整时填写原因（前后值走审计/修改记录）
    expected_days_adjust_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    actual_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False)
    # 出勤工时：非特殊岗位按 厅面÷9/厨房÷9.5 折算实际出勤天数（允许小数，无最低工时门槛）
    # Nullable is meaningful: 0 denotes a real zero-work attendance record,
    # while NULL means the hourly source was never supplied and must block
    # payroll rather than being silently treated as zero.
    worked_hours: Mapped[Decimal | None] = mapped_column(DAYS, nullable=True)
    # 休息天数：特殊岗位实际出勤 = 应出勤 − 休息天数
    rest_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    overtime_hours: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    holiday_worked_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    leave_days: Mapped[Decimal] = mapped_column(DAYS, nullable=False, default=Decimal(0))
    late_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    early_leave_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class ExpectedAttendanceRule(Base, TimestampMixin):
    """按组织/岗位/用工类型匹配的可配置应出勤生成规则。"""

    __tablename__ = "expected_attendance_rule"

    name: Mapped[str] = mapped_column(String(64), nullable=False)
    org_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_unit.id"), nullable=True, index=True
    )
    employment_type: Mapped[EmploymentType | None] = mapped_column(
        Enum(EmploymentType, name="employment_type"), nullable=True
    )
    department: Mapped[Department | None] = mapped_column(
        Enum(Department, name="department"), nullable=True
    )
    position_title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    is_special_position: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # 0=周一 … 6=周日。可为空（全年无固定周休），但必须与固定月应出勤
    # 至少配置一项。
    weekly_rest_days: Mapped[list[int]] = mapped_column(JSONB, nullable=False, default=list)
    # 某些门店按月排班，不能由固定每周休息日表示。部分月按自然日比例折算。
    monthly_expected_days: Mapped[Decimal | None] = mapped_column(DAYS, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PerformanceRecord(Base, TimestampMixin):
    """员工某计薪周期的绩效系数/得分，一员工一周期一条。"""

    __tablename__ = "performance_record"
    __table_args__ = (UniqueConstraint("employee_id", "period", name="uq_performance_emp_period"),)

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    coefficient: Mapped[Decimal] = mapped_column(COEFF, nullable=False, default=Decimal("1.000"))
    score: Mapped[Decimal | None] = mapped_column(Numeric(6, 2), nullable=True)
    remark: Mapped[str | None] = mapped_column(String(255), nullable=True)
