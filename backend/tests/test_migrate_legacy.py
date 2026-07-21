from decimal import Decimal

import pytest

from app.importing.migrate_legacy import (
    LegacyAlreadyMigrated,
    LegacyMigrationError,
    migrate_rows,
)
from app.models.org import OrgType, OrgUnit
from app.models.salary import SalaryRecord, SalarySource

pytestmark = pytest.mark.usefixtures("pg_engine")


def test_migrate_rows_maps_and_reconciles(db_session):
    store = OrgUnit(code="S1", name="广州店", type=OrgType.STORE, city="广州")
    db_session.add(store)
    db_session.flush()
    legacy = [
        {
            "月份": "2026-05",
            "姓名": "张三",
            "门店": "广州店",
            "标准门店": "广州店",
            "合计工资": "5000",
            "综合薪资": "5000",
        },
        {"月份": "2026-05", "姓名": "李四", "门店": "未知店", "合计工资": "3,000"},
    ]
    report = migrate_rows(db_session, legacy)
    assert report.written == 2
    assert report.matched_org == 1  # 只有广州店匹配到组织
    assert report.total_cost == Decimal("8000")  # 5000 + 3000（对账）
    assert report.periods == {"2026-05": 2}

    recs = db_session.query(SalaryRecord).all()
    assert all(r.source == SalarySource.HISTORICAL for r in recs)
    zhang = next(r for r in recs if r.name == "张三")
    assert zhang.org_unit_id == store.id
    assert "月份" not in zhang.fields  # 元数据键不进 fields
    assert zhang.fields["合计工资"] == "5000"


def test_migrate_rejects_malformed_rows_without_partial_write(db_session):
    legacy = [
        {"月份": "2026-05", "姓名": "张三", "门店": "广州店", "合计工资": "5000"},
        {"月份": "2026-05", "姓名": "李四"},  # 无门店
    ]

    with pytest.raises(LegacyMigrationError, match=r"第 2 行.*缺少门店"):
        migrate_rows(db_session, legacy)

    assert db_session.query(SalaryRecord).count() == 0


def test_migrate_rejects_unparseable_legacy_total(db_session):
    legacy = [{"月份": "2026-05", "姓名": "张三", "门店": "广州店", "合计工资": "五千"}]

    with pytest.raises(LegacyMigrationError, match=r"第 1 行.*合计工资"):
        migrate_rows(db_session, legacy)


def test_migrate_rechecks_idempotency_inside_dataset_lock(db_session):
    legacy = [{"月份": "2026-05", "姓名": "张三", "门店": "广州店", "合计工资": "5000"}]
    migrate_rows(db_session, legacy)

    with pytest.raises(LegacyAlreadyMigrated, match="已存在 1 条历史记录"):
        migrate_rows(db_session, legacy)
