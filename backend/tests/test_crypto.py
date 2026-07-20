import pytest
from sqlalchemy import text

from app.core.crypto import (
    decrypt_pii,
    encrypt_pii,
    mask_bank_account,
    mask_id_card,
)
from app.models.employee import Employee
from app.models.org import OrgType, OrgUnit

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_encrypt_decrypt_roundtrip():
    token = encrypt_pii("440101199001011234")
    assert token != "440101199001011234"  # 密文不等于明文
    assert decrypt_pii(token) == "440101199001011234"
    assert encrypt_pii(None) is None
    assert decrypt_pii(None) is None


def test_encrypted_column_stores_ciphertext(db_session):
    store = OrgUnit(code="S", name="店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    emp = Employee(emp_no="E1", name="张三", org_unit_id=store.id, id_card="440101199001011234")
    db_session.add(emp)
    db_session.flush()

    # ORM 读回是明文
    db_session.refresh(emp)
    assert emp.id_card == "440101199001011234"
    # 库中原始存储是密文（绕过 ORM 直接读列）
    raw = db_session.execute(
        text("SELECT id_card FROM employee WHERE id = :i"), {"i": emp.id}
    ).scalar_one()
    assert raw != "440101199001011234"
    assert decrypt_pii(raw) == "440101199001011234"


def test_mask_id_card():
    # 18 位身份证：前 3 + 后 2 保留，中间 13 位打码
    assert mask_id_card("440101199001011234") == f"440{'*' * 13}34"
    assert mask_id_card("123") == "***"
    assert mask_id_card(None) is None


def test_mask_bank_account():
    # 16 位卡号：仅保留后 4 位，前 12 位打码
    assert mask_bank_account("6222021234567890") == f"{'*' * 12}7890"
    assert mask_bank_account("12") == "**"
    assert mask_bank_account(None) is None
