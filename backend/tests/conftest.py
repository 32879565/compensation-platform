import os
from collections.abc import Iterator

import pytest

# 测试环境注入必填配置（database_url 为 fail-closed 必填字段）。
# DB 相关测试用 testcontainers 起独立 Postgres，并覆盖此默认值。
os.environ.setdefault(
    "COMP_DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/compensation_test"
)


@pytest.fixture(scope="session")
def pg_engine() -> Iterator[object]:
    """会话级 Postgres 容器 + 建表（create_all）。无 docker 时跳过依赖它的测试。"""
    try:
        from testcontainers.postgres import PostgresContainer
    except ImportError:  # pragma: no cover
        pytest.skip("testcontainers 未安装")

    from sqlalchemy import create_engine

    import app.models  # noqa: F401  触发所有模型注册进 Base.metadata
    from app.db.base import Base

    with PostgresContainer("postgres:16", driver="psycopg") as pg:
        engine = create_engine(pg.get_connection_url(), future=True)
        Base.metadata.create_all(engine)
        yield engine
        engine.dispose()


@pytest.fixture
def db_session(pg_engine) -> Iterator[object]:
    """函数级会话，每个测试用例结束回滚，保证隔离。"""
    from sqlalchemy.orm import sessionmaker

    connection = pg_engine.connect()
    trans = connection.begin()
    session = sessionmaker(bind=connection, future=True)()
    try:
        yield session
    finally:
        session.close()
        trans.rollback()
        connection.close()
