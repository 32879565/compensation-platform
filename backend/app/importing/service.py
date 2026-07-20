"""导入服务：解析→暂存(带校验)→人工核对→确认写库。

确认前若存在 ERROR 行则阻断（不变量2：不让脏数据静默入库）。
"""

from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.importing.header_rules import MONEY_FIELDS
from app.importing.parser import SalaryRow, dedupe_rows
from app.models.org import OrgType, OrgUnit
from app.models.salary import (
    ImportBatch,
    ImportStagingRow,
    ImportStatus,
    RowStatus,
    SalaryRecord,
    SalarySource,
)


class ImportError_(Exception):
    """导入流程错误（如带错误行时强行确认）。"""


def _validate_row(row: SalaryRow) -> list[str]:
    errors: list[str] = []
    if not row.name:
        errors.append("缺少姓名")
    if not row.store_name:
        errors.append("缺少门店")
    # 金额字段有原值文本但无法解析 → 报错（不静默归零）
    for f in MONEY_FIELDS:
        if row.fields.get(f) and row.money.get(f) is None:
            errors.append(f"金额字段『{f}』无法解析：{row.fields.get(f)}")
    return errors


def _money_to_str(row: SalaryRow) -> dict[str, str]:
    """把行的字段序列化为可入 JSONB 的字典；金额用字符串保精度。"""
    out = dict(row.fields)
    for f, v in row.money.items():
        if isinstance(v, Decimal):
            out[f] = str(v)
    return out


def stage_import(
    session: Session,
    *,
    filename: str,
    period: str,
    rows: list[SalaryRow],
    source: SalarySource = SalarySource.IMPORT,
) -> ImportBatch:
    """去重后逐行校验并写入暂存表；返回批次（状态 PARSED）。"""
    deduped = dedupe_rows(rows)
    batch = ImportBatch(
        filename=filename,
        period=period,
        source=source,
        status=ImportStatus.PARSED,
        total_rows=len(deduped),
    )
    session.add(batch)
    session.flush()

    error_count = 0
    for idx, row in enumerate(deduped):
        errors = _validate_row(row)
        status = RowStatus.ERROR if errors else RowStatus.OK
        if errors:
            error_count += 1
        session.add(
            ImportStagingRow(
                batch_id=batch.id,
                row_index=idx,
                period=row.period,
                emp_no=row.emp_no,
                name=row.name or "(空)",
                store_name=row.store_name or "(空)",
                parsed_fields=_money_to_str(row),
                errors=errors,
                status=status,
            )
        )
    batch.error_rows = error_count
    session.flush()
    return batch


def confirm_import(session: Session, batch: ImportBatch) -> int:
    """把批次的 OK 行写入 salary_record；有 ERROR 行则拒绝。返回写入条数。"""
    # 行锁防并发/双击重复确认（重复确认将只在第一个成功，其余见 CONFIRMED 报错）
    session.refresh(batch, with_for_update=True)
    if batch.status == ImportStatus.CONFIRMED:
        raise ImportError_("批次已确认")
    if batch.error_rows > 0:
        raise ImportError_(f"存在 {batch.error_rows} 行错误，请先修正后再确认")

    # 门店名 → org_unit_id 解析（仅门店类型，避免同名区域/门店冲突）
    org_by_name: dict[str, int] = {}
    for oid, name in session.execute(
        select(OrgUnit.id, OrgUnit.name).where(
            OrgUnit.is_deleted.is_(False), OrgUnit.type == OrgType.STORE
        )
    ).all():
        org_by_name.setdefault(name, oid)  # 同名取第一个，稳定

    staged = session.scalars(
        select(ImportStagingRow).where(
            ImportStagingRow.batch_id == batch.id,
            ImportStagingRow.status == RowStatus.OK,
        )
    ).all()

    written = 0
    for srow in staged:
        record = SalaryRecord(
            period=srow.period,
            emp_no=srow.emp_no,
            name=srow.name,
            store_name=srow.store_name,
            org_unit_id=org_by_name.get(srow.store_name),
            source=batch.source,
            fields=srow.parsed_fields,
            import_batch_id=batch.id,
        )
        session.add(record)
        session.flush()
        srow.salary_record_id = record.id
        written += 1

    batch.status = ImportStatus.CONFIRMED
    batch.confirmed_at = func.now()
    session.flush()
    return written
