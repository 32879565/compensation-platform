"""认证路由：登录 / 刷新 / 登出 / 当前用户。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.deps import get_current_principal
from app.auth.service import (
    AuthError,
    Principal,
    RefreshReuseError,
    access_token_for,
    authenticate,
    build_principal,
    issue_refresh_token,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.auth.throttle import LoginThrottle
from app.core.config import get_settings
from app.db.session import get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

_REFRESH_COOKIE = "comp_refresh"
_COOKIE_PATH = "/api/auth"

_settings = get_settings()
_throttle = LoginThrottle(
    max_failures=_settings.login_max_failures,
    lockout_minutes=_settings.login_lockout_minutes,
)


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=1, max_length=256)


class TokenResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    username: str
    permissions: list[str]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_refresh_cookie(response: Response, raw: str) -> None:
    settings = get_settings()
    response.set_cookie(
        key=_REFRESH_COOKIE,
        value=raw,
        max_age=settings.refresh_token_ttl_days * 24 * 3600,
        httponly=True,
        secure=settings.cookie_secure,
        # Strict：该 cookie 只由同源 SPA 的 XHR 使用，无需跨站导航携带
        samesite="strict",
        path=_COOKIE_PATH,
    )


def _principal_payload(session: Session, user_id: int, username: str) -> TokenResponse:
    from app.models.auth import User

    user = session.get(User, user_id)
    principal = build_principal(session, user)  # type: ignore[arg-type]
    return TokenResponse(
        access_token=access_token_for(user_id),
        username=username,
        permissions=sorted(principal.permissions),
    )


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> TokenResponse:
    ip = _client_ip(request)
    if _throttle.is_locked(ip, body.username):
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="尝试过于频繁，请稍后再试",
        )
    try:
        user = authenticate(session, body.username, body.password)
    except AuthError:
        _throttle.record_failure(ip, body.username)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="用户名或密码错误"
        ) from None

    _throttle.reset(ip, body.username)
    raw_refresh = issue_refresh_token(session, user.id)
    result = _principal_payload(session, user.id, user.username)
    session.commit()
    _set_refresh_cookie(response, raw_refresh)
    return result


@router.post("/refresh", response_model=TokenResponse)
def refresh(
    request: Request, response: Response, session: Session = Depends(get_session)
) -> TokenResponse:
    raw = request.cookies.get(_REFRESH_COOKIE)
    if not raw:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="缺少 refresh token")
    try:
        user_id, new_raw = rotate_refresh_token(session, raw)
    except RefreshReuseError:
        # 重放检测触发了该用户全部会话吊销，必须持久化这些安全副作用
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效"
        ) from None
    except AuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="refresh token 无效"
        ) from None
    from app.models.auth import User

    user = session.get(User, user_id)
    if user is None or user.is_deleted or user.status != "ACTIVE":
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="账号不可用")
    result = _principal_payload(session, user_id, user.username)
    session.commit()
    _set_refresh_cookie(response, new_raw)
    return result


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request, response: Response, session: Session = Depends(get_session)
) -> Response:
    raw = request.cookies.get(_REFRESH_COOKIE)
    if raw:
        revoke_refresh_token(session, raw)
        session.commit()
    response.delete_cookie(_REFRESH_COOKIE, path=_COOKIE_PATH)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


class MeResponse(BaseModel):
    username: str
    permissions: list[str]
    unrestricted_scope: bool


@router.get("/me", response_model=MeResponse)
def me(principal: Principal = Depends(get_current_principal)) -> MeResponse:
    return MeResponse(
        username=principal.username,
        permissions=sorted(principal.permissions),
        unrestricted_scope=principal.org_scope is None,
    )
