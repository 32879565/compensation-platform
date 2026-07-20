import enum
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Enum, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base, TimestampMixin


class SalarySource(enum.StrEnum):
    HISTORICAL = "HISTORICAL"  # 从旧系统迁移的历史数据（只读）
    IMPORT = "IMPORT"  # Excel 导入
    PAYROLL_RUN = "PAYROLL_RUN"  # 系统核算产出（S13）


class ImportStatus(enum.StrEnum):
    PARSED = "PARSED"  # 已解析暂存，待人工核对确认
    CONFIRMED = "CONFIRMED"  # 已确认写入
    FAILED = "FAILED"


class RowStatus(enum.StrEnum):
    OK = "OK"
    ERROR = "ERROR"  # 有校验错误（无法解析金额/缺姓名等），阻断确认


class SalaryRecord(Base, TimestampMixin):
    """薪资记录（历史迁移 / 导入 / 核算）。

    fields 为灵活的规范字段字典（字段名→字符串），金额以字符串存储以保精度
    （不变量1：读取时转 Decimal，绝不用 float）。
    """

    __tablename__ = "salary_record"

    period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    emp_no: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    store_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    org_unit_id: Mapped[int | None] = mapped_column(
        ForeignKey("org_unit.id"), nullable=True, index=True
    )
    employee_id: Mapped[int | None] = mapped_column(
        ForeignKey("employee.id"), nullable=True, index=True
    )
    source: Mapped[SalarySource] = mapped_column(
        Enum(SalarySource, name="salary_source"), nullable=False, index=True
    )
    fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    import_batch_id: Mapped[int | None] = mapped_column(
        ForeignKey("import_batch.id"), nullable=True, index=True
    )


class ImportBatch(Base, TimestampMixin):
    __tablename__ = "import_batch"

    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    period: Mapped[str | None] = mapped_column(String(7), nullable=True)
    source: Mapped[SalarySource] = mapped_column(
        Enum(SalarySource, name="salary_source"), nullable=False
    )
    status: Mapped[ImportStatus] = mapped_column(
        Enum(ImportStatus, name="import_status"), nullable=False, default=ImportStatus.PARSED
    )
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ImportStagingRow(Base):
    """导入暂存行：每行带校验状态与错误清单，供人工核对后确认写库。"""

    __tablename__ = "import_staging_row"

    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batch.id"), nullable=False, index=True)
    row_index: Mapped[int] = mapped_column(Integer, nullable=False)
    sheet: Mapped[str | None] = mapped_column(String(128), nullable=True)
    period: Mapped[str] = mapped_column(String(7), nullable=False)
    emp_no: Mapped[str | None] = mapped_column(String(32), nullable=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    store_name: Mapped[str] = mapped_column(String(128), nullable=False)
    parsed_fields: Mapped[dict] = mapped_column(JSONB, nullable=False)
    errors: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    status: Mapped[RowStatus] = mapped_column(
        Enum(RowStatus, name="row_status"), nullable=False, default=RowStatus.OK
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # 关联到已确认写入的 salary_record（确认后回填）
    salary_record_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
