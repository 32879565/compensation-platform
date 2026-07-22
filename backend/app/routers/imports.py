from __future__ import annotations

from collections.abc import Sequence
from datetime import date
from hashlib import sha256

from fastapi import (
    APIRouter,
    BackgroundTasks,
    Depends,
    File,
    HTTPException,
    Query,
    UploadFile,
    status,
)
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal, resolve_permission_org_scope
from app.core.config import DingTalkMode, Settings, get_settings
from app.db.session import get_session
from app.dingtalk import service as dingtalk
from app.importing.excel import WorkbookLimitError, read_salary_workbook
from app.importing.header_rules import CURRENT_IMPORT_FIELDS
from app.importing.publish import (
    ImportPublishError,
    list_import_publish_targets,
    publish_import_for_review,
)
from app.importing.service import ImportError_, confirm_import, restage_import, stage_import
from app.importing.source_lock import lock_legacy_salary_dataset
from app.importing.store_aliases import STORE_ALIASES
from app.models.employee import Department
from app.models.salary import (
    ImportBatch,
    ImportStagingRow,
    ImportStatus,
    RowStatus,
    SalarySource,
)
from app.repositories.salary import SalaryRecordRepository

router = APIRouter(prefix="/api/imports", tags=["imports"])


def _require_global_importer(
    principal: Principal = Depends(require_permission(Perm.IMPORT_RUN)),
    session: Session = Depends(get_session),
) -> Principal:
    """Import staging is group-wide, so a local grant must not become global."""
    if resolve_permission_org_scope(session, principal, Perm.IMPORT_RUN) is not None:
        raise HTTPException(
            status_code=403, detail="salary import requires global organization scope"
        )
    return principal


def _require_global_publisher(
    principal: Principal = Depends(_require_global_importer),
    payroll_principal: Principal = Depends(require_permission(Perm.PAYROLL_RUN)),
    session: Session = Depends(get_session),
) -> Principal:
    """Publishing is both a group-wide import and payroll lifecycle action."""
    if principal.user_id != payroll_principal.user_id:
        raise HTTPException(status_code=403, detail="invalid payroll publish principal")
    if resolve_permission_org_scope(session, principal, Perm.PAYROLL_RUN) is not None:
        raise HTTPException(
            status_code=403, detail="salary publish requires global organization scope"
        )
    return principal


class BatchSummary(BaseModel):
    id: int
    filename: str
    period: str | None
    status: str
    total_rows: int
    error_rows: int

    model_config = {"from_attributes": True}


class StagingRowOut(BaseModel):
    row_index: int
    period: str
    emp_no: str | None
    name: str
    store_name: str
    parsed_fields: dict
    errors: list
    status: str

    model_config = {"from_attributes": True}


def _same_file_batch(
    session: Session, *, period: str, file_sha256: str, for_update: bool = False
) -> ImportBatch | None:
    statement = select(ImportBatch).where(
        ImportBatch.period == period,
        ImportBatch.source == SalarySource.IMPORT,
        ImportBatch.file_sha256 == file_sha256,
    )
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    return session.scalars(statement).first()


def _can_restage(batch: ImportBatch) -> bool:
    return (
        batch.status is ImportStatus.PARSED and getattr(batch, "published_batch_id", None) is None
    )


def _return_reused_upload(
    session: Session, *, batch: ImportBatch, principal: Principal
) -> ImportBatch:
    audit.record(
        session,
        action="import.upload.reuse",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=batch.id,
        detail={"status": batch.status.value, "rows": batch.total_rows, "errors": batch.error_rows},
    )
    session.commit()
    return batch


def _unsupported_current_import_fields(rows: Sequence[object]) -> list[str]:
    fields: set[str] = set()
    for row in rows:
        row_fields = getattr(row, "fields", {})
        row_money = getattr(row, "money", {})
        if isinstance(row_fields, dict):
            fields.update(str(name) for name in row_fields)
        if isinstance(row_money, dict):
            fields.update(str(name) for name in row_money)
    return sorted(fields - CURRENT_IMPORT_FIELDS)


@router.post("", response_model=BatchSummary, status_code=status.HTTP_201_CREATED)
def upload_import(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    file: UploadFile = File(...),
    principal: Principal = Depends(_require_global_importer),
    session: Session = Depends(get_session),
) -> ImportBatch:
    try:
        date.fromisoformat(f"{period}-01")
    except ValueError:
        raise HTTPException(status_code=400, detail="薪资月份无效") from None
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx/.xlsm 文件")
    import io

    # Serialize before reading or expanding the upload, bounding concurrent
    # memory as well as parser work across all application workers.
    lock_legacy_salary_dataset(session)
    _MAX_UPLOAD = 20 * 1024 * 1024  # 20MB 上限，防超大文件 OOM
    content = file.file.read(_MAX_UPLOAD + 1)
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件超过 20MB 上限")
    file_sha256 = sha256(content).hexdigest()
    existing = _same_file_batch(
        session,
        period=period,
        file_sha256=file_sha256,
        for_update=True,
    )
    if existing is not None and not _can_restage(existing):
        return _return_reused_upload(session, batch=existing, principal=principal)
    try:
        result = read_salary_workbook(io.BytesIO(content), period=period, aliases=STORE_ALIASES)
    except WorkbookLimitError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from None
    except Exception as exc:  # noqa: BLE001  解析失败以 400 反馈，不泄露内部堆栈
        raise HTTPException(status_code=400, detail=f"解析失败：{type(exc).__name__}") from None
    if result.warnings:
        visible = result.warnings[:10]
        suffix = (
            f"；另有 {len(result.warnings) - len(visible)} 项警告"
            if len(result.warnings) > 10
            else ""
        )
        empty_prefix = "工作簿没有可导入的员工工资数据；" if not result.rows else ""
        raise HTTPException(
            status_code=400,
            detail=(
                f"{empty_prefix}工作簿解析未完整：{'；'.join(visible)}{suffix}。"
                "请修正或删除相应工作表后重新上传"
            ),
        )
    if not result.rows:
        raise HTTPException(status_code=400, detail="工作簿没有可导入的员工工资数据")
    unsupported_fields = _unsupported_current_import_fields(result.rows)
    if unsupported_fields:
        visible = unsupported_fields[:10]
        suffix = (
            f"；另有 {len(unsupported_fields) - len(visible)} 个字段"
            if len(unsupported_fields) > len(visible)
            else ""
        )
        raise HTTPException(
            status_code=400,
            detail=(
                f"工作簿包含模板未支持的字段：{'、'.join(visible)}{suffix}。"
                "为避免身份证、银行卡等敏感信息进入薪资记录，"
                "请删除这些列或使用系统模板"
            ),
        )
    if existing is not None:
        previous_errors = existing.error_rows
        if _can_restage(existing) and restage_import(
            session,
            existing,
            filename=file.filename or "upload.xlsx",
            period=period,
            rows=result.rows,
            created_by=principal.user_id,
        ):
            audit.record(
                session,
                action="import.upload.restage",
                actor=(principal.user_id, principal.username),
                target_type="import_batch",
                target_id=existing.id,
                detail={
                    "previous_errors": previous_errors,
                    "rows": existing.total_rows,
                    "errors": existing.error_rows,
                },
            )
            session.commit()
            return existing
        return _return_reused_upload(session, batch=existing, principal=principal)
    batch = stage_import(
        session,
        filename=file.filename or "upload.xlsx",
        period=period,
        rows=result.rows,
        file_sha256=file_sha256,
        created_by=principal.user_id,
    )
    audit.record(
        session,
        action="import.upload",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=batch.id,
        detail={
            "file_extension": (file.filename or "").rsplit(".", maxsplit=1)[-1].lower(),
            "rows": batch.total_rows,
            "errors": batch.error_rows,
        },
    )
    session.commit()
    return batch


@router.get("/{batch_id}", response_model=list[StagingRowOut])
def get_batch_rows(
    batch_id: int,
    only_errors: bool = False,
    principal: Principal = Depends(_require_global_importer),
    session: Session = Depends(get_session),
) -> list[ImportStagingRow]:
    if session.get(ImportBatch, batch_id) is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    stmt = select(ImportStagingRow).where(ImportStagingRow.batch_id == batch_id)
    if only_errors:
        stmt = stmt.where(ImportStagingRow.status == RowStatus.ERROR)
    response = list(session.scalars(stmt.order_by(ImportStagingRow.row_index)).all())
    audit.record(
        session,
        action="import.staging_rows.view",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=batch_id,
        detail={"only_errors": only_errors, "returned": len(response)},
    )
    session.commit()
    return response


class ConfirmResult(BaseModel):
    written: int


@router.post("/{batch_id}/confirm", response_model=ConfirmResult)
def confirm_batch(
    batch_id: int,
    principal: Principal = Depends(_require_global_importer),
    session: Session = Depends(get_session),
) -> ConfirmResult:
    batch = session.get(ImportBatch, batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="批次不存在")
    try:
        written = confirm_import(session, batch)
    except ImportError_ as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    audit.record(
        session,
        action="import.confirm",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=batch.id,
        detail={"written": written},
    )
    session.commit()
    return ConfirmResult(written=written)


class PublishTargetOut(BaseModel):
    store_id: int
    store_name: str
    employee_count: int
    departments: list[Department]

    model_config = {"from_attributes": True}


@router.get("/{batch_id}/publish-targets", response_model=list[PublishTargetOut])
def get_publish_targets(
    batch_id: int,
    principal: Principal = Depends(_require_global_publisher),
    session: Session = Depends(get_session),
) -> list[PublishTargetOut]:
    imported = session.get(ImportBatch, batch_id)
    if imported is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    try:
        targets = list_import_publish_targets(session, imported)
    except ImportPublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    response = [PublishTargetOut.model_validate(target) for target in targets]
    audit.record(
        session,
        action="import.publish_targets.view",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=imported.id,
        detail={"stores": len(response)},
    )
    session.commit()
    return response


class PublishSelection(BaseModel):
    store_ids: list[int] = Field(min_length=1, max_length=500)

    model_config = {"extra": "forbid"}

    @field_validator("store_ids", mode="before")
    @classmethod
    def validate_store_ids(cls, value: object) -> object:
        if not isinstance(value, list):
            raise ValueError("store_ids must be a list")
        if not 1 <= len(value) <= 500:
            raise ValueError("store_ids must contain between 1 and 500 entries")
        if any(type(store_id) is not int or store_id <= 0 for store_id in value):
            raise ValueError("store_ids must contain only positive integers")
        if len(set(value)) != len(value):
            raise ValueError("store_ids must not contain duplicates")
        return value


class PublishResult(BaseModel):
    import_batch_id: int
    payroll_batch_id: int
    batch_version: int
    employees: int
    scopes: int
    routed: int
    configuration_failures: int
    existing: int
    selected_stores: int
    selected_scopes: int
    already_published: bool
    sandbox: bool


@router.post("/{batch_id}/publish", response_model=PublishResult)
def publish_batch_for_review(
    batch_id: int,
    selection: PublishSelection,
    background_tasks: BackgroundTasks,
    principal: Principal = Depends(_require_global_publisher),
    settings: Settings = Depends(get_settings),
    session: Session = Depends(get_session),
) -> PublishResult:
    """Publish exact imported totals and route each store/department review scope."""
    imported = session.get(ImportBatch, batch_id)
    if imported is None:
        raise HTTPException(status_code=404, detail="导入批次不存在")
    selected_store_ids = frozenset(selection.store_ids)
    try:
        published = publish_import_for_review(
            session,
            imported,
            store_ids=selected_store_ids,
        )
        delivery = dingtalk.stage_review_deliveries(
            session,
            batch_id=published.payroll_batch_id,
            settings=settings,
            org_unit_ids=selected_store_ids,
        )
    except ImportPublishError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None
    except dingtalk.DingTalkError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from None

    sandbox = settings.dingtalk_mode is DingTalkMode.SANDBOX
    audit.record(
        session,
        action="import.publish",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=imported.id,
        detail={
            "payroll_batch_id": published.payroll_batch_id,
            "batch_version": published.batch_version,
            "selected_store_ids": sorted(selected_store_ids),
            "selected_stores": len(selected_store_ids),
            "selected_scopes": published.scopes,
            "already_published": published.already_published,
        },
    )
    audit.record(
        session,
        action="dingtalk.review.stage",
        actor=(principal.user_id, principal.username),
        target_type="payroll_batch",
        target_id=published.payroll_batch_id,
        detail={
            "sandbox": sandbox,
            "selected_stores": len(selected_store_ids),
            "selected_scopes": delivery.scopes,
            "routed": delivery.routed,
            "configuration_failures": delivery.configuration_failures,
            "existing": delivery.existing,
        },
    )
    session.commit()
    if settings.dingtalk_mode is DingTalkMode.LIVE and delivery.pending_delivery_ids:
        background_tasks.add_task(
            dingtalk.dispatch_live_deliveries,
            delivery.pending_delivery_ids,
        )
    return PublishResult(
        import_batch_id=published.import_batch_id,
        payroll_batch_id=published.payroll_batch_id,
        batch_version=published.batch_version,
        employees=published.employees,
        scopes=published.scopes,
        routed=delivery.routed,
        configuration_failures=delivery.configuration_failures,
        existing=delivery.existing,
        selected_stores=len(selected_store_ids),
        selected_scopes=delivery.scopes,
        already_published=published.already_published,
        sandbox=sandbox,
    )


# ------------------- 薪资记录查询（历史工资可查，M2 核心）-------------------
salary_router = APIRouter(prefix="/api/salary-records", tags=["salary"])


class SalaryRecordOut(BaseModel):
    id: int
    period: str
    emp_no: str | None
    name: str
    store_name: str
    source: str
    fields: dict

    model_config = {"from_attributes": True}


class SalaryRecordPage(BaseModel):
    items: list[SalaryRecordOut]
    total: int
    page: int
    page_size: int


@salary_router.get("", response_model=SalaryRecordPage)
def search_salary(
    name: str | None = None,
    emp_no: str | None = None,
    period: str | None = None,
    store: str | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=500),
    principal: Principal = Depends(require_permission(Perm.SALARY_READ)),
    session: Session = Depends(get_session),
) -> SalaryRecordPage:
    repo = SalaryRecordRepository(
        session,
        org_scope=resolve_permission_org_scope(session, principal, Perm.SALARY_READ),
    )
    result = repo.search(
        name=name, emp_no=emp_no, period=period, store=store, page=page, page_size=page_size
    )
    response = SalaryRecordPage(
        items=[SalaryRecordOut.model_validate(r) for r in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )
    audit.record(
        session,
        action="salary.records.search",
        actor=(principal.user_id, principal.username),
        target_type="salary_record",
        detail={
            "has_name_filter": name is not None,
            "has_emp_no_filter": emp_no is not None,
            "has_period_filter": period is not None,
            "has_store_filter": store is not None,
            "page": page,
            "page_size": page_size,
            "returned": len(response.items),
        },
    )
    session.commit()
    return response
