"""Database locks shared by DingTalk routing and manager review access."""

from sqlalchemy import text
from sqlalchemy.orm import Session

_REVIEW_AUTHORIZATION_TABLE_LOCK = text(
    "LOCK TABLE app_user, employee, org_unit, permission, role_permission, "
    "user_review_scope, user_role IN SHARE MODE"
)


def lock_review_authorization_tables(session: Session) -> None:
    """Freeze every local row family used to authorize salary disclosure."""

    if session.get_bind().dialect.name == "postgresql":
        session.execute(_REVIEW_AUTHORIZATION_TABLE_LOCK)
