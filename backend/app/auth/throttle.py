"""Shared, bounded login throttling backed by PostgreSQL.

Bucket keys are domain-separated HMAC digests.  This keeps IPs and attempted
usernames out of operational state while allowing every API worker to make the
same decision.  An IP bucket is always checked before password verification;
per-account state is only added for an existing account and a pair bucket is
bounded by the IP threshold.
"""

from __future__ import annotations

import hashlib
import hmac
import ipaddress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import delete, func, select, tuple_
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.auth import LoginThrottleBucket

_PURGE_BATCH_SIZE = 1_000
_LOCK_TIMEOUT = "500ms"


class BucketScope(StrEnum):
    IP = "IP"
    ACCOUNT = "ACCOUNT"
    IP_ACCOUNT = "IP_ACCOUNT"


class ThrottleUnavailableError(RuntimeError):
    """The shared limiter could not make an atomic decision; fail closed."""


@dataclass(frozen=True)
class _BucketKey:
    scope: BucketScope
    digest: str


@dataclass(frozen=True)
class ThrottleDecision:
    ip_locked: bool
    credential_locked: bool
    retry_after_seconds: int | None


@dataclass(frozen=True)
class ThrottleFailure:
    ip_became_locked: bool
    credential_became_locked: bool
    failure_count: int


class LoginThrottle:
    """A transaction-scoped PostgreSQL login throttle.

    Every call acquires deterministic transaction advisory locks.  The router
    keeps that transaction open through credential verification, so concurrent
    requests for the same IP cannot all pass the pre-verification check.
    """

    def __init__(
        self,
        max_failures: int,
        lockout_minutes: int,
        *,
        secret: str | None = None,
    ) -> None:
        if max_failures < 1:
            raise ValueError("max_failures must be positive")
        if lockout_minutes < 1:
            raise ValueError("lockout_minutes must be positive")
        self._ip_max_failures = max_failures
        # A higher account-wide threshold limits distributed credential attacks
        # without making a single short burst an inexpensive account lockout.
        self._account_max_failures = max(max_failures * 4, max_failures + 1)
        self._lockout = timedelta(minutes=lockout_minutes)
        self._secret = (secret or get_settings().secret_key).encode("utf-8")

    @staticmethod
    def canonical_ip(value: str) -> str:
        """Normalize trusted peer IPs and collapse IPv4-mapped IPv6 forms."""

        try:
            parsed = ipaddress.ip_address(value)
        except ValueError:
            # ``Request.client`` is supplied by the trusted proxy boundary.
            # Fail closed into one shared bucket if a malformed peer leaks in.
            return "invalid-peer"
        if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
            return str(parsed.ipv4_mapped)
        return str(parsed)

    def _now(self) -> datetime:
        return datetime.now(UTC)

    def _digest(self, scope: BucketScope, value: bytes) -> str:
        # The scope is part of the authenticated input, preventing the same
        # value from correlating IP and account buckets. Rotating COMP_SECRET_KEY
        # deliberately resets at-most-lockout-duration operational state.
        message = b"comp-login-throttle-v1\0" + scope.value.encode("ascii") + b"\0" + value
        return hmac.new(self._secret, message, hashlib.sha256).hexdigest()

    def _keys(
        self, ip: str, username: str | None, account_id: int | None
    ) -> tuple[_BucketKey, ...]:
        canonical_ip = self.canonical_ip(ip)
        ip_bytes = canonical_ip.encode("ascii")
        keys = [
            _BucketKey(BucketScope.IP, self._digest(BucketScope.IP, ip_bytes)),
        ]
        if account_id is not None:
            keys.append(
                _BucketKey(
                    BucketScope.ACCOUNT,
                    self._digest(BucketScope.ACCOUNT, str(account_id).encode("ascii")),
                )
            )
        if username is not None:
            # Authentication treats usernames as exact, case-sensitive identifiers;
            # do not case-fold here or two valid accounts could cross-lock.
            pair_value = ip_bytes + b"\0" + username.encode("utf-8")
            keys.append(
                _BucketKey(
                    BucketScope.IP_ACCOUNT,
                    self._digest(BucketScope.IP_ACCOUNT, pair_value),
                )
            )
        return tuple(keys)

    @staticmethod
    def _advisory_lock_id(digest: str) -> int:
        return int.from_bytes(bytes.fromhex(digest[:16]), byteorder="big", signed=True)

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)

    def _prepare(
        self,
        session: Session,
        ip: str,
        username: str | None,
        account_id: int | None,
        *,
        purge_expired: bool,
    ) -> tuple[_BucketKey, ...]:
        keys = self._keys(ip, username, account_id)
        try:
            # A small bounded cleanup avoids a permanent table-growth path.
            # Correctness never depends on it: reads still treat expiry as
            # inactive before evaluating any lock.
            session.execute(select(func.set_config("lock_timeout", _LOCK_TIMEOUT, True)))
            for key in keys:
                session.execute(
                    select(func.pg_advisory_xact_lock(self._advisory_lock_id(key.digest)))
                )
            # Global cleanup may lock unrelated bucket rows. Run it only in
            # the pre-authentication check phase, before the router can hold a
            # User row lock. Post-auth updates reacquire only their own keys.
            if purge_expired:
                expired_ids = (
                    select(LoginThrottleBucket.id)
                    .where(LoginThrottleBucket.expires_at <= self._now())
                    .order_by(LoginThrottleBucket.expires_at)
                    .limit(_PURGE_BATCH_SIZE)
                    .scalar_subquery()
                )
                session.execute(
                    delete(LoginThrottleBucket).where(LoginThrottleBucket.id.in_(expired_ids))
                )
        except SQLAlchemyError as exc:
            raise ThrottleUnavailableError("shared login throttle is unavailable") from exc
        return keys

    def _rows(
        self, session: Session, keys: tuple[_BucketKey, ...]
    ) -> dict[tuple[BucketScope, str], LoginThrottleBucket]:
        pairs = [(key.scope.value, key.digest) for key in keys]
        try:
            rows = session.scalars(
                select(LoginThrottleBucket)
                .where(tuple_(LoginThrottleBucket.scope, LoginThrottleBucket.key_digest).in_(pairs))
                .with_for_update()
            ).all()
        except SQLAlchemyError as exc:
            raise ThrottleUnavailableError("shared login throttle is unavailable") from exc
        return {(BucketScope(row.scope), row.key_digest): row for row in rows}

    def _is_locked(self, row: LoginThrottleBucket | None, now: datetime) -> bool:
        if row is None or row.locked_until is None:
            return False
        return self._as_utc(row.expires_at) > now and self._as_utc(row.locked_until) > now

    def check(
        self, session: Session, ip: str, username: str, account_id: int | None = None
    ) -> ThrottleDecision:
        keys = self._prepare(session, ip, username, account_id, purge_expired=True)
        now = self._now()
        rows = self._rows(session, keys)
        by_scope = {key.scope: rows.get((key.scope, key.digest)) for key in keys}
        ip_row = by_scope.get(BucketScope.IP)
        ip_locked = self._is_locked(ip_row, now)
        credential_locked = any(
            self._is_locked(by_scope.get(scope), now)
            for scope in (BucketScope.ACCOUNT, BucketScope.IP_ACCOUNT)
            if scope in by_scope
        )
        retry_after = None
        if ip_locked and ip_row is not None and ip_row.locked_until is not None:
            remaining = self._as_utc(ip_row.locked_until) - now
            retry_after = max(1, int(remaining.total_seconds()) + 1)
        return ThrottleDecision(
            ip_locked=ip_locked,
            credential_locked=credential_locked,
            retry_after_seconds=retry_after,
        )

    def check_ip(self, session: Session, ip: str) -> ThrottleDecision:
        """Check only the source bucket before any account lookup or Argon2 work."""

        keys = self._prepare(session, ip, None, None, purge_expired=True)
        now = self._now()
        rows = self._rows(session, keys)
        ip_row = rows.get((BucketScope.IP, keys[0].digest))
        ip_locked = self._is_locked(ip_row, now)
        retry_after = None
        if ip_locked and ip_row is not None and ip_row.locked_until is not None:
            remaining = self._as_utc(ip_row.locked_until) - now
            retry_after = max(1, int(remaining.total_seconds()) + 1)
        return ThrottleDecision(
            ip_locked=ip_locked,
            credential_locked=False,
            retry_after_seconds=retry_after,
        )

    def _threshold(self, scope: BucketScope) -> int:
        return self._account_max_failures if scope == BucketScope.ACCOUNT else self._ip_max_failures

    def record_failure(
        self, session: Session, ip: str, username: str, account_id: int | None = None
    ) -> ThrottleFailure:
        keys = self._prepare(session, ip, username, account_id, purge_expired=False)
        now = self._now()
        rows = self._rows(session, keys)
        ip_became_locked = False
        credential_became_locked = False
        maximum_count = 0
        try:
            for key in keys:
                row = rows.get((key.scope, key.digest))
                was_locked = self._is_locked(row, now)
                if was_locked:
                    maximum_count = max(maximum_count, row.failure_count if row is not None else 0)
                    continue
                if row is None or self._as_utc(row.expires_at) <= now:
                    count = 1
                    if row is None:
                        row = LoginThrottleBucket(
                            scope=key.scope.value,
                            key_digest=key.digest,
                            failure_count=count,
                            window_started_at=now,
                            locked_until=None,
                            expires_at=now + self._lockout,
                        )
                        session.add(row)
                        rows[(key.scope, key.digest)] = row
                    else:
                        row.failure_count = count
                        row.window_started_at = now
                        row.locked_until = None
                        row.expires_at = now + self._lockout
                else:
                    count = row.failure_count + 1
                    row.failure_count = count
                    row.expires_at = now + self._lockout

                maximum_count = max(maximum_count, count)
                if count >= self._threshold(key.scope):
                    locked_until = now + self._lockout
                    row.locked_until = locked_until
                    row.expires_at = locked_until
                    if key.scope == BucketScope.IP:
                        ip_became_locked = True
                    else:
                        credential_became_locked = True
            session.flush()
        except SQLAlchemyError as exc:
            raise ThrottleUnavailableError("shared login throttle is unavailable") from exc
        return ThrottleFailure(
            ip_became_locked=ip_became_locked,
            credential_became_locked=credential_became_locked,
            failure_count=maximum_count,
        )

    def reset_after_success(
        self, session: Session, ip: str, username: str, account_id: int | None
    ) -> None:
        """Clear credential-specific failures without clearing the IP bucket."""

        keys = self._prepare(session, ip, username, account_id, purge_expired=False)
        credential_pairs = [
            (key.scope.value, key.digest) for key in keys if key.scope != BucketScope.IP
        ]
        if not credential_pairs:
            return
        try:
            session.execute(
                delete(LoginThrottleBucket).where(
                    tuple_(LoginThrottleBucket.scope, LoginThrottleBucket.key_digest).in_(
                        credential_pairs
                    )
                )
            )
        except SQLAlchemyError as exc:
            raise ThrottleUnavailableError("shared login throttle is unavailable") from exc
