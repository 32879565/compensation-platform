"""认证路由：登录 / 刷新 / 登出 / 当前用户。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import get_current_principal
from app.auth.service import (
    AuthError,
    Principal,
    RefreshReuseError,
    access_token_for,
    authenticate,
    build_principal,
    get_user_by_username,
    issue_refresh_token,
    load_global_permissions,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.auth.throttle import LoginThrottle, ThrottleUnavailableError
from app.core.config import get_settings
from app.db.session import get_session

router = APIRouter(prefix="/api/auth", tags=["auth"])

_REFRESH_COOKIE = "comp_refresh"
_COOKIE_PATH = "/api/auth"
_AUTH_NO_STORE_HEADERS = {"Cache-Control": "no-store"}


def _auth_unavailable(session: Session) -> HTTPException:
    """Never issue credentials when the shared rate limiter is unavailable."""

    session.rollback()
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication is temporarily unavailable",
        headers=_AUTH_NO_STORE_HEADERS,
    )


def _ip_throttled(session: Session, retry_after_seconds: int | None) -> HTTPException:
    """Persist the read-only decision before returning the public IP throttle response."""

    try:
        session.commit()
    except SQLAlchemyError:
        return _auth_unavailable(session)
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail="尝试过于频繁，请稍后再试",
        headers={
            **_AUTH_NO_STORE_HEADERS,
            "Retry-After": str(retry_after_seconds or 1),
        },
    )


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
    global_permissions: list[str]


def _client_ip(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _set_refresh_cookie(response: Response, raw: str) -> None:
    settings = get_settings()
    response.headers["Cache-Control"] = "no-store"
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
        global_permissions=sorted(load_global_permissions(session, user_id)),
    )


@router.post("/login", response_model=TokenResponse)
def login(
    body: LoginRequest,
    request: Request,
    response: Response,
    session: Session = Depends(get_session),
) -> TokenResponse:
    response.headers["Cache-Control"] = "no-store"
    ip = _throttle.canonical_ip(_client_ip(request))
    try:
        ip_decision = _throttle.check_ip(session, ip)
    except (ThrottleUnavailableError, SQLAlchemyError):
        raise _auth_unavailable(session) from None
    if ip_decision.ip_locked:
        raise _ip_throttled(session, ip_decision.retry_after_seconds)
    try:
        # Use the exact existing account identity only for the higher distributed
        # attack threshold. Unknown usernames remain protected by the IP bucket
        # without creating unbounded account records.
        candidate = get_user_by_username(session, body.username)
        decision = _throttle.check(
            session,
            ip,
            body.username,
            account_id=candidate.id if candidate is not None else None,
        )
    except (ThrottleUnavailableError, SQLAlchemyError):
        raise _auth_unavailable(session) from None
    if decision.ip_locked:
        # The IP advisory lock makes this branch unreachable in the ordinary
        # flow, but keep the response correct if the check sequence changes.
        raise _ip_throttled(session, decision.retry_after_seconds)
    if decision.credential_locked:
        # Keep account-level locking indistinguishable from a wrong password
        # so the higher distributed threshold does not become an account probe.
        try:
            session.commit()
        except SQLAlchemyError:
            raise _auth_unavailable(session) from None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers=_AUTH_NO_STORE_HEADERS,
        )
    try:
        user = authenticate(session, body.username, body.password)
    except AuthError:
        try:
            failure = _throttle.record_failure(
                session,
                ip,
                body.username,
                account_id=candidate.id if candidate is not None else None,
            )
            # Audit only a lock transition, not every anonymous rejected
            # request. The append-only audit table remains useful under abuse.
            if failure.ip_became_locked:
                audit.record(
                    session,
                    action="auth.login",
                    result="LOCKED",
                    ip=ip,
                    detail={"scope": "ip", "failure_count": failure.failure_count},
                )
            elif failure.credential_became_locked:
                audit.record(
                    session,
                    action="auth.login",
                    result="LOCKED",
                    ip=ip,
                    detail={"scope": "credential", "failure_count": failure.failure_count},
                )
            session.commit()
        except (ThrottleUnavailableError, SQLAlchemyError):
            raise _auth_unavailable(session) from None
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="用户名或密码错误",
            headers=_AUTH_NO_STORE_HEADERS,
        ) from None

    try:
        _throttle.reset_after_success(session, ip, body.username, account_id=user.id)
        raw_refresh = issue_refresh_token(session, user.id)
        result = _principal_payload(session, user.id, user.username)
        audit.record(
            session, action="auth.login", result="SUCCESS", actor=(user.id, user.username), ip=ip
        )
        session.commit()
    except (ThrottleUnavailableError, SQLAlchemyError):
        raise _auth_unavailable(session) from None
    _set_refresh_cookie(response, raw_refresh)
    return result


@router.post(
    "/refresh",
    response_model=TokenResponse,
    responses={204: {"description": "无 refresh cookie（未登录游客）"}},
)
def refresh(
    request: Request, response: Response, session: Session = Depends(get_session)
) -> TokenResponse | Response:
    response.headers["Cache-Control"] = "no-store"
    raw = request.cookies.get(_REFRESH_COOKIE)
    if not raw:
        # 游客无 cookie 是正常情况，返 204 避免浏览器控制台报 401 错误
        response.status_code = status.HTTP_204_NO_CONTENT
        return response
    try:
        user_id, new_raw = rotate_refresh_token(session, raw)
    except RefreshReuseError:
        # 重放检测触发了该用户全部会话吊销，必须持久化这些安全副作用
        audit.record(session, action="auth.refresh_reuse", result="FAILURE", ip=_client_ip(request))
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 无效",
            headers=_AUTH_NO_STORE_HEADERS,
        ) from None
    except AuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="refresh token 无效",
            headers=_AUTH_NO_STORE_HEADERS,
        ) from None
    from app.models.auth import User

    user = session.get(User, user_id)
    if user is None or user.is_deleted or user.status != "ACTIVE":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="账号不可用",
            headers=_AUTH_NO_STORE_HEADERS,
        )
    result = _principal_payload(session, user_id, user.username)
    session.commit()
    _set_refresh_cookie(response, new_raw)
    return result


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
def logout(
    request: Request, response: Response, session: Session = Depends(get_session)
) -> Response:
    response.headers["Cache-Control"] = "no-store"
    raw = request.cookies.get(_REFRESH_COOKIE)
    if raw:
        revoke_refresh_token(session, raw)
        audit.record(session, action="auth.logout", ip=_client_ip(request))
        session.commit()
    response.delete_cookie(_REFRESH_COOKIE, path=_COOKIE_PATH)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


class MeResponse(BaseModel):
    username: str
    permissions: list[str]
    global_permissions: list[str]
    unrestricted_scope: bool


@router.get("/me", response_model=MeResponse)
def me(
    principal: Principal = Depends(get_current_principal),
    session: Session = Depends(get_session),
) -> MeResponse:
    return MeResponse(
        username=principal.username,
        permissions=sorted(principal.permissions),
        global_permissions=sorted(load_global_permissions(session, principal.user_id)),
        unrestricted_scope=principal.org_scope is None,
    )
