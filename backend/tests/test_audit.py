import pytest
from sqlalchemy import select, text

from app.audit import service as audit
from app.audit.service import mask_detail
from app.models.audit import AuditLog

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_record_inserts_entry(db_session):
    entry = audit.record(
        db_session,
        action="employee.update",
        actor=(7, "hr"),
        target_type="employee",
        target_id=99,
        ip="10.0.0.1",
        detail={"changed": ["name"]},
    )
    fetched = db_session.get(AuditLog, entry.id)
    assert fetched.action == "employee.update"
    assert fetched.actor_user_id == 7
    assert fetched.actor_username == "hr"
    assert fetched.ip == "10.0.0.1"
    assert fetched.result == "SUCCESS"


def test_mask_detail_redacts_sensitive_keys():
    masked = mask_detail(
        {
            "username": "bob",
            "password": "secret123",
            "nested": {"access_token": "abc", "ok": 1},
            "id_card": "440101",
            "list": [{"bank_account": "6222"}],
        }
    )
    assert masked["username"] == "bob"
    assert masked["password"] == "***"
    assert masked["nested"]["access_token"] == "***"
    assert masked["nested"]["ok"] == 1
    assert masked["id_card"] == "***"
    assert masked["list"][0]["bank_account"] == "***"


def test_record_masks_detail(db_session):
    entry = audit.record(db_session, action="x", detail={"password": "p", "keep": "v"})
    fetched = db_session.get(AuditLog, entry.id)
    assert fetched.detail == {"password": "***", "keep": "v"}


def test_audit_log_is_append_only_update_blocked(db_session):
    entry = audit.record(db_session, action="x")
    with pytest.raises(Exception) as ei:
        db_session.execute(text("UPDATE audit_log SET result='X' WHERE id = :i"), {"i": entry.id})
    assert "append-only" in str(ei.value)


def test_audit_log_delete_blocked(db_session):
    entry = audit.record(db_session, action="x")
    with pytest.raises(Exception) as ei:
        db_session.execute(text("DELETE FROM audit_log WHERE id = :i"), {"i": entry.id})
    assert "append-only" in str(ei.value)


def test_record_defaults_result_success(db_session):
    entry = audit.record(db_session, action="x")
    assert (
        db_session.scalars(select(AuditLog).where(AuditLog.id == entry.id)).one().result
        == "SUCCESS"
    )
