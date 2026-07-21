import asyncio
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.core.config import DingTalkMode
from app.dingtalk.service import DeliveryStageSummary
from app.importing.excel import ReadResult
from app.importing.publish import ImportPublishSummary
from app.importing.store_aliases import STORE_ALIASES
from app.routers import imports as imports_router


def test_upload_passes_canonical_store_aliases_to_workbook_parser(monkeypatch):
    parsed = ReadResult(rows=[], warnings=[])
    batch = SimpleNamespace(id=17, filename="salary.xlsx", total_rows=0, error_rows=0)
    parse_calls: list[dict] = []
    session = Mock()

    def fake_read_salary_workbook(*_args, **kwargs):
        parse_calls.append(kwargs)
        return parsed

    monkeypatch.setattr(imports_router, "read_salary_workbook", fake_read_salary_workbook)
    monkeypatch.setattr(imports_router, "stage_import", lambda *_args, **_kwargs: batch)
    monkeypatch.setattr(imports_router.audit, "record", lambda *_args, **_kwargs: None)
    upload = UploadFile(io.BytesIO(b"workbook"), filename="salary.xlsx")
    principal = SimpleNamespace(user_id=1, username="hr")

    result = asyncio.run(
        imports_router.upload_import(
            period="2026-05", file=upload, principal=principal, session=session
        )
    )

    assert result is batch
    assert parse_calls == [{"period": "2026-05", "aliases": STORE_ALIASES}]
    session.commit.assert_called_once_with()


def test_staging_row_view_is_audited_without_imported_salary_fields(monkeypatch):
    session = Mock()
    session.get.return_value = SimpleNamespace(id=17)
    rows = [SimpleNamespace(emp_no="E001", name="张三", parsed_fields={"net": "5000.00"})]
    session.scalars.return_value.all.return_value = rows
    audit_calls: list[dict] = []

    def record(_session, **kwargs):
        audit_calls.append(kwargs)

    monkeypatch.setattr(imports_router.audit, "record", record)
    principal = SimpleNamespace(user_id=7, username="hr")

    result = imports_router.get_batch_rows(
        batch_id=17, only_errors=True, principal=principal, session=session
    )

    assert result == rows
    assert audit_calls == [
        {
            "action": "import.staging_rows.view",
            "actor": (7, "hr"),
            "target_type": "import_batch",
            "target_id": 17,
            "detail": {"only_errors": True, "returned": 1},
        }
    ]
    assert "张三" not in str(audit_calls[0])
    assert "E001" not in str(audit_calls[0])
    assert "5000.00" not in str(audit_calls[0])
    session.commit.assert_called_once_with()


def test_staging_row_view_rejects_an_unknown_batch_without_a_success_audit(monkeypatch):
    session = Mock()
    session.get.return_value = None
    record = Mock()
    monkeypatch.setattr(imports_router.audit, "record", record)

    with pytest.raises(HTTPException) as exc_info:
        imports_router.get_batch_rows(
            batch_id=999,
            only_errors=False,
            principal=SimpleNamespace(user_id=7, username="hr"),
            session=session,
        )

    assert exc_info.value.status_code == 404
    record.assert_not_called()
    session.commit.assert_not_called()


def test_publish_stages_scoped_review_delivery_and_audits_counts_only(monkeypatch):
    session = Mock()
    imported = SimpleNamespace(id=17)
    session.get.return_value = imported
    publish_summary = ImportPublishSummary(
        import_batch_id=17,
        payroll_batch_id=31,
        batch_version=2,
        employees=28,
        scopes=4,
        already_published=False,
    )
    delivery_summary = DeliveryStageSummary(
        routed=3,
        configuration_failures=1,
        existing=0,
        pending_delivery_ids=(),
    )
    publish_call = Mock(return_value=publish_summary)
    stage_call = Mock(return_value=delivery_summary)
    audit_calls: list[dict] = []
    monkeypatch.setattr(imports_router, "publish_import_for_review", publish_call)
    monkeypatch.setattr(imports_router.dingtalk, "stage_review_deliveries", stage_call)
    monkeypatch.setattr(
        imports_router.audit,
        "record",
        lambda _session, **kwargs: audit_calls.append(kwargs),
    )
    principal = SimpleNamespace(user_id=7, username="hr")
    settings = SimpleNamespace(dingtalk_mode=DingTalkMode.SANDBOX)

    response = imports_router.publish_batch_for_review(
        batch_id=17,
        background_tasks=BackgroundTasks(),
        principal=principal,
        settings=settings,
        session=session,
    )

    assert response.import_batch_id == 17
    assert response.payroll_batch_id == 31
    assert response.batch_version == 2
    assert response.employees == 28
    assert response.scopes == 4
    assert response.routed == 3
    assert response.configuration_failures == 1
    assert response.existing == 0
    assert response.sandbox is True
    publish_call.assert_called_once_with(session, imported)
    stage_call.assert_called_once_with(session, batch_id=31, settings=settings)
    assert [call["action"] for call in audit_calls] == [
        "import.publish",
        "dingtalk.review.stage",
    ]
    assert "员工" not in str(audit_calls)
    assert "工资" not in str(audit_calls)
    session.commit.assert_called_once_with()
