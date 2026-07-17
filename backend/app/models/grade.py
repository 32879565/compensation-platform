from datetime import date
from decimal import Decimal

from sqlalchemy import Date, ForeignKey, Integer, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin

# 金额精度：全系统统一 NUMERIC(14,2)（不变量1：禁 float）
MONEY = Numeric(14, 2)


class JobGrade(Base, TimestampMixin, SoftDeleteMixin):
    """职级。rank 用于排序（数字越大级别越高）。"""

    __tablename__ = "job_grade"

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    bands: Mapped[list["SalaryBand"]] = relationship("SalaryBand", back_populates="job_grade")


class SalaryBand(Base, TimestampMixin, SoftDeleteMixin):
    """薪档带宽：某职级在某生效日起的 min/mid/max。S7 做 compa-ratio 校验。"""

    __tablename__ = "salary_band"

    job_grade_id: Mapped[int] = mapped_column(
        ForeignKey("job_grade.id"), nullable=False, index=True
    )
    band_min: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    band_mid: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    band_max: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    job_grade: Mapped["JobGrade"] = relationship("JobGrade", back_populates="bands")
