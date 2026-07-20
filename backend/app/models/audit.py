from datetime import datetime

from sqlalchemy import BigInteger, DateTime, String, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AuditLog(Base):
    """审计日志（append-only，不变量5）。

    DB 层由触发器阻止 UPDATE/DELETE（见 S4 迁移）；应用层只 INSERT。
    detail 存脱敏后的上下文，绝不含明文口令/令牌/完整 PII。
    """

    __tablename__ = "audit_log"

    ts: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    actor_user_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True, index=True)
    actor_username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    result: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SUCCESS", server_default="SUCCESS"
    )
    target_type: Mapped[str | None] = mapped_column(String(48), nullable=True)
    target_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    # actor_user_id 无 FK：审计不可因用户被删而级联丢失
