import enum
from datetime import date

from sqlalchemy import Boolean, Date, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.core.crypto import EncryptedString
from app.db.base import Base, SoftDeleteMixin, TimestampMixin


class EmploymentType(enum.StrEnum):
    FULL_TIME = "FULL_TIME"  # 全职月薪制
    PART_TIME_HOURLY = "PART_TIME_HOURLY"  # 兼职小时工（餐饮高占比，时薪制）
    LABOR = "LABOR"  # 劳务


class Department(enum.StrEnum):
    """部门：决定出勤折算除数与钉钉推送收件人（厅面→店长/厨房→厨房经理）。"""

    DINING = "DINING"  # 厅面（出勤工时÷9）
    KITCHEN = "KITCHEN"  # 厨房（出勤工时÷9.5）
    OTHER = "OTHER"  # 其他（后勤/管理等）


class EmployeeStatus(enum.StrEnum):
    ACTIVE = "ACTIVE"
    RESIGNED = "RESIGNED"
    SUSPENDED = "SUSPENDED"


def requires_approved_attendance_days(position_title: str | None) -> bool:
    """Return whether a named role must use HR-approved attendance days.

    The explicit employee flag remains available for company-designated roles.
    These named role families are mandatory in the payroll specification, so a
    caller cannot accidentally opt them out by leaving that flag false.
    """

    if not position_title:
        return False
    normalized = "".join(position_title.split()).replace("（", "(").replace("）", ")")
    if "洗碗" in normalized or normalized in {"寒假工", "暑假工"}:
        return True
    return ("店长" in normalized or "厨师长" in normalized) and (
        "实习" in normalized or "储备" in normalized
    )


class Employee(Base, TimestampMixin, SoftDeleteMixin):
    """员工。emp_no 为全局唯一身份键（不变量3：绝不以姓名作身份）。

    id_card/bank_account 用 EncryptedString：库中存 Fernet 密文，Python 侧读写明文
    （不变量7）。API 层展示须调用 mask_id_card/mask_bank_account 脱敏。
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
    department: Mapped[Department] = mapped_column(
        Enum(Department, name="department"),
        nullable=False,
        default=Department.OTHER,
        server_default=Department.OTHER.value,
    )
    position_title: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 特殊职位（店长/厨师长实习储备、洗碗、寒暑假工等）按审批实际出勤天数核算，不走工时折算
    is_special_position: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # 社保参保城市（可与所属门店 city 不同）；S12 按此取城市政策
    social_city: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # PII：库中密文、应用层明文（EncryptedString）
    id_card: Mapped[str | None] = mapped_column(EncryptedString(512), nullable=True)
    bank_account: Mapped[str | None] = mapped_column(EncryptedString(512), nullable=True)
    # Keyed one-way digest only: the provider userid is never stored in
    # plaintext or returned by the API.  It supports stable equality matching
    # after names/job numbers change.
    dingtalk_user_id_hash: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True
    )
