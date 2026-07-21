"""审计记录服务（append-only，不变量5）。

record() 只把一条 AuditLog 加入会话（不 commit，由调用方决定事务边界）。
detail 在写入前统一脱敏，绝不落明文口令/令牌/完整 PII。
"""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.audit.context import get_actor, get_client_ip
from app.models.audit import AuditLog

# detail 中命中这些子串的键一律打码
_SENSITIVE_KEY_HINTS = (
    "password",
    "passwd",
    "token",
    "secret",
    "authorization",
    "cookie",
    "id_card",
    "bank",
    "ssn",
    "attachment",
    "url",
    "uri",
)
_REDACTED = "***"


def _is_sensitive(key: str) -> bool:
    low = key.lower()
    return any(hint in low for hint in _SENSITIVE_KEY_HINTS)


def mask_detail(value: Any) -> Any:
    """递归脱敏：命中敏感键的值替换为 ***。"""
    if isinstance(value, dict):
        return {
            k: (_REDACTED if _is_sensitive(str(k)) else mask_detail(v)) for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [mask_detail(v) for v in value]
    return value


def record(
    session: Session,
    *,
    action: str,
    result: str = "SUCCESS",
    actor: tuple[int, str] | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    ip: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    if actor is None:
        actor = get_actor()
    actor_user_id, actor_username = actor if actor else (None, None)
    entry = AuditLog(
        action=action,
        result=result,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        target_type=target_type,
        target_id=target_id,
        ip=ip if ip is not None else get_client_ip(),
        detail=mask_detail(detail) if detail is not None else None,
    )
    session.add(entry)
    session.flush()
    return entry
