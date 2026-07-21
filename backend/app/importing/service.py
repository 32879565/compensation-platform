"""导入服务：解析→暂存(带校验)→人工核对→确认写库。

确认前若存在 ERROR 行则阻断（不变量2：不让脏数据静默入库）。
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.importing.header_rules import CURRENT_IMPORT_FIELDS, MONEY_FIELDS
from app.importing.parser import SalaryRow, dedupe_rows
from app.importing.source_lock import lock_legacy_salary_dataset
from app.models.employee import Employee
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


def _employee_ids_by_emp_no(session: Session, emp_nos: set[str]) -> dict[str, int]:
    """Return non-deleted master-data employee ids keyed by their global employee number."""
    if not emp_nos:
        return {}
    return {
        emp_no: employee_id
        for emp_no, employee_id in session.execute(
            select(Employee.emp_no, Employee.id).where(
                Employee.emp_no.in_(emp_nos), Employee.is_deleted.is_(False)
            )
        ).tuples()
    }


def _employee_org_ids_by_emp_no(session: Session, emp_nos: set[str]) -> dict[str, int]:
    """Return each non-deleted employee's current master-data store."""
    if not emp_nos:
        return {}
    return {
        emp_no: org_unit_id
        for emp_no, org_unit_id in session.execute(
            select(Employee.emp_no, Employee.org_unit_id).where(
                Employee.emp_no.in_(emp_nos), Employee.is_deleted.is_(False)
            )
        ).tuples()
    }


def _employee_names_by_emp_no(session: Session, emp_nos: set[str]) -> dict[str, str]:
    """Return authoritative current employee names for identity validation."""
    if not emp_nos:
        return {}
    return {
        emp_no: name
        for emp_no, name in session.execute(
            select(Employee.emp_no, Employee.name).where(
                Employee.emp_no.in_(emp_nos), Employee.is_deleted.is_(False)
            )
        ).tuples()
    }


def _active_store_ids_by_name(session: Session) -> dict[str, int | None]:
    """Resolve active stores without guessing when names are ambiguous."""
    stores_by_name: dict[str, int | None] = {}
    for org_unit_id, name in session.execute(
        select(OrgUnit.id, OrgUnit.name).where(
            OrgUnit.is_deleted.is_(False), OrgUnit.type == OrgType.STORE
        )
    ).all():
        if name in stores_by_name:
            stores_by_name[name] = None
        else:
            stores_by_name[name] = org_unit_id
    return stores_by_name


def _conflicting_employee_identities(rows: list[SalaryRow]) -> set[tuple]:
    """Find one-period employee identities that disagree about employee display data.

    The canonical identity is ``(period, emp_no)``.  A repeated row from another workbook is
    normal and is handled by ``dedupe_rows``; a changed name or store is not safe to choose
    automatically, so the selected staging row is made an actionable error instead.
    """
    details_by_identity: dict[tuple, set[tuple[str, str]]] = {}
    for row in rows:
        if row.emp_no:
            details_by_identity.setdefault(row.identity_key(), set()).add(
                (row.name, row.store_name)
            )
    return {key for key, details in details_by_identity.items() if len(details) > 1}


def _duplicate_employee_identities(rows: list[SalaryRow]) -> set[tuple]:
    """Find current-payroll employee keys represented by more than one source row.

    Final payroll imports are authoritative money instructions.  Even when two
    rows look like an old-system shadow pair, choosing one silently could drop
    a legitimate zero-pay or correction row.  Preserve both for HR and block
    confirmation until the workbook contains exactly one row per employee.
    """
    counts: dict[tuple, int] = {}
    for row in rows:
        if row.emp_no:
            key = row.identity_key()
            counts[key] = counts.get(key, 0) + 1
    return {key for key, count in counts.items() if count > 1}


def _validate_row(
    row: SalaryRow,
    *,
    batch_period: str,
    source: SalarySource,
    employee_ids: dict[str, int],
    employee_org_ids: dict[str, int],
    employee_names: dict[str, str],
    store_ids: dict[str, int | None],
    conflicting_identities: set[tuple],
    duplicate_identities: set[tuple],
) -> list[str]:
    errors: list[str] = []
    if row.period != batch_period:
        errors.append(f"计薪周期『{row.period}』与导入批次『{batch_period}』不一致")
    if not row.name:
        errors.append("缺少姓名")
    if not row.store_name:
        errors.append("缺少门店")
    if source is SalarySource.IMPORT:
        if not row.emp_no:
            errors.append(
                "缺少工号：当前薪资导入必须匹配员工主数据；请补充工号后重新导入，或转历史迁移/人工认领"
            )
        elif row.emp_no not in employee_ids:
            errors.append(
                f"工号『{row.emp_no}』未匹配员工主数据；请先创建员工或完成人工认领后重新导入"
            )
        elif row.identity_key() in conflicting_identities:
            errors.append(
                f"工号『{row.emp_no}』在周期『{row.period}』对应多个姓名或门店；请核对后保留唯一记录"
            )
        elif row.name.strip() != employee_names.get(row.emp_no, "").strip():
            errors.append(f"工号『{row.emp_no}』与员工主数据姓名不一致")
        if row.emp_no and row.identity_key() in duplicate_identities:
            errors.append(
                f"工号『{row.emp_no}』在同一计薪月份出现重复工资行；" "请合并或删除重复行后重新导入"
            )
    # 金额字段有原值文本但无法解析 → 报错（不静默归零）
    for f in MONEY_FIELDS:
        if row.fields.get(f) and row.money.get(f) is None:
            errors.append(f"金额字段『{f}』无法解析：{row.fields.get(f)}")
    if (
        source is SalarySource.IMPORT
        and row.emp_no in employee_ids
        and row.identity_key() not in conflicting_identities
    ):
        workbook_store_id = store_ids.get(row.store_name)
        if workbook_store_id is None:
            errors.append(
                f"Workbook store「{row.store_name}」is missing or ambiguous in active master data."
            )
        elif workbook_store_id != employee_org_ids[row.emp_no]:
            errors.append(
                f"Workbook store「{row.store_name}」does not match the employee master store."
            )
    return errors


def _money_to_str(row: SalaryRow) -> dict[str, str]:
    """把行的字段序列化为可入 JSONB 的字典；金额用字符串保精度。"""
    out = dict(row.fields)
    for f, v in row.money.items():
        if isinstance(v, Decimal):
            out[f] = str(v)
    return out


def _write_staging_rows(
    session: Session,
    *,
    batch: ImportBatch,
    period: str,
    rows: list[SalaryRow],
    source: SalarySource,
) -> None:
    """Validate and persist one complete staging snapshot for ``batch``."""
    staged_rows = list(rows) if source is SalarySource.IMPORT else dedupe_rows(rows)
    emp_nos = {row.emp_no for row in staged_rows if row.emp_no}
    employee_ids = _employee_ids_by_emp_no(session, emp_nos)
    employee_org_ids = _employee_org_ids_by_emp_no(session, emp_nos)
    employee_names = _employee_names_by_emp_no(session, emp_nos)
    store_ids = _active_store_ids_by_name(session)
    conflicting_identities = _conflicting_employee_identities(rows)
    duplicate_identities = (
        _duplicate_employee_identities(rows) if source is SalarySource.IMPORT else set()
    )

    error_count = 0
    for idx, row in enumerate(staged_rows):
        errors = _validate_row(
            row,
            batch_period=period,
            source=source,
            employee_ids=employee_ids,
            employee_org_ids=employee_org_ids,
            employee_names=employee_names,
            store_ids=store_ids,
            conflicting_identities=conflicting_identities,
            duplicate_identities=duplicate_identities,
        )
        row_status = RowStatus.ERROR if errors else RowStatus.OK
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
                status=row_status,
            )
        )

    batch.total_rows = len(staged_rows)
    batch.error_rows = error_count
    session.flush()


def stage_import(
    session: Session,
    *,
    filename: str,
    period: str,
    rows: list[SalaryRow],
    source: SalarySource = SalarySource.IMPORT,
    file_sha256: str | None = None,
    created_by: int | None = None,
) -> ImportBatch:
    """逐行校验并写入暂存表；历史来源仍按兼容规则去重。"""
    batch = ImportBatch(
        filename=filename,
        period=period,
        source=source,
        status=ImportStatus.PARSED,
        total_rows=0,
        file_sha256=file_sha256,
        created_by=created_by,
    )
    session.add(batch)
    session.flush()
    _write_staging_rows(session, batch=batch, period=period, rows=rows, source=source)
    return batch


def restage_import(
    session: Session,
    batch: ImportBatch,
    *,
    filename: str,
    period: str,
    rows: list[SalaryRow],
    created_by: int | None = None,
) -> bool:
    """Atomically replace a still-PARSED batch's validation snapshot.

    The dataset advisory lock and batch row lock use the same ordering as
    confirmation. If confirmation or publication won the race, the immutable
    evidence is left untouched and ``False`` is returned.
    """
    del filename, created_by  # Preserve the original upload's evidence metadata.
    lock_legacy_salary_dataset(session)
    session.refresh(batch, with_for_update=True)
    if (
        batch.status is not ImportStatus.PARSED
        or batch.source is not SalarySource.IMPORT
        or batch.published_batch_id is not None
    ):
        return False
    if batch.period != period:
        raise ImportError_("不能使用不同计薪周期重新暂存导入批次")

    session.execute(delete(ImportStagingRow).where(ImportStagingRow.batch_id == batch.id))
    _write_staging_rows(
        session,
        batch=batch,
        period=period,
        rows=rows,
        source=batch.source,
    )
    return True


def confirm_import(session: Session, batch: ImportBatch) -> int:
    """把批次的 OK 行写入 salary_record；有 ERROR 行则拒绝。返回写入条数。"""
    # 行锁防并发/双击重复确认；成功重试返回同一批次的原写入数。
    # Catalog evidence is reviewed as one append-only dataset. Take the same
    # transaction lock used by preview/apply before inserting any source row.
    lock_legacy_salary_dataset(session)
    session.refresh(batch, with_for_update=True)
    if batch.status == ImportStatus.CONFIRMED:
        written = (
            session.scalar(
                select(func.count())
                .select_from(SalaryRecord)
                .where(SalaryRecord.import_batch_id == batch.id)
            )
            or 0
        )
        if written <= 0:
            raise ImportError_("批次已确认但工资记录缺失，请联系系统管理员")
        return int(written)
    if batch.error_rows > 0:
        raise ImportError_(f"存在 {batch.error_rows} 行错误，请先修正后再确认")

    # 门店名 → org_unit_id 解析（仅门店类型，同名作为歧义拒绝）
    org_by_name: dict[str, int | None] = _active_store_ids_by_name(session)

    staged = session.scalars(
        select(ImportStagingRow).where(
            ImportStagingRow.batch_id == batch.id,
            ImportStagingRow.status == RowStatus.OK,
        )
    ).all()

    employee_ids: dict[str, int] = {}
    if batch.source is SalarySource.IMPORT:
        unsupported_fields = sorted(
            {
                str(field)
                for srow in staged
                if isinstance(srow.parsed_fields, dict)
                for field in srow.parsed_fields
                if str(field) not in CURRENT_IMPORT_FIELDS
            }
        )
        if unsupported_fields:
            visible = unsupported_fields[:10]
            suffix = (
                f"；另有 {len(unsupported_fields) - len(visible)} 个字段"
                if len(unsupported_fields) > len(visible)
                else ""
            )
            raise ImportError_(
                "导入批次包含模板未支持的字段："
                + "、".join(visible)
                + suffix
                + "；请重新上传系统模板"
            )
        employee_ids = _employee_ids_by_emp_no(
            session, {srow.emp_no for srow in staged if srow.emp_no}
        )
        employee_org_ids = _employee_org_ids_by_emp_no(
            session, {srow.emp_no for srow in staged if srow.emp_no}
        )
        employee_names = _employee_names_by_emp_no(
            session, {srow.emp_no for srow in staged if srow.emp_no}
        )
        unmatched = sorted(
            {srow.emp_no or "(缺少工号)" for srow in staged if srow.emp_no not in employee_ids}
        )
        if unmatched:
            identifiers = "、".join(unmatched)
            raise ImportError_(f"工号 {identifiers} 未匹配员工主数据；请重新暂存并完成人工认领")

        mismatched_names = sorted(
            {
                srow.emp_no or "(missing employee number)"
                for srow in staged
                if srow.emp_no and srow.name.strip() != employee_names.get(srow.emp_no, "").strip()
            }
        )
        if mismatched_names:
            raise ImportError_(
                "Workbook name does not match the employee master record for: "
                + ", ".join(mismatched_names)
            )

        invalid_stores = sorted(
            {srow.store_name for srow in staged if org_by_name.get(srow.store_name) is None}
        )
        if invalid_stores:
            raise ImportError_(
                "Workbook stores are missing or ambiguous in active master data: "
                + ", ".join(invalid_stores)
            )
        mismatched_employees = sorted(
            {
                srow.emp_no or "(missing employee number)"
                for srow in staged
                if employee_org_ids.get(srow.emp_no or "") != org_by_name.get(srow.store_name)
            }
        )
        if mismatched_employees:
            raise ImportError_(
                "Workbook store does not match the employee master store for: "
                + ", ".join(mismatched_employees)
            )

    written = 0
    for srow in staged:
        record = SalaryRecord(
            period=srow.period,
            emp_no=srow.emp_no,
            name=srow.name,
            store_name=srow.store_name,
            org_unit_id=org_by_name.get(srow.store_name),
            employee_id=employee_ids.get(srow.emp_no) if srow.emp_no else None,
            source=batch.source,
            fields=srow.parsed_fields,
            import_batch_id=batch.id,
            created_by=batch.created_by,
        )
        session.add(record)
        session.flush()
        srow.salary_record_id = record.id
        written += 1

    batch.status = ImportStatus.CONFIRMED
    batch.confirmed_at = datetime.now(UTC)
    session.flush()
    return written
