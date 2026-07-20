from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.audit import service as audit
from app.auth.deps import principal_scope, require_permission
from app.auth.permissions import Perm
from app.auth.service import Principal
from app.db.session import get_session
from app.importing.excel import read_salary_workbook
from app.importing.service import ImportError_, confirm_import, stage_import
from app.models.salary import ImportBatch, ImportStagingRow, RowStatus
from app.repositories.salary import SalaryRecordRepository

router = APIRouter(prefix="/api/imports", tags=["imports"])


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


@router.post("", response_model=BatchSummary, status_code=status.HTTP_201_CREATED)
async def upload_import(
    period: str = Query(..., pattern=r"^\d{4}-\d{2}$"),
    file: UploadFile = File(...),
    principal: Principal = Depends(require_permission(Perm.IMPORT_RUN)),
    session: Session = Depends(get_session),
) -> ImportBatch:
    if not (file.filename or "").lower().endswith((".xlsx", ".xlsm")):
        raise HTTPException(status_code=400, detail="仅支持 .xlsx/.xlsm 文件")
    import io

    _MAX_UPLOAD = 20 * 1024 * 1024  # 20MB 上限，防超大文件 OOM
    content = await file.read(_MAX_UPLOAD + 1)
    if len(content) > _MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="文件超过 20MB 上限")
    try:
        result = read_salary_workbook(io.BytesIO(content), period=period)
    except Exception as exc:  # noqa: BLE001  解析失败以 400 反馈，不泄露内部堆栈
        raise HTTPException(status_code=400, detail=f"解析失败：{type(exc).__name__}") from None
    batch = stage_import(
        session, filename=file.filename or "upload.xlsx", period=period, rows=result.rows
    )
    audit.record(
        session,
        action="import.upload",
        actor=(principal.user_id, principal.username),
        target_type="import_batch",
        target_id=batch.id,
        detail={"filename": batch.filename, "rows": batch.total_rows, "errors": batch.error_rows},
    )
    session.commit()
    return batch


@router.get("/{batch_id}", response_model=list[StagingRowOut])
def get_batch_rows(
    batch_id: int,
    only_errors: bool = False,
    _p: Principal = Depends(require_permission(Perm.IMPORT_RUN)),
    session: Session = Depends(get_session),
) -> list[ImportStagingRow]:
    stmt = select(ImportStagingRow).where(ImportStagingRow.batch_id == batch_id)
    if only_errors:
        stmt = stmt.where(ImportStagingRow.status == RowStatus.ERROR)
    return list(session.scalars(stmt.order_by(ImportStagingRow.row_index)).all())


class ConfirmResult(BaseModel):
    written: int


@router.post("/{batch_id}/confirm", response_model=ConfirmResult)
def confirm_batch(
    batch_id: int,
    principal: Principal = Depends(require_permission(Perm.IMPORT_RUN)),
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
    repo = SalaryRecordRepository(session, org_scope=principal_scope(principal))
    result = repo.search(
        name=name, emp_no=emp_no, period=period, store=store, page=page, page_size=page_size
    )
    return SalaryRecordPage(
        items=[SalaryRecordOut.model_validate(r) for r in result.items],
        total=result.total,
        page=result.page,
        page_size=result.page_size,
    )
