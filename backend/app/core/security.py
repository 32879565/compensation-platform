"""口令哈希（Argon2）与 JWT access token 工具。

- 口令：Argon2id，verify 为常量时间比较（防时序攻击）。
- access token：短时效 JWT（HS256），仅承载身份（sub）；权限每请求从库加载，
  保证角色变更即时生效、可吊销。
- refresh token：不可预测的随机串（不入 JWT），仅存 sha256 摘要于库，支持服务端吊销。
"""

from __future__ import annotations

import hashlib
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import jwt
from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError

from app.core.config import get_settings

_ph = PasswordHasher()
_ALGORITHM = "HS256"
_ACCESS_TYPE = "access"


def hash_password(plain: str) -> str:
    return _ph.hash(plain)


def verify_password(password_hash: str, plain: str) -> bool:
    try:
        return _ph.verify(password_hash, plain)
    except VerifyMismatchError:
        return False
    except Exception:  # 哈希格式损坏等异常一律视为验证失败（fail-closed）
        return False


def needs_rehash(password_hash: str) -> bool:
    return _ph.check_needs_rehash(password_hash)


def create_access_token(subject: str | int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(subject),
        "type": _ACCESS_TYPE,
        "iat": now,
        "exp": now + timedelta(minutes=settings.access_token_ttl_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_access_token(token: str) -> dict:
    """解码并校验 access token；无效/过期/类型不符抛 jwt 异常或 ValueError。"""
    settings = get_settings()
    payload = jwt.decode(
        token,
        settings.secret_key,
        algorithms=[_ALGORITHM],
        options={"require": ["exp", "sub", "type"]},
    )
    if payload.get("type") != _ACCESS_TYPE:
        raise ValueError("token 类型不是 access")
    return payload


def generate_refresh_token() -> tuple[str, str]:
    """返回 (原始 token 给客户端, sha256 摘要存库)。"""
    raw = secrets.token_urlsafe(48)
    return raw, hash_refresh_token(raw)


def hash_refresh_token(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
