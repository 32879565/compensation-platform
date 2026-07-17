import time

import jwt
import pytest

from app.core.security import (
    create_access_token,
    decode_access_token,
    generate_refresh_token,
    hash_password,
    hash_refresh_token,
    verify_password,
)


def test_password_hash_roundtrip():
    h = hash_password("s3cret-Passw0rd")
    assert h != "s3cret-Passw0rd"  # 不是明文
    assert verify_password(h, "s3cret-Passw0rd") is True
    assert verify_password(h, "wrong") is False


def test_verify_corrupt_hash_is_false():
    # 损坏的哈希不得抛异常，一律验证失败（fail-closed）
    assert verify_password("not-a-valid-argon2-hash", "x") is False


def test_access_token_roundtrip():
    token = create_access_token(42)
    payload = decode_access_token(token)
    assert payload["sub"] == "42"
    assert payload["type"] == "access"


def test_access_token_rejects_tampered(monkeypatch):
    token = create_access_token(1)
    with pytest.raises(jwt.InvalidSignatureError):
        jwt.decode(token, "wrong-key", algorithms=["HS256"])


def test_access_token_expired(monkeypatch):
    from app.core import security

    # 令 TTL 为负数模拟过期
    settings = security.get_settings()
    monkeypatch.setattr(settings, "access_token_ttl_minutes", -1)
    token = create_access_token(1)
    time.sleep(0.01)
    with pytest.raises(jwt.ExpiredSignatureError):
        decode_access_token(token)


def test_refresh_token_hash_is_deterministic_and_opaque():
    raw, digest = generate_refresh_token()
    assert digest == hash_refresh_token(raw)
    assert raw != digest
    assert len(digest) == 64  # sha256 hex
