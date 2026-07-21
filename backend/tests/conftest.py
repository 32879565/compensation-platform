import os
from collections.abc import Iterator

import pytest

# 测试环境注入必填配置（database_url/secret_key 为 fail-closed 必填字段）。
# DB 相关测试用 testcontainers 起独立 Postgres，并覆盖 database_url。
os.environ.setdefault(
    "COMP_DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/compensation_test"
)
os.environ.setdefault("COMP_SECRET_KEY", "test-secret-key-only-for-tests-not-production")
os.environ.setdefault("COMP_ENCRYPTION_KEY", "test-encryption-key-only-for-tests")
os.environ.setdefault("COMP_COOKIE_SECURE", "false")
# A developer's backend/.env may opt into docs.  Tests exercise the secure
# production default regardless of that local convenience setting.
os.environ["COMP_DEBUG"] = "false"

# 与 S4 迁移一致的 audit_log append-only 触发器（create_all 不会建触发器，
# 测试里手动补上以保真）。
_AUDIT_APPEND_ONLY_SQL = [
    """
    CREATE OR REPLACE FUNCTION audit_log_block_modify() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'audit_log is append-only: % not allowed', TG_OP;
    END;
    $$ LANGUAGE plpgsql;
    """,
    """
    CREATE TRIGGER audit_log_no_update_delete
    BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_block_modify();
    """,
]


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[object]:
    """会话级 Postgres 容器 + 建表（create_all）。无 docker 时跳过依赖它的测试。"""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers 未安装")
    from docker.errors import DockerException
    from sqlalchemy import create_engine, text

    import app.models  # noqa: F401  触发所有模型注册进 Base.metadata
    from app.db.base import Base

    try:
        with PostgresContainer("postgres:16", driver="psycopg") as pg:
            engine = create_engine(pg.get_connection_url(), future=True)
            Base.metadata.create_all(engine)
            with engine.begin() as conn:
                for stmt in _AUDIT_APPEND_ONLY_SQL:
                    conn.execute(text(stmt))
            yield engine
            engine.dispose()
    except DockerException as exc:
        # Local Windows development machines often have the Docker Python
        # client installed while the daemon/npipe integration is unavailable.
        # That is an environment skip, not an application test failure.
        pytest.skip(f"Docker unavailable for PostgreSQL integration tests: {exc}")


@pytest.fixture
def db_session(pg_engine) -> Iterator[object]:
    """函数级会话，每个测试用例结束回滚，保证隔离。

    join_transaction_mode='create_savepoint'：被测代码内部的 session.commit()
    只提交 SAVEPOINT，外层事务仍可整体回滚，从而在有 commit 的路径下保持隔离。
    """
    from sqlalchemy.orm import sessionmaker

    connection = pg_engine.connect()
    trans = connection.begin()
    session = sessionmaker(bind=connection, future=True, join_transaction_mode="create_savepoint")()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
