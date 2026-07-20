"""PII 列级加密（不变量7）与脱敏。

- 加密：Fernet（AES128-CBC + HMAC），密钥由 COMP_ENCRYPTION_KEY 经 sha256 派生。
- EncryptedString：SQLAlchemy TypeDecorator，写入自动加密、读取自动解密，
  应用层始终拿到明文，密文只存在于库中。
- 脱敏：面向 API/日志展示，绝不返回全量 PII。
"""

from __future__ import annotations

import base64
import hashlib
from functools import lru_cache

from cryptography.fernet import Fernet
from sqlalchemy import String, TypeDecorator

from app.core.config import get_settings


@lru_cache
def _fernet() -> Fernet:
    digest = hashlib.sha256(get_settings().encryption_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_pii(plain: str | None) -> str | None:
    if plain is None:
        return None
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_pii(token: str | None) -> str | None:
    if token is None:
        return None
    return _fernet().decrypt(token.encode("ascii")).decode("utf-8")


class EncryptedString(TypeDecorator):
    """透明加密的字符串列：DB 存 Fernet 密文，Python 侧读写明文。"""

    impl = String
    cache_ok = True

    def process_bind_param(self, value: str | None, dialect: object) -> str | None:
        return encrypt_pii(value)

    def process_result_value(self, value: str | None, dialect: object) -> str | None:
        return decrypt_pii(value)


def mask_id_card(plain: str | None) -> str | None:
    """身份证脱敏：保留前 3 后 2，中间打码。"""
    if not plain:
        return plain
    if len(plain) <= 5:
        return "*" * len(plain)
    return f"{plain[:3]}{'*' * (len(plain) - 5)}{plain[-2:]}"


def mask_bank_account(plain: str | None) -> str | None:
    """银行卡脱敏：仅保留后 4 位。"""
    if not plain:
        return plain
    if len(plain) <= 4:
        return "*" * len(plain)
    return f"{'*' * (len(plain) - 4)}{plain[-4:]}"
