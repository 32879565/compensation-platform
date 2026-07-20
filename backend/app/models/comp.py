import enum
from datetime import date
from decimal import Decimal

from sqlalchemy import Boolean, Date, Enum, ForeignKey, Integer, String, UniqueConstraint
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

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    component_type: Mapped[ComponentType] = mapped_column(
        Enum(ComponentType, name="component_type"), nullable=False
    )
    taxable: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    in_social_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    in_housing_base: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
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
            "employee_id", "component_id", "effective_from", name="uq_ess_emp_comp_from"
        ),
    )

    employee_id: Mapped[int] = mapped_column(ForeignKey("employee.id"), nullable=False, index=True)
    component_id: Mapped[int] = mapped_column(
        ForeignKey("salary_component_def.id"), nullable=False, index=True
    )
    amount: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
