import inspect
import io
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile

from app.core.config import DingTalkMode
from app.importing.excel import ReadResult, WorkbookLimitError
from app.importing.publish import ImportPublishSummary
from app.importing.store_aliases import STORE_ALIASES
from app.models.salary import ImportStatus
from app.routers import imports as imports_router


def test_upload_endpoint_is_synchronous_so_database_locks_do_not_block_the_event_loop():
    assert not inspect.iscoroutinefunction(imports_router.upload_import)


def test_upload_passes_canonical_store_aliases_to_workbook_parser(monkeypatch):
    parsed = ReadResult(rows=[SimpleNamespace()], warnings=[])
    batch = SimpleNamespace(id=17, filename="salary.xlsx", total_rows=0, error_rows=0)
    parse_calls: list[dict] = []
    safety_events: list[str] = []
    session = Mock()
    session.scalars.return_value.first.return_value = None

    def fake_read_salary_workbook(*_args, **kwargs):
        safety_events.append("parse")
        parse_calls.append(kwargs)
        return parsed

    monkeypatch.setattr(imports_router, "read_salary_workbook", fake_read_salary_workbook)
    monkeypatch.setattr(imports_router, "stage_import", lambda *_args, **_kwargs: batch)
    monkeypatch.setattr(
        imports_router,
        "lock_legacy_salary_dataset",
        lambda *_args, **_kwargs: safety_events.append("lock"),
    )
    monkeypatch.setattr(imports_router.audit, "record", lambda *_args, **_kwargs: None)
    upload = UploadFile(io.BytesIO(b"workbook"), filename="salary.xlsx")
    principal = SimpleNamespace(user_id=1, username="hr")

    result = imports_router.upload_import(
        period="2026-05", file=upload, principal=principal, session=session
    )

    assert result is batch
    assert parse_calls == [{"period": "2026-05", "aliases": STORE_ALIASES}]
    assert safety_events[:2] == ["lock", "parse"]
    session.commit.assert_called_once_with()


def test_upload_rejects_a_workbook_without_employee_rows(monkeypatch):
    monkeypatch.setattr(
        imports_router,
        "read_salary_workbook",
        lambda *_args, **_kwargs: ReadResult(rows=[], warnings=["没有数据"]),
    )
    stage = Mock()
    monkeypatch.setattr(imports_router, "stage_import", stage)

    session = Mock()
    session.scalars.return_value.first.return_value = None
    with pytest.raises(HTTPException) as exc_info:
        imports_router.upload_import(
            period="2026-05",
            file=UploadFile(io.BytesIO(b"workbook"), filename="empty.xlsx"),
            principal=SimpleNamespace(user_id=1, username="hr"),
            session=session,
        )

    assert exc_info.value.status_code == 400
    assert "没有可导入" in str(exc_info.value.detail)
    stage.assert_not_called()


def test_upload_rejects_parser_warnings_instead_of_silently_skipping_sheets(monkeypatch):
    monkeypatch.setattr(
        imports_router,
        "read_salary_workbook",
        lambda *_args, **_kwargs: ReadResult(
            rows=[SimpleNamespace()],
            warnings=["工作表『漏导门店』未找到姓名列，已跳过"],
        ),
    )
    stage = Mock()
    monkeypatch.setattr(imports_router, "stage_import", stage)

    session = Mock()
    session.scalars.return_value.first.return_value = None
    with pytest.raises(HTTPException) as exc_info:
        imports_router.upload_import(
            period="2026-05",
            file=UploadFile(io.BytesIO(b"workbook"), filename="partial.xlsx"),
            principal=SimpleNamespace(user_id=1, username="hr"),
            session=session,
        )

    assert exc_info.value.status_code == 400
    assert "漏导门店" in str(exc_info.value.detail)
    assert "未完整" in str(exc_info.value.detail)
    stage.assert_not_called()


def test_upload_rejects_non_template_fields_before_sensitive_values_are_staged(monkeypatch):
    sensitive_value = "440101199001011234"
    monkeypatch.setattr(
        imports_router,
        "read_salary_workbook",
        lambda *_args, **_kwargs: ReadResult(
            rows=[
                SimpleNamespace(
                    fields={"复核部门": "厅面", "身份证号": sensitive_value},
                    money={},
                )
            ],
            warnings=[],
        ),
    )
    stage = Mock()
    monkeypatch.setattr(imports_router, "stage_import", stage)
    session = Mock()
    session.scalars.return_value.first.return_value = None

    with pytest.raises(HTTPException) as exc_info:
        imports_router.upload_import(
            period="2026-05",
            file=UploadFile(io.BytesIO(b"workbook"), filename="sensitive.xlsx"),
            principal=SimpleNamespace(user_id=1, username="hr"),
            session=session,
        )

    assert exc_info.value.status_code == 400
    assert "身份证号" in str(exc_info.value.detail)
    assert sensitive_value not in str(exc_info.value.detail)
    stage.assert_not_called()


def test_upload_rejects_an_impossible_month_before_parsing(monkeypatch):
    parser = Mock()
    monkeypatch.setattr(imports_router, "read_salary_workbook", parser)

    with pytest.raises(HTTPException) as exc_info:
        imports_router.upload_import(
            period="2026-99",
            file=UploadFile(io.BytesIO(b"workbook"), filename="salary.xlsx"),
            principal=SimpleNamespace(user_id=1, username="hr"),
            session=Mock(),
        )

    assert exc_info.value.status_code == 400
    assert "薪资月份" in str(exc_info.value.detail)
    parser.assert_not_called()


def test_upload_of_the_same_file_reuses_the_existing_batch_without_reparsing(monkeypatch):
    existing = SimpleNamespace(
        id=21,
        filename="salary.xlsx",
        period="2026-05",
        status=ImportStatus.CONFIRMED,
        total_rows=8,
        error_rows=0,
    )
    session = Mock()
    session.scalars.return_value.first.return_value = existing
    parser = Mock()
    monkeypatch.setattr(imports_router, "read_salary_workbook", parser)
    monkeypatch.setattr(imports_router.audit, "record", Mock())

    response = imports_router.upload_import(
        period="2026-05",
        file=UploadFile(io.BytesIO(b"same-workbook"), filename="salary.xlsx"),
        principal=SimpleNamespace(user_id=1, username="hr"),
        session=session,
    )

    assert response is existing
    parser.assert_not_called()
    session.commit.assert_called_once_with()


def test_upload_of_same_parsed_file_revalidates_existing_batch(monkeypatch):
    existing = SimpleNamespace(
        id=22,
        filename="salary.xlsx",
        period="2026-05",
        status=ImportStatus.PARSED,
        total_rows=1,
        error_rows=1,
        published_batch_id=None,
    )
    parsed = ReadResult(rows=[SimpleNamespace(emp_no="E1")], warnings=[])
    session = Mock()
    session.scalars.return_value.first.return_value = existing
    parser = Mock(return_value=parsed)

    def mark_revalidated(*_args, **_kwargs):
        existing.error_rows = 0
        return True

    restage = Mock(side_effect=mark_revalidated)
    audit_record = Mock()

    monkeypatch.setattr(imports_router, "read_salary_workbook", parser)
    monkeypatch.setattr(imports_router, "restage_import", restage, raising=False)
    monkeypatch.setattr(imports_router, "lock_legacy_salary_dataset", Mock())
    monkeypatch.setattr(imports_router.audit, "record", audit_record)

    response = imports_router.upload_import(
        period="2026-05",
        file=UploadFile(io.BytesIO(b"same-parsed-workbook"), filename="salary.xlsx"),
        principal=SimpleNamespace(user_id=1, username="hr"),
        session=session,
    )

    assert response is existing
    parser.assert_called_once()
    restage.assert_called_once_with(
        session,
        existing,
        filename="salary.xlsx",
        period="2026-05",
        rows=parsed.rows,
        created_by=1,
    )
    audit_record.assert_called_once_with(
        session,
        action="import.upload.restage",
        actor=(1, "hr"),
        target_type="import_batch",
        target_id=22,
        detail={"previous_errors": 1, "rows": 1, "errors": 0},
    )
    session.commit.assert_called_once_with()


def test_upload_reports_workbook_safety_limits_as_payload_too_large(monkeypatch):
    def reject_workbook(*_args, **_kwargs):
        raise WorkbookLimitError("XLSX archive expanded size exceeds the limit")

    session = Mock()
    session.scalars.return_value.first.return_value = None
    monkeypatch.setattr(imports_router, "read_salary_workbook", reject_workbook)

    with pytest.raises(HTTPException) as exc_info:
        imports_router.upload_import(
            period="2026-05",
            file=UploadFile(io.BytesIO(b"workbook"), filename="oversized.xlsx"),
            principal=SimpleNamespace(user_id=1, username="hr"),
            session=session,
        )

    assert exc_info.value.status_code == 413
    assert "expanded size" in str(exc_info.value.detail)


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
    delivery_summary = SimpleNamespace(
        routed=3,
        configuration_failures=1,
        existing=0,
        pending_delivery_ids=(),
        scopes=3,
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
        selection=SimpleNamespace(store_ids=[11, 12]),
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
    assert response.selected_stores == 2
    assert response.selected_scopes == 3
    assert response.sandbox is True
    publish_call.assert_called_once_with(session, imported)
    stage_call.assert_called_once_with(
        session,
        batch_id=31,
        settings=settings,
        org_unit_ids=frozenset({11, 12}),
    )
    assert [call["action"] for call in audit_calls] == [
        "import.publish",
        "dingtalk.review.stage",
    ]
    assert "员工" not in str(audit_calls)
    assert "工资" not in str(audit_calls)
    assert audit_calls[0]["detail"]["selected_store_ids"] == [11, 12]
    assert audit_calls[1]["detail"]["selected_stores"] == 2
    assert audit_calls[1]["detail"]["selected_scopes"] == 3
    session.commit.assert_called_once_with()


@pytest.mark.parametrize(
    "store_ids",
    [
        pytest.param([], id="empty"),
        pytest.param([11, 11], id="duplicate"),
        pytest.param([0], id="zero"),
        pytest.param([-1], id="negative"),
        pytest.param(["11"], id="numeric-string"),
        pytest.param([True], id="boolean"),
        pytest.param(list(range(1, 502)), id="too-many"),
    ],
)
def test_publish_selection_schema_rejects_invalid_store_lists(store_ids):
    with pytest.raises(ValueError):
        imports_router.PublishSelection(store_ids=store_ids)
