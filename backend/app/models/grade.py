from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import CheckConstraint, Date, ForeignKey, Index, Integer, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base, SoftDeleteMixin, TimestampMixin

# 金额精度：全系统统一 NUMERIC(14,2)（不变量1：禁 float）
MONEY = Numeric(14, 2)


class JobGrade(Base, TimestampMixin, SoftDeleteMixin):
    """职级。rank 用于排序（数字越大级别越高）。"""

    __tablename__ = "job_grade"
    __table_args__ = (
        CheckConstraint("btrim(code) <> ''", name="ck_job_grade_code_nonblank"),
        CheckConstraint("btrim(name) <> ''", name="ck_job_grade_name_nonblank"),
        CheckConstraint("version > 0", name="ck_job_grade_version_positive"),
    )

    code: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    rank: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")

    bands: Mapped[list["SalaryBand"]] = relationship("SalaryBand", back_populates="job_grade")

    @property
    def is_active(self) -> bool:
        """Expose lifecycle state without leaking the storage-level delete flag."""

        return not self.is_deleted

    @property
    def deactivated_at(self) -> datetime | None:
        """Expose the lifecycle timestamp using catalog terminology."""

        return self.deleted_at


class SalaryBand(Base, TimestampMixin, SoftDeleteMixin):
    """薪档带宽：某职级在某生效日起的 min/mid/max。S7 做 compa-ratio 校验。"""

    __tablename__ = "salary_band"
    __table_args__ = (
        CheckConstraint(
            "band_min <> 'NaN'::numeric AND "
            "band_mid <> 'NaN'::numeric AND "
            "band_max <> 'NaN'::numeric",
            name="ck_salary_band_not_nan",
        ),
        CheckConstraint(
            "band_min >= 0 AND band_mid >= 0 AND band_max >= 0",
            name="ck_salary_band_nonnegative",
        ),
        CheckConstraint(
            "band_min <= band_mid AND band_mid <= band_max",
            name="ck_salary_band_order",
        ),
        Index(
            "uq_salary_band_grade_effective_from_active",
            "job_grade_id",
            "effective_from",
            unique=True,
            postgresql_where=text("is_deleted = false"),
        ),
    )

    job_grade_id: Mapped[int] = mapped_column(
        ForeignKey("job_grade.id"), nullable=False, index=True
    )
    band_min: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    band_mid: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    band_max: Mapped[Decimal] = mapped_column(MONEY, nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)

    job_grade: Mapped["JobGrade"] = relationship("JobGrade", back_populates="bands")
