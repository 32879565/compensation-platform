"""Atomic, shared attempt limiter for the public manager-session endpoint."""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.models.auth import LoginThrottleBucket

_PURGE_BATCH_SIZE = 1_000
_LOCK_TIMEOUT = "500ms"
_IP_SCOPE = "MGR_IP"
_REVIEW_SCOPE = "MGR_REVIEW"


class ManagerSessionThrottleUnavailable(RuntimeError):
    """The shared limiter could not reserve provider-call capacity."""


@dataclass(frozen=True)
class ManagerSessionThrottleDecision:
    allowed: bool
    retry_after_seconds: int | None


@dataclass(frozen=True)
class _BucketKey:
    scope: str
    digest: str


class ManagerSessionThrottle:
    """Consume fixed-window IP and review-link capacity before provider I/O."""

    def __init__(
        self,
        *,
        ip_max_attempts: int,
        review_max_attempts: int,
        window_minutes: int,
        secret: str,
    ) -> None:
        if (
            ip_max_attempts < 1
            or review_max_attempts < 1
            or ip_max_attempts < review_max_attempts
            or window_minutes < 1
            or not secret
        ):
            raise ValueError("manager session throttle configuration is invalid")
        self.ip_max_attempts = ip_max_attempts
        self.review_max_attempts = review_max_attempts
        self._window = timedelta(minutes=window_minutes)
        self._secret = secret.encode("utf-8")

    @staticmethod
    def canonical_ip(value: str) -> str:
        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            return "invalid-peer"
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(parsed)

    def _digest(self, scope: str, value: str) -> str:
        message = (
            b"comp-manager-session-throttle-v1\0"
            + scope.encode("ascii")
            + b"\0"
            + value.encode("utf-8")
        )
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _keys(self, ip: str, review_id: str) -> tuple[_BucketKey, _BucketKey]:
        return (
            _BucketKey(_IP_SCOPE, self._digest(_IP_SCOPE, self.canonical_ip(ip))),
            _BucketKey(_REVIEW_SCOPE, self._digest(_REVIEW_SCOPE, review_id)),
        )

    def _threshold(self, scope: str) -> int:
        return self.ip_max_attempts if scope == _IP_SCOPE else self.review_max_attempts

    @staticmethod
    def _advisory_lock_id(digest: str) -> int:
        return int.from_bytes(bytes.fromhex(digest[:16]), byteorder="big", signed=True)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def consume(
        self,
        session: Session,
        *,
        ip: str,
        review_id: str,
    ) -> ManagerSessionThrottleDecision:
        """Atomically reserve one attempt; the caller must immediately commit."""

        keys = self._keys(ip, review_id)
        now = datetime.now(UTC)
        try:
            session.execute(select(func.set_config("lock_timeout", _LOCK_TIMEOUT, True)))
            for key in sorted(keys, key=lambda item: item.digest):
                session.execute(
                    select(func.pg_advisory_xact_lock(self._advisory_lock_id(key.digest)))
                )
            expired_ids = (
                select(LoginThrottleBucket.id)
                .where(LoginThrottleBucket.expires_at <= now)
                .order_by(LoginThrottleBucket.expires_at)
                .limit(_PURGE_BATCH_SIZE)
                .scalar_subquery()
            )
            session.execute(
                delete(LoginThrottleBucket).where(LoginThrottleBucket.id.in_(expired_ids))
            )
            pairs = [(key.scope, key.digest) for key in keys]
            rows = session.scalars(
                select(LoginThrottleBucket)
                .where(
                    tuple_(
                        LoginThrottleBucket.scope,
                        LoginThrottleBucket.key_digest,
                    ).in_(pairs)
                )
                .with_for_update()
            ).all()
            rows_by_key = {(row.scope, row.key_digest): row for row in rows}
            blocked_until = [
                self._as_utc(row.expires_at)
                for key in keys
                if (row := rows_by_key.get((key.scope, key.digest))) is not None
                and self._as_utc(row.expires_at) > now
                and row.failure_count >= self._threshold(key.scope)
            ]
            if blocked_until:
                retry_after = max(1, int((max(blocked_until) - now).total_seconds()) + 1)
                return ManagerSessionThrottleDecision(False, retry_after)

            for key in keys:
                row = rows_by_key.get((key.scope, key.digest))
                if row is None or self._as_utc(row.expires_at) <= now:
                    expires_at = now + self._window
                    if row is None:
                        row = LoginThrottleBucket(
                            scope=key.scope,
                            key_digest=key.digest,
                            failure_count=1,
                            window_started_at=now,
                            locked_until=None,
                            expires_at=expires_at,
                        )
                        session.add(row)
                    else:
                        row.failure_count = 1
                        row.window_started_at = now
                        row.locked_until = None
                        row.expires_at = expires_at
                else:
                    row.failure_count += 1
                if row.failure_count >= self._threshold(key.scope):
                    row.locked_until = row.expires_at
            session.flush()
        except SQLAlchemyError as exc:
            raise ManagerSessionThrottleUnavailable(
                "manager session throttle is unavailable"
            ) from exc
        return ManagerSessionThrottleDecision(True, None)
