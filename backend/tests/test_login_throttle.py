from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete, select
from sqlalchemy.orm import sessionmaker

from app.auth.throttle import LoginThrottle
from app.models.auth import LoginThrottleBucket

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_throttle_state_is_shared_between_database_connections(pg_engine) -> None:
    first = LoginThrottle(max_failures=3, lockout_minutes=15)
    second = LoginThrottle(max_failures=3, lockout_minutes=15)
    session_factory = sessionmaker(bind=pg_engine, future=True)
    writer = session_factory()
    reader = session_factory()

    try:
        assert first.check(writer, "203.0.113.7", "hr").ip_locked is False
        for _ in range(3):
            first.record_failure(writer, "203.0.113.7", "hr")
        writer.commit()

        assert second.check(reader, "203.0.113.7", "hr").ip_locked is True
        buckets = reader.scalars(select(LoginThrottleBucket)).all()
        assert {bucket.scope for bucket in buckets} == {"IP", "IP_ACCOUNT"}
        assert all(bucket.key_digest != "hr" for bucket in buckets)
    finally:
        reader.rollback()
        reader.close()
        writer.rollback()
        writer.execute(delete(LoginThrottleBucket))
        writer.commit()
        writer.close()


def test_expired_buckets_are_purged_by_the_shared_throttle(db_session) -> None:
    db_session.add(
        LoginThrottleBucket(
            scope="IP",
            key_digest="a" * 64,
            failure_count=5,
            window_started_at=datetime.now(UTC) - timedelta(minutes=30),
            locked_until=datetime.now(UTC) - timedelta(minutes=15),
            expires_at=datetime.now(UTC) - timedelta(minutes=1),
        )
    )
    db_session.flush()

    throttle = LoginThrottle(max_failures=5, lockout_minutes=15)
    assert throttle.check(db_session, "203.0.113.8", "hr").ip_locked is False
    assert db_session.scalars(select(LoginThrottleBucket)).all() == []


def test_success_reset_does_not_clear_the_ip_bucket(db_session) -> None:
    throttle = LoginThrottle(max_failures=3, lockout_minutes=15)
    for _ in range(2):
        throttle.record_failure(db_session, "203.0.113.9", "hr", account_id=7)

    throttle.reset_after_success(db_session, "203.0.113.9", "hr", account_id=7)
    throttle.record_failure(db_session, "203.0.113.9", "hr", account_id=7)

    decision = throttle.check(db_session, "203.0.113.9", "hr", account_id=7)
    assert decision.ip_locked is True
