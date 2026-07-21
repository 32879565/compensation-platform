"""Read-only, paginated audit-log access for authorized auditors."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.models.audit import AuditLog

router = APIRouter(prefix="/api/audit-logs", tags=["audit"])


class AuditLogOut(BaseModel):
    id: int
    ts: datetime
    actor_user_id: int | None
    actor_username: str | None
    action: str
    result: str
    target_type: str | None
    target_id: int | None
    detail: dict[str, Any] | None


class AuditLogPage(BaseModel):
    items: list[AuditLogOut]
    total: int
    page: int
    page_size: int


class _AuditFilters(BaseModel):
    action: str | None = Field(default=None, max_length=64)
    actor_username: str | None = Field(default=None, max_length=64)

    @field_validator("action", "actor_username", mode="before")
    @classmethod
    def normalize_optional_filter(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip() or None
        return value


def _out(row: AuditLog) -> AuditLogOut:
    return AuditLogOut(
        id=row.id,
        ts=row.ts,
        actor_user_id=row.actor_user_id,
        actor_username=row.actor_username,
        action=row.action,
        result=row.result,
        target_type=row.target_type,
        target_id=row.target_id,
        # Defense in depth: historic rows may predate the centralized writer
        # or have been imported, so never trust persisted JSON to be masked.
        detail=audit.mask_detail(row.detail) if row.detail is not None else None,
    )


@router.get("", response_model=AuditLogPage)
def list_audit_logs(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    action: str | None = Query(None, max_length=64),
    actor_username: str | None = Query(None, max_length=64),
    principal: Principal = Depends(require_permission(Perm.AUDIT_READ)),
    session: Session = Depends(get_session),
) -> AuditLogPage:
    filters = _AuditFilters(action=action, actor_username=actor_username)
    where = []
    if filters.action is not None:
        where.append(AuditLog.action == filters.action)
    if filters.actor_username is not None:
        where.append(AuditLog.actor_username == filters.actor_username)

    total = session.scalar(select(func.count()).select_from(AuditLog).where(*where)) or 0
    rows = list(
        session.scalars(
            select(AuditLog)
            .where(*where)
            .order_by(AuditLog.ts.desc(), AuditLog.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    response = AuditLogPage(
        items=[_out(row) for row in rows],
        total=total,
        page=page,
        page_size=page_size,
    )

    # Record after reading and constructing the response so this viewing event
    # cannot appear recursively in the page that caused it.
    audit.record(
        session,
        action="audit.log.view",
        actor=(principal.user_id, principal.username),
        target_type="audit_log",
        detail={
            "action": filters.action,
            "actor_username": filters.actor_username,
            "page": page,
            "page_size": page_size,
            "returned": len(rows),
        },
    )
    session.commit()
    return response
