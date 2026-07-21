"""从旧 salary_search_app 的 salary_data.sqlite 迁移历史薪资到新库（source=HISTORICAL）。

旧缓存是单行 JSON blob（salary_cache.payload），含已解析的 rows。历史数据无工号，
按 (月份,姓名,标准门店) 落库并按门店名匹配 org_unit。迁移只读、可对账。

注意：旧系统按 (月份,姓名) 去重（缺门店）已在缓存阶段丢失部分同名不同店记录，
历史迁移无法恢复；going-forward 的 Excel 导入使用已修复的去重逻辑。
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.importing.parser import (
    clean_text,
    normalize_store_name,
    parse_money,
    standard_store_name,
)
from app.importing.source_lock import lock_legacy_salary_dataset
from app.importing.store_aliases import STORE_ALIASES
from app.models.org import OrgType, OrgUnit
from app.models.salary import SalaryRecord, SalarySource

_META_KEYS = {"月份", "姓名", "门店", "标准门店", "工作表", "来源文件"}
_PERIOD_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_LOAD_REVISION = "089dead90284"


@dataclass
class MigrationReport:
    total_rows: int
    written: int
    matched_org: int
    periods: dict[str, int]
    total_cost: Decimal  # 合计工资总额（对账用）


class LegacyMigrationError(ValueError):
    """The legacy cache contains rows that cannot safely be migrated."""

    def __init__(self, issues: list[str]):
        self.issues = issues
        super().__init__(f"历史薪资迁移校验失败：{'；'.join(issues)}")


class LegacyAlreadyMigrated(LegacyMigrationError):
    """Raised under the dataset lock when historical rows already exist."""

    def __init__(self, record_count: int):
        self.record_count = record_count
        super().__init__([f"已存在 {record_count} 条历史记录"])


@dataclass(frozen=True)
class _LegacyRow:
    period: str
    name: str
    store_name: str
    fields: dict
    total_cost: Decimal


def assert_legacy_load_revision(session: Session) -> None:
    """Fail closed unless the CLI is run at the pre-backfill S6 revision."""

    revision = session.scalar(text("SELECT version_num FROM alembic_version"))
    if revision != _LOAD_REVISION:
        raise LegacyMigrationError(
            [
                "目标库必须先执行 alembic upgrade 089dead90284，导入完成后再执行 "
                f"alembic upgrade head；当前版本为 {revision or 'unknown'}"
            ]
        )


def _canonical_store_name(row: dict) -> str:
    raw_store = row.get("标准门店") or row.get("门店")
    return standard_store_name(normalize_store_name(raw_store), STORE_ALIASES)


def _validate_legacy_rows(legacy_rows: list[dict]) -> list[_LegacyRow]:
    """Validate every source row before creating any ORM objects.

    This deliberately aggregates all malformed row diagnostics and fails before the first
    insert.  A historic migration is an auditable conversion, not a best-effort import.
    """
    issues: list[str] = []
    normalized: list[_LegacyRow] = []
    for row_index, row in enumerate(legacy_rows, start=1):
        if not isinstance(row, dict):
            issues.append(f"第 {row_index} 行不是对象记录")
            continue

        period = clean_text(row.get("月份"))
        name = clean_text(row.get("姓名"))
        store_name = _canonical_store_name(row)
        row_issues: list[str] = []
        if not period:
            row_issues.append("缺少月份")
        elif not _PERIOD_RE.fullmatch(period):
            row_issues.append(f"月份格式无效『{period}』")
        if not name:
            row_issues.append("缺少姓名")
        if not store_name:
            row_issues.append("缺少门店")

        raw_total = row.get("合计工资")
        total_cost = Decimal(0)
        if raw_total not in (None, ""):
            parsed_total = parse_money(raw_total)
            if parsed_total is None:
                row_issues.append(f"合计工资无法解析『{clean_text(raw_total)}』")
            else:
                total_cost = parsed_total

        if row_issues:
            issues.append(f"第 {row_index} 行" + "、".join(row_issues))
            continue

        fields = {
            key: value
            for key, value in row.items()
            if key not in _META_KEYS and value not in (None, "")
        }
        normalized.append(
            _LegacyRow(
                period=period,
                name=name,
                store_name=store_name,
                fields=fields,
                total_cost=total_cost,
            )
        )

    if issues:
        raise LegacyMigrationError(issues)
    return normalized


def load_legacy_rows(sqlite_path: str) -> list[dict]:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT payload FROM salary_cache WHERE id = 1").fetchone()
    finally:
        conn.close()
    if not row:
        return []
    try:
        payload = json.loads(row[0])
    except (TypeError, json.JSONDecodeError) as exc:
        raise LegacyMigrationError(["salary_cache payload 不是有效 JSON"]) from exc
    if not isinstance(payload, dict):
        raise LegacyMigrationError(["salary_cache payload 必须是对象"])
    rows = payload.get("rows")
    if not isinstance(rows, list):
        raise LegacyMigrationError(["salary_cache payload 缺少 rows 数组"])
    return rows


def migrate_rows(session: Session, legacy_rows: list[dict]) -> MigrationReport:
    lock_legacy_salary_dataset(session)
    existing = session.scalar(
        select(func.count())
        .select_from(SalaryRecord)
        .where(SalaryRecord.source == SalarySource.HISTORICAL)
    )
    if existing:
        raise LegacyAlreadyMigrated(int(existing))
    normalized_rows = _validate_legacy_rows(legacy_rows)
    org_by_name = {
        name: oid
        for oid, name in session.execute(
            select(OrgUnit.id, OrgUnit.name).where(
                OrgUnit.is_deleted.is_(False), OrgUnit.type == OrgType.STORE
            )
        ).all()
    }
    written = 0
    matched = 0
    periods: dict[str, int] = {}
    total_cost = Decimal(0)

    for row in normalized_rows:
        org_id = org_by_name.get(row.store_name)
        if org_id is not None:
            matched += 1
        session.add(
            SalaryRecord(
                period=row.period,
                emp_no=None,
                name=row.name,
                store_name=row.store_name,
                org_unit_id=org_id,
                source=SalarySource.HISTORICAL,
                fields=row.fields,
            )
        )
        written += 1
        periods[row.period] = periods.get(row.period, 0) + 1
        total_cost += row.total_cost

    session.flush()
    return MigrationReport(
        total_rows=len(legacy_rows),
        written=written,
        matched_org=matched,
        periods=periods,
        total_cost=total_cost,
    )


def main() -> None:  # pragma: no cover - CLI 入口
    import argparse

    from app.db.session import SessionLocal

    parser = argparse.ArgumentParser(description="迁移旧 salary_data.sqlite 历史薪资")
    parser.add_argument("--sqlite", required=True, help="旧 salary_data.sqlite 路径")
    args = parser.parse_args()

    legacy_rows = load_legacy_rows(args.sqlite)
    with SessionLocal() as session:
        assert_legacy_load_revision(session)
        try:
            report = migrate_rows(session, legacy_rows)
        except LegacyAlreadyMigrated as exc:
            session.rollback()
            print(f"已存在 {exc.record_count} 条历史记录，跳过迁移（如需重迁请先清理）。")
            return
        session.commit()
    print(
        f"历史迁移完成：源 {report.total_rows} 行 → 写入 {report.written} 条，"
        f"匹配门店 {report.matched_org} 条，覆盖 {len(report.periods)} 个月份，"
        f"合计工资总额 {report.total_cost}。"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
