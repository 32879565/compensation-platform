from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import delete, event, select
from sqlalchemy.orm import Session

from app.auth.service import (
    AuthError,
    authenticate,
    get_user_by_username,
    issue_refresh_token,
    revoke_all_for_user,
    revoke_refresh_token,
    rotate_refresh_token,
)
from app.auth.throttle import LoginThrottle
from app.core.security import hash_password
from app.models.auth import RefreshToken, User

pytestmark = pytest.mark.usefixtures("pg_engine")

_PASSWORD = "StrongPass123!"


def _seed_user(pg_engine, prefix: str) -> tuple[int, str]:
    username = f"{prefix}-{uuid4().hex}"
    with Session(pg_engine) as session:
        user = User(username=username, password_hash=hash_password(_PASSWORD))
        session.add(user)
        session.commit()
        return user.id, username


def _cleanup_user(pg_engine, user_id: int) -> None:
    with Session(pg_engine) as session:
        session.execute(delete(RefreshToken).where(RefreshToken.user_id == user_id))
        session.execute(delete(User).where(User.id == user_id))
        session.commit()


def _normalized_statements(session: Session) -> tuple[list[str], Callable[..., None]]:
    statements: list[str] = []
    connection = session.connection()

    def record_statement(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(" ".join(statement.upper().split()))

    event.listen(connection, "before_cursor_execute", record_statement)
    return statements, record_statement


def test_refresh_lifecycle_uses_user_before_token_lock_order(db_session) -> None:
    user = User(username=f"lock-order-{uuid4().hex}", password_hash=hash_password(_PASSWORD))
    db_session.add(user)
    db_session.flush()
    statements, listener = _normalized_statements(db_session)
    connection = db_session.connection()
    try:
        authenticated = authenticate(db_session, user.username, _PASSWORD)
        assert authenticated.id == user.id
        auth_user_lock = next(
            statement
            for statement in statements
            if "FROM APP_USER" in statement and "APP_USER.USERNAME" in statement
        )
        assert "FOR UPDATE" in auth_user_lock

        statements.clear()
        raw = issue_refresh_token(db_session, user.id)
        issue_user_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM APP_USER" in statement and "FOR UPDATE" in statement
        )
        issue_insert = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("INSERT INTO REFRESH_TOKEN")
        )
        assert issue_user_lock < issue_insert

        statements.clear()
        _user_id, rotated_raw = rotate_refresh_token(db_session, raw)
        token_lookup = next(
            index
            for index, statement in enumerate(statements)
            if statement.startswith("SELECT REFRESH_TOKEN.USER_ID")
            and "FOR UPDATE" not in statement
        )
        rotate_user_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM APP_USER" in statement and "FOR UPDATE" in statement
        )
        token_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM REFRESH_TOKEN" in statement and "FOR UPDATE" in statement
        )
        assert token_lookup < rotate_user_lock < token_lock

        statements.clear()
        revoke_refresh_token(db_session, rotated_raw)
        revoke_user_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM APP_USER" in statement and "FOR UPDATE" in statement
        )
        revoke_token_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM REFRESH_TOKEN" in statement and "FOR UPDATE" in statement
        )
        assert revoke_user_lock < revoke_token_lock

        issue_refresh_token(db_session, user.id)
        statements.clear()
        revoke_all_for_user(db_session, user.id)
        revoke_all_user_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM APP_USER" in statement and "FOR UPDATE" in statement
        )
        revoke_all_token_lock = next(
            index
            for index, statement in enumerate(statements)
            if "FROM REFRESH_TOKEN" in statement and "FOR UPDATE" in statement
        )
        assert revoke_all_user_lock < revoke_all_token_lock
        assert "ORDER BY REFRESH_TOKEN.ID" in statements[revoke_all_token_lock]
    finally:
        event.remove(connection, "before_cursor_execute", listener)


def test_post_auth_throttle_updates_do_not_repeat_global_bucket_cleanup(db_session) -> None:
    username = f"throttle-order-{uuid4().hex}"
    user = User(username=username, password_hash=hash_password(_PASSWORD))
    db_session.add(user)
    db_session.flush()
    throttle = LoginThrottle(max_failures=5, lockout_minutes=15)
    ip = "203.0.113.41"

    throttle.check(db_session, ip, username, account_id=user.id)
    authenticate(db_session, username, _PASSWORD)
    statements, listener = _normalized_statements(db_session)
    connection = db_session.connection()
    try:
        throttle.record_failure(db_session, ip, username, account_id=user.id)
        assert not any(
            row.startswith("DELETE FROM LOGIN_THROTTLE_BUCKET")
            and "LOGIN_THROTTLE_BUCKET.EXPIRES_AT" in row
            for row in statements
        )

        statements.clear()
        throttle.reset_after_success(db_session, ip, username, account_id=user.id)
        assert not any(
            row.startswith("DELETE FROM LOGIN_THROTTLE_BUCKET")
            and "LOGIN_THROTTLE_BUCKET.EXPIRES_AT" in row
            for row in statements
        )
    finally:
        event.remove(connection, "before_cursor_execute", listener)


def test_authenticate_revalidates_a_candidate_cached_before_account_disable(pg_engine) -> None:
    user_id, username = _seed_user(pg_engine, "login-revalidate")
    try:
        with Session(pg_engine) as login_session:
            candidate = get_user_by_username(login_session, username)
            assert candidate is not None and candidate.status == "ACTIVE"

            with Session(pg_engine) as admin_session:
                account = admin_session.get(User, user_id)
                assert account is not None
                account.status = "DISABLED"
                admin_session.commit()

            with pytest.raises(AuthError):
                authenticate(login_session, username, _PASSWORD)
    finally:
        _cleanup_user(pg_engine, user_id)


def test_concurrent_login_then_revoke_leaves_no_active_refresh_token(pg_engine) -> None:
    user_id, username = _seed_user(pg_engine, "login-revoke")
    authenticated = threading.Event()
    release_login = threading.Event()
    revoke_attempted_user_lock = threading.Event()
    revoke_done = threading.Event()

    def login_and_issue() -> str:
        with Session(pg_engine) as session:
            user = authenticate(session, username, _PASSWORD)
            authenticated.set()
            if not release_login.wait(timeout=5):
                raise TimeoutError("test did not release the login transaction")
            raw = issue_refresh_token(session, user.id)
            session.commit()
            return raw

    def revoke_sessions() -> None:
        with Session(pg_engine) as session:
            connection = session.connection()

            def note_user_lock_attempt(
                _conn, _cursor, statement, _parameters, _context, _executemany
            ):
                normalized = " ".join(statement.upper().split())
                if "FROM APP_USER" in normalized and "FOR UPDATE" in normalized:
                    revoke_attempted_user_lock.set()

            event.listen(connection, "before_cursor_execute", note_user_lock_attempt)
            try:
                revoke_all_for_user(session, user_id)
                session.commit()
                revoke_done.set()
            finally:
                event.remove(connection, "before_cursor_execute", note_user_lock_attempt)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            login_future = executor.submit(login_and_issue)
            assert authenticated.wait(timeout=5)
            revoke_future = executor.submit(revoke_sessions)
            assert revoke_attempted_user_lock.wait(timeout=5)
            assert not revoke_done.wait(timeout=0.2)
            release_login.set()
            raw = login_future.result(timeout=5)
            revoke_future.result(timeout=5)

        with Session(pg_engine) as session:
            active = session.scalars(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
            ).all()
            assert active == []
            with pytest.raises(AuthError):
                rotate_refresh_token(session, raw)
    finally:
        release_login.set()
        _cleanup_user(pg_engine, user_id)


@pytest.mark.parametrize("revoke_mode", ["account", "presented_token"])
def test_concurrent_refresh_then_revoke_revokes_the_rotated_token(
    pg_engine, revoke_mode: str
) -> None:
    user_id, _username = _seed_user(pg_engine, "refresh-revoke")
    with Session(pg_engine) as session:
        raw = issue_refresh_token(session, user_id)
        session.commit()

    refresh_holds_user = threading.Event()
    release_refresh = threading.Event()
    revoke_attempted_user_lock = threading.Event()
    revoke_done = threading.Event()

    def rotate_while_paused_after_user_lock() -> str:
        with Session(pg_engine) as session:
            connection = session.connection()

            def pause_after_user_lock(
                _conn, _cursor, statement, _parameters, _context, _executemany
            ):
                normalized = " ".join(statement.upper().split())
                if (
                    "FROM APP_USER" in normalized
                    and "FOR UPDATE" in normalized
                    and not refresh_holds_user.is_set()
                ):
                    refresh_holds_user.set()
                    if not release_refresh.wait(timeout=5):
                        raise TimeoutError("test did not release the refresh transaction")

            event.listen(connection, "after_cursor_execute", pause_after_user_lock)
            try:
                _uid, rotated = rotate_refresh_token(session, raw)
                session.commit()
                return rotated
            finally:
                event.remove(connection, "after_cursor_execute", pause_after_user_lock)

    def revoke_sessions() -> None:
        with Session(pg_engine) as session:
            connection = session.connection()

            def note_user_lock_attempt(
                _conn, _cursor, statement, _parameters, _context, _executemany
            ):
                normalized = " ".join(statement.upper().split())
                if "FROM APP_USER" in normalized and "FOR UPDATE" in normalized:
                    revoke_attempted_user_lock.set()

            event.listen(connection, "before_cursor_execute", note_user_lock_attempt)
            try:
                if revoke_mode == "account":
                    revoke_all_for_user(session, user_id)
                else:
                    revoke_refresh_token(session, raw)
                session.commit()
                revoke_done.set()
            finally:
                event.remove(connection, "before_cursor_execute", note_user_lock_attempt)

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            refresh_future = executor.submit(rotate_while_paused_after_user_lock)
            assert refresh_holds_user.wait(timeout=5)
            revoke_future = executor.submit(revoke_sessions)
            assert revoke_attempted_user_lock.wait(timeout=5)
            assert not revoke_done.wait(timeout=0.2)
            release_refresh.set()
            rotated_raw = refresh_future.result(timeout=5)
            revoke_future.result(timeout=5)

        with Session(pg_engine) as session:
            active = session.scalars(
                select(RefreshToken).where(
                    RefreshToken.user_id == user_id,
                    RefreshToken.revoked_at.is_(None),
                )
            ).all()
            assert active == []
            with pytest.raises(AuthError):
                rotate_refresh_token(session, rotated_raw)
    finally:
        release_refresh.set()
        _cleanup_user(pg_engine, user_id)
