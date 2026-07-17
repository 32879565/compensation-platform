from app.auth.throttle import LoginThrottle


def test_lock_after_max_failures():
    t = LoginThrottle(max_failures=3, lockout_minutes=15)
    assert t.is_locked("1.1.1.1", "bob") is False
    for _ in range(3):
        t.record_failure("1.1.1.1", "bob")
    assert t.is_locked("1.1.1.1", "bob") is True


def test_reset_clears_lock():
    t = LoginThrottle(max_failures=2, lockout_minutes=15)
    t.record_failure("1.1.1.1", "bob")
    t.record_failure("1.1.1.1", "bob")
    assert t.is_locked("1.1.1.1", "bob") is True
    t.reset("1.1.1.1", "bob")
    assert t.is_locked("1.1.1.1", "bob") is False


def test_per_key_isolation():
    t = LoginThrottle(max_failures=1, lockout_minutes=15)
    t.record_failure("1.1.1.1", "bob")
    assert t.is_locked("1.1.1.1", "bob") is True
    assert t.is_locked("2.2.2.2", "bob") is False  # 不同 IP 不受影响
    assert t.is_locked("1.1.1.1", "alice") is False  # 不同账号不受影响
