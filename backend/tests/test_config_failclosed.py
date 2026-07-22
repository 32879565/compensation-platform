import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import DingTalkMode, Settings


def test_missing_secret_key_fails_closed(monkeypatch):
    # 清空环境中的必填项，构造时不读 .env，应因缺 secret_key/database_url 而失败
    for key in ("COMP_SECRET_KEY", "COMP_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_present_required_fields_ok(monkeypatch):
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.delenv("COMP_COOKIE_SECURE", raising=False)  # 隔离 conftest 的默认值
    s = Settings(_env_file=None)
    assert s.secret_key == "a" * 48
    assert s.cookie_secure is True  # 默认安全


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("COMP_SECRET_KEY", "change-me-generate-a-long-random-secret"),
        ("COMP_ENCRYPTION_KEY", "change-me-generate-another-random-secret"),
        ("COMP_SECRET_KEY", "too-short"),
        ("COMP_DATABASE_URL", "postgresql+psycopg://comp:change-me@localhost/compensation"),
    ],
)
def test_placeholder_or_short_deployment_configuration_fails_closed(monkeypatch, name, value):
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_complete_dingtalk_credentials_remain_sandboxed_by_default(monkeypatch):
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_ID", "ding-client")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_SECRET", "c" * 48)
    monkeypatch.setenv("COMP_DINGTALK_AGENT_ID", "123")

    settings = Settings(_env_file=None)

    assert settings.dingtalk_mode is DingTalkMode.SANDBOX
    assert settings.dingtalk_credentials_configured is True
    assert settings.dingtalk_read_sync_enabled is False
    assert isinstance(settings.dingtalk_client_secret, SecretStr)
    assert "c" * 48 not in repr(settings)


def test_partial_or_non_https_live_dingtalk_configuration_fails_closed(monkeypatch):
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_ID", "ding-client")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)

    monkeypatch.setenv("COMP_DINGTALK_CLIENT_SECRET", "c" * 48)
    monkeypatch.setenv("COMP_DINGTALK_AGENT_ID", "123")
    monkeypatch.setenv("COMP_DINGTALK_MODE", "live")
    monkeypatch.setenv("COMP_DINGTALK_PUBLIC_BASE_URL", "http://example.test")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_live_dingtalk_manager_review_requires_corp_id(monkeypatch):
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_ID", "ding-client")
    monkeypatch.setenv("COMP_DINGTALK_CLIENT_SECRET", "c" * 48)
    monkeypatch.setenv("COMP_DINGTALK_AGENT_ID", "123")
    monkeypatch.setenv("COMP_DINGTALK_MODE", "live")
    monkeypatch.setenv("COMP_DINGTALK_PUBLIC_BASE_URL", "https://payroll.example.test")
    monkeypatch.delenv("COMP_DINGTALK_CORP_ID", raising=False)

    with pytest.raises(ValidationError, match="corp_id"):
        Settings(_env_file=None)

    monkeypatch.setenv("COMP_DINGTALK_CORP_ID", "ding-corp")
    assert Settings(_env_file=None).dingtalk_corp_id == "ding-corp"


def test_dingtalk_read_sync_cannot_be_enabled_without_complete_credentials(monkeypatch):
    monkeypatch.setenv("COMP_SECRET_KEY", "a" * 48)
    monkeypatch.setenv("COMP_ENCRYPTION_KEY", "b" * 48)
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.setenv("COMP_DINGTALK_READ_SYNC_ENABLED", "true")
    monkeypatch.delenv("COMP_DINGTALK_CLIENT_ID", raising=False)
    monkeypatch.delenv("COMP_DINGTALK_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("COMP_DINGTALK_AGENT_ID", raising=False)

    with pytest.raises(ValidationError):
        Settings(_env_file=None)
