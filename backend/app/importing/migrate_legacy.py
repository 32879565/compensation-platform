"""从旧 salary_search_app 的 salary_data.sqlite 迁移历史薪资到新库（source=HISTORICAL）。

旧缓存是单行 JSON blob（salary_cache.payload），含已解析的 rows。历史数据无工号，
按 (月份,姓名,标准门店) 落库并按门店名匹配 org_unit。迁移只读、可对账。

注意：旧系统按 (月份,姓名) 去重（缺门店）已在缓存阶段丢失部分同名不同店记录，
历史迁移无法恢复；going-forward 的 Excel 导入使用已修复的去重逻辑。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.org import OrgUnit
from app.models.salary import SalaryRecord, SalarySource

_META_KEYS = {"月份", "姓名", "门店", "标准门店", "工作表", "来源文件"}


@dataclass
class MigrationReport:
    total_rows: int
    written: int
    matched_org: int
    periods: dict[str, int]
    total_cost: Decimal  # 合计工资总额（对账用）


def load_legacy_rows(sqlite_path: str) -> list[dict]:
    conn = sqlite3.connect(sqlite_path)
    try:
        row = conn.execute("SELECT payload FROM salary_cache WHERE id = 1").fetchone()
    finally:
        conn.close()
    if not row:
        return []
    payload = json.loads(row[0])
    return payload.get("rows", [])


def migrate_rows(session: Session, legacy_rows: list[dict]) -> MigrationReport:
    org_by_name = {
        name: oid
        for oid, name in session.execute(
            select(OrgUnit.id, OrgUnit.name).where(OrgUnit.is_deleted.is_(False))
        ).all()
    }
    written = 0
    matched = 0
    periods: dict[str, int] = {}
    total_cost = Decimal(0)

    for r in legacy_rows:
        period = r.get("月份")
        name = r.get("姓名")
        store = r.get("标准门店") or r.get("门店")
        if not period or not name or not store:
            continue
        fields = {k: v for k, v in r.items() if k not in _META_KEYS and v not in (None, "")}
        org_id = org_by_name.get(store)
        if org_id is not None:
            matched += 1
        session.add(
            SalaryRecord(
                period=period,
                emp_no=None,
                name=name,
                store_name=store,
                org_unit_id=org_id,
                source=SalarySource.HISTORICAL,
                fields=fields,
            )
        )
        written += 1
        periods[period] = periods.get(period, 0) + 1
        cost = fields.get("合计工资")
        if cost:
            try:
                total_cost += Decimal(str(cost).replace(",", ""))
            except Exception:  # noqa: BLE001  对账容错，异常值不计入
                pass

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
        # 幂等保护：若已迁移过历史数据则拒绝重复
        existing = session.scalar(
            select(func.count())
            .select_from(SalaryRecord)
            .where(SalaryRecord.source == SalarySource.HISTORICAL)
        )
        if existing:
            print(f"已存在 {existing} 条历史记录，跳过迁移（如需重迁请先清理）。")
            return
        report = migrate_rows(session, legacy_rows)
        session.commit()
    print(
        f"历史迁移完成：源 {report.total_rows} 行 → 写入 {report.written} 条，"
        f"匹配门店 {report.matched_org} 条，覆盖 {len(report.periods)} 个月份，"
        f"合计工资总额 {report.total_cost}。"
    )


if __name__ == "__main__":  # pragma: no cover
    main()
