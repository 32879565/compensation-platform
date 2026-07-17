import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_missing_secret_key_fails_closed(monkeypatch):
    # 清空环境中的必填项，构造时不读 .env，应因缺 secret_key/database_url 而失败
    for key in ("COMP_SECRET_KEY", "COMP_DATABASE_URL"):
        monkeypatch.delenv(key, raising=False)
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_present_required_fields_ok(monkeypatch):
    monkeypatch.setenv("COMP_SECRET_KEY", "k")
    monkeypatch.setenv("COMP_DATABASE_URL", "postgresql+psycopg://a:b@localhost/c")
    monkeypatch.delenv("COMP_COOKIE_SECURE", raising=False)  # 隔离 conftest 的默认值
    s = Settings(_env_file=None)
    assert s.secret_key == "k"
    assert s.cookie_secure is True  # 默认安全
