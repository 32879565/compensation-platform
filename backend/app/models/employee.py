import enum
from datetime import date

from sqlalchemy import Date, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin


class EmploymentType(enum.StrEnum):
    FULL_TIME = "FULL_TIME"  # 全职月薪制
    PART_TIME_HOURLY = "PART_TIME_HOURLY"  # 兼职小时工（餐饮高占比，时薪制）
    LABOR = "LABOR"  # 劳务


class EmployeeStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    RESIGNED = "RESIGNED"
    SUSPENDED = "SUSPENDED"


class Employee(Base, TimestampMixin, SoftDeleteMixin):
    """员工。emp_no 为全局唯一身份键（不变量3：绝不以姓名作身份）。

    id_card_enc/bank_account_enc 存密文——列在此定义，加密工具在 S4 落地。
    position 岗位目录属主数据，放在 S5；此处先不建岗位表。
    """

    __tablename__ = "employee"

    emp_no: Mapped[str] = mapped_column(String(32), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    org_unit_id: Mapped[int] = mapped_column(ForeignKey("org_unit.id"), nullable=False, index=True)
    job_grade_id: Mapped[int | None] = mapped_column(
        ForeignKey("job_grade.id"), nullable=True, index=True
    )
    employment_type: Mapped[EmploymentType] = mapped_column(
        Enum(EmploymentType, name="employment_type"),
        nullable=False,
        default=EmploymentType.FULL_TIME,
    )
    status: Mapped[EmployeeStatus] = mapped_column(
        Enum(EmployeeStatus, name="employee_status"),
        nullable=False,
        default=EmployeeStatus.ACTIVE,
    )
    hire_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    probation_end: Mapped[date | None] = mapped_column(Date, nullable=True)
    leave_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    # 社保参保城市（可与所属门店 city 不同）；S12 按此取城市政策
    social_city: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # PII 密文列（S4 加密工具落地前存占位/明文迁移值，S4 起写密文）
    id_card_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
    bank_account_enc: Mapped[str | None] = mapped_column(String(512), nullable=True)
