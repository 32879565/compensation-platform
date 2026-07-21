import pytest
from sqlalchemy import select

from app.auth.throttle import LoginThrottle
from app.models.auth import LoginThrottleBucket

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_ip_lock_after_max_failures(db_session) -> None:
    throttle = LoginThrottle(max_failures=3, lockout_minutes=15)
    assert throttle.check(db_session, "1.1.1.1", "bob").ip_locked is False
    for _ in range(3):
        throttle.record_failure(db_session, "1.1.1.1", "bob")
    assert throttle.check(db_session, "1.1.1.1", "bob").ip_locked is True


def test_success_clears_credential_buckets_but_preserves_source_bucket(db_session) -> None:
    throttle = LoginThrottle(max_failures=3, lockout_minutes=15)
    throttle.record_failure(db_session, "1.1.1.1", "bob", account_id=7)

    throttle.reset_after_success(db_session, "1.1.1.1", "bob", account_id=7)

    assert [bucket.scope for bucket in db_session.scalars(select(LoginThrottleBucket))] == ["IP"]


def test_source_lock_applies_across_usernames_without_affecting_other_ips(db_session) -> None:
    throttle = LoginThrottle(max_failures=1, lockout_minutes=15)
    throttle.record_failure(db_session, "1.1.1.1", "alice")

    assert throttle.check(db_session, "1.1.1.1", "bob").ip_locked is True
    assert throttle.check(db_session, "2.2.2.2", "alice").ip_locked is False
