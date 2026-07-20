from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# 锚定到 backend/ 根，避免 env_file 随进程 CWD 漂移
_BACKEND_ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    """应用配置。全部来自环境变量 / backend/.env（前缀 COMP_），禁止硬编码敏感值。

    fail-closed：database_url 与 secret_key 均为必填，未配置时启动即
    ValidationError（不变量 4/8）。本地开发：cp deploy/.env.example backend/.env。
    """

    model_config = SettingsConfigDict(
        env_file=str(_BACKEND_ROOT / ".env"), env_prefix="COMP_", extra="ignore"
    )

    app_name: str = "compensation-platform"
    debug: bool = False
    database_url: str

    # 认证（不变量4：无默认，缺失即 fail-closed）
    secret_key: str
    # PII 列级加密口令（不变量7）。任意高熵字符串；轮换需重新加密现有数据（S17）。
    encryption_key: str
    access_token_ttl_minutes: int = 15
    refresh_token_ttl_days: int = 7
    # 登录限速（防爆破）
    login_max_failures: int = 5
    login_lockout_minutes: int = 15
    # cookie：生产必须 True（HTTPS）；本地 HTTP 开发可经 env 关掉
    cookie_secure: bool = True


@lru_cache
def get_settings() -> Settings:
    # 必填字段由 pydantic-settings 在运行时从环境/.env 注入
    return Settings()  # type: ignore[call-arg]
