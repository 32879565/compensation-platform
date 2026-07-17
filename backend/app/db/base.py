from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Integer, false, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """所有 ORM 模型的声明基类；metadata 供 Alembic 使用。

    统一主键 id 定义在此，供仓储基类的泛型约束访问。
    """

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
    # S3 引入 user 表后补 FK；此处仅记录操作者 id，审计细节走 S4 audit_log。
    created_by: Mapped[int | None] = mapped_column(Integer, nullable=True)


class SoftDeleteMixin:
    """软删除。仓储基类默认过滤 is_deleted=True 的行。"""

    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=false()
    )
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
