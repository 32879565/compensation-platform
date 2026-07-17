"""FastAPI 鉴权依赖：从 Bearer token 解析主体、按权限守卫。"""

from __future__ import annotations

from collections.abc import Callable

import jwt
from fastapi import Depends, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.auth.service import Principal, build_principal
from app.core.security import decode_access_token
from app.db.session import get_session
from app.models.auth import User

_UNAUTH = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="未认证",
    headers={"WWW-Authenticate": "Bearer"},
)


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise _UNAUTH
    return token


def get_current_principal(request: Request, session: Session = Depends(get_session)) -> Principal:
    token = _bearer_token(request)
    try:
        payload = decode_access_token(token)
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, ValueError, KeyError):
        raise _UNAUTH from None

    user = session.get(User, user_id)
    if user is None or user.is_deleted or user.status != "ACTIVE":
        raise _UNAUTH
    return build_principal(session, user)


def require_permission(permission: str) -> Callable[[Principal], Principal]:
    """依赖工厂：要求主体具备指定权限，否则 403。"""

    def _dep(principal: Principal = Depends(get_current_principal)) -> Principal:
        if not principal.has_permission(permission):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="无权限")
        return principal

    return _dep
