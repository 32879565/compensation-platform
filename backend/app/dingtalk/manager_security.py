"""Short-lived, purpose-bound bearer tokens for DingTalk payroll review."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.core.config import get_settings

_ALGORITHM = "HS256"
_TOKEN_TYPE = "dingtalk_manager_review"
_AUDIENCE = "dingtalk-manager-review"


class ManagerReviewTokenError(ValueError):
    """A manager-review token is invalid, expired, or bound elsewhere."""


@dataclass(frozen=True)
class ManagerReviewClaims:
    user_id: int
    delivery_id: int
    batch_version: int


def create_manager_review_token(*, user_id: int, delivery_id: int, batch_version: int) -> str:
    settings = get_settings()
    now = datetime.now(UTC)
    payload = {
        "sub": str(user_id),
        "delivery_id": delivery_id,
        "batch_version": batch_version,
        "type": _TOKEN_TYPE,
        "aud": _AUDIENCE,
        "iss": settings.app_name,
        "iat": now,
        "exp": now + timedelta(minutes=settings.dingtalk_review_session_ttl_minutes),
        "jti": uuid.uuid4().hex,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_manager_review_token(
    token: str, *, expected_delivery_id: int | None = None
) -> ManagerReviewClaims:
    settings = get_settings()
    try:
        payload = jwt.decode(
            token,
            settings.secret_key,
            algorithms=[_ALGORITHM],
            audience=_AUDIENCE,
            issuer=settings.app_name,
            options={
                "require": [
                    "exp",
                    "iat",
                    "sub",
                    "type",
                    "delivery_id",
                    "batch_version",
                    "aud",
                    "iss",
                ]
            },
        )
        if payload.get("type") != _TOKEN_TYPE:
            raise ManagerReviewTokenError("wrong token type")
        user_id = int(payload["sub"])
        delivery_id = int(payload["delivery_id"])
        batch_version = int(payload["batch_version"])
        if min(user_id, delivery_id, batch_version) <= 0:
            raise ManagerReviewTokenError("invalid token identifiers")
        if expected_delivery_id is not None and delivery_id != expected_delivery_id:
            raise ManagerReviewTokenError("token is bound to another delivery")
    except (jwt.PyJWTError, KeyError, TypeError, ValueError) as exc:
        if isinstance(exc, ManagerReviewTokenError):
            raise
        raise ManagerReviewTokenError("invalid manager-review token") from None
    return ManagerReviewClaims(
        user_id=user_id,
        delivery_id=delivery_id,
        batch_version=batch_version,
    )
