import os

# 测试环境注入必填配置（database_url 为 fail-closed 必填字段）
os.environ.setdefault(
    "COMP_DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/compensation_test"
)
