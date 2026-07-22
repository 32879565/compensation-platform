from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import delete, tuple_
from sqlalchemy.orm import Session

from app.dingtalk.session_throttle import ManagerSessionThrottle
from app.models.auth import LoginThrottleBucket


def test_manager_session_throttle_atomically_caps_concurrent_provider_attempts(pg_engine):
    throttle = ManagerSessionThrottle(
        ip_max_attempts=20,
        review_max_attempts=3,
        window_minutes=15,
        secret="manager-session-throttle-test-secret",
    )
    source_ip = "203.0.113.17"
    review_id = "f" * 32
    keys = throttle._keys(source_ip, review_id)
    pairs = [(key.scope, key.digest) for key in keys]

    def clean() -> None:
        with Session(pg_engine) as session:
            session.execute(
                delete(LoginThrottleBucket).where(
                    tuple_(
                        LoginThrottleBucket.scope,
                        LoginThrottleBucket.key_digest,
                    ).in_(pairs)
                )
            )
            session.commit()

    clean()
    barrier = threading.Barrier(6)

    def consume_once() -> bool:
        with Session(pg_engine) as session:
            barrier.wait(timeout=5)
            decision = throttle.consume(
                session,
                ip=source_ip,
                review_id=review_id,
            )
            session.commit()
            return decision.allowed

    try:
        with ThreadPoolExecutor(max_workers=6) as executor:
            decisions = list(executor.map(lambda _index: consume_once(), range(6)))
        assert sum(decisions) == 3
    finally:
        clean()


def test_manager_session_throttle_allows_many_review_links_behind_one_ip(pg_engine):
    throttle = ManagerSessionThrottle(
        ip_max_attempts=5,
        review_max_attempts=2,
        window_minutes=15,
        secret="manager-session-shared-ip-test-secret",
    )
    source_ip = "203.0.113.18"
    review_ids = [f"{index:x}" * 32 for index in range(6)]
    pairs = {
        (key.scope, key.digest)
        for review_id in review_ids
        for key in throttle._keys(source_ip, review_id)
    }

    def clean() -> None:
        with Session(pg_engine) as session:
            session.execute(
                delete(LoginThrottleBucket).where(
                    tuple_(
                        LoginThrottleBucket.scope,
                        LoginThrottleBucket.key_digest,
                    ).in_(list(pairs))
                )
            )
            session.commit()

    clean()
    try:
        decisions = []
        for review_id in review_ids:
            with Session(pg_engine) as session:
                decision = throttle.consume(
                    session,
                    ip=source_ip,
                    review_id=review_id,
                )
                session.commit()
                decisions.append(decision.allowed)
        assert decisions == [True, True, True, True, True, False]
    finally:
        clean()
