import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, SoftDeleteMixin, TimestampMixin
from app.models.grade import MONEY


class ComponentType(enum.StrEnum):
    BASE = "BASE"  # 基本薪资
    COMPREHENSIVE = "COMPREHENSIVE"  # 综合薪资（出勤工资计薪基数）
    PERFORMANCE = "PERFORMANCE"  # 绩效
    POSITION = "POSITION"  # 岗位
    ALLOWANCE = "ALLOWANCE"  # 补贴
    HOUSING = "HOUSING"  # 房补
    OVERTIME = "OVERTIME"  # 加班
    DEDUCTION = "DEDUCTION"  # 扣款


class AllowanceKind(enum.StrEnum):
    """补贴细分（7.7）：固定/浮动；房补另有 15 天与入离职按比例规则。"""

    FIXED = "FIXED"  # 固定补贴
    FLOATING = "FLOATING"  # 浮动补贴


class SalaryComponentDef(Base, TimestampMixin, SoftDeleteMixin):
    """薪资组件目录。taxable/in_social_base/in_housing_base 供 S11/S12 核算引用。"""

    __tablename__ = "salary_component_def"
    __table_args__ = (
        CheckConstraint(
            "(component_type = 'ALLOWANCE' AND allowance_kind IS NOT NULL) "
            "OR (component_type <> 'ALLOWANCE' AND allowance_kind IS NULL)",
            name="ck_salary_component_allowance_kind",
        ),
        CheckConstraint(
            "component_type = 'ALLOWANCE' OR prorate_by_attendance = false",
            name="ck_salary_component_attendance_proration_allowance_only",
        ),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    component_type: Mapped[ComponentType] = mapped_column(
        Enum(ComponentType, name="component_type"), nullable=False
    )
    taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    in_social_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_housing_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prorate_by_attendance: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # 仅 ALLOWANCE 类型需区分固定/浮动；其余为空
    allowance_kind: Mapped[AllowanceKind | None] = mapped_column(
        Enum(AllowanceKind, name="allowance_kind"), nullable=True
    )
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)


class EmployeeSalaryStructure(Base, TimestampMixin):
    """员工薪资结构（生效日期化）。调薪产生新记录，绝不覆盖历史。

    某 (员工,组件) 的有效记录：effective_from <= 目标日 且 (effective_to 为空 或 > 目标日)。
    同一 (员工,组件) 的开放记录（effective_to 为空）至多一条。
    """

    __tablename__ = "employee_salary_structure"
    __table_args__ = (
        UniqueConstraint(
            "employee_id",
            "component_id",
            "effective_from",
            "revision",
            name="uq_ess_emp_comp_from_revision",
        ),
        # A single open interval is the database backstop for the service's
        # employee-row lock.  Without it, two first-time writes could both
        # create an open structure record and double-count payroll inputs.
        Index(
            "uq_ess_open_employee_component",
            "employee_id",
            "component_id",
            unique=True,
            postgresql_where=text("effective_to IS NULL"),
        ),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    component_id: Mapped[int] = mapped_column(
        ForeignKey("salary_component_def.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # Set only by the approval handler.  It gives every approved structure
    # revision a durable link back to its originating business document.
    source_adjustment_id: Mapped[int | None] = mapped_column(
        ForeignKey("salary_adjustment.id"), nullable=True, index=True
    )
    # Controlled business evidence for manually configured structure rows.
    # URLs/references are intentionally not written to generic audit JSON,
    # whose readers may not be allowed to access the underlying document.
    source_reason: Mapped[str | None] = mapped_column(String(2000), nullable=True)
    source_attachment_url: Mapped[str | None] = mapped_column(String(512), nullable=True)
