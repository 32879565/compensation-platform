from decimal import Decimal

import pytest

from app.importing.parser import (
    SalaryRow,
    auto_store_aliases,
    dedupe_rows,
    infer_month,
    is_shadow_row,
    normalize_header,
    normalize_store_name,
    parse_money,
    standard_store_name,
)


# ---------------- parse_money（不变量1/2：Decimal + 失败不归零）----------------
@pytest.mark.parametrize(
    "raw,expected",
    [
        (3500, Decimal("3500")),
        (3500.5, Decimal("3500.5")),
        (Decimal("3500.55"), Decimal("3500.55")),
        ("3500", Decimal("3500")),
        ("3,500.50", Decimal("3500.50")),
        ("3，500", Decimal("3500")),  # 全角逗号
        ("¥3500", Decimal("3500")),
        ("3500元", Decimal("3500")),
        ("(200)", Decimal("-200")),  # 会计括号负数
        ("-200", Decimal("-200")),
    ],
)
def test_parse_money_success(raw, expected):
    assert parse_money(raw) == expected


@pytest.mark.parametrize("raw", [None, "", "  ", "abc", "8%", "无", "-", "3500元整"])
def test_parse_money_failure_returns_none_not_zero(raw):
    # 关键修复：无法解析返回 None（上层报错），绝不返回 0
    assert parse_money(raw) is None


def test_parse_money_preserves_precision():
    assert parse_money("4500.55") == Decimal("4500.55")


# ---------------- normalize_header ----------------
def test_header_rename():
    assert normalize_header("职务") == "职位"
    assert normalize_header("底薪") == "综合薪资"
    assert normalize_header("矿工") == "旷工"  # 错别字修正
    assert normalize_header("通勤费") == "补贴"


def test_header_drop():
    assert normalize_header("序号") is None
    assert normalize_header("性别") is None
    assert normalize_header("2026-05-01") is None  # 日期形表头


def test_header_post_alias():
    assert normalize_header("传菜") == "传菜岗"
    assert normalize_header("经理") == "经理岗"


def test_header_month_salary_context():
    # N月综合薪资：仅当 N==文件月份才认作综合薪资
    assert normalize_header("5月综合薪资", month=5) == "综合薪资"
    assert normalize_header("4月综合薪资", month=5) is None


def test_header_legal_attendance_by_column():
    assert normalize_header("法定", column_index=45) == "法定出勤"
    assert normalize_header("法定", column_index=10) == "法定补贴"


def test_header_passthrough_unknown():
    assert normalize_header("综合薪资") == "综合薪资"
    assert normalize_header("某个未知字段") == "某个未知字段"


# ---------------- 门店归一化 ----------------
def test_store_alias_and_normalize():
    aliases = {"万科": "天河智慧城店"}
    assert standard_store_name("万科", aliases) == "天河智慧城店"
    assert normalize_store_name("海岸城店月工资表（C0.8）") == "海岸城店"


def test_auto_store_aliases():
    names = {"万科", "万科店", "广州店"}
    assert auto_store_aliases(names) == {"万科": "万科店"}


# ---------------- infer_month ----------------
def test_infer_month():
    assert infer_month("工资核对表/2026年/5月/深圳一区.xlsx") == "2026-05"
    assert infer_month("4月东莞.xlsx", default_month="2026-05") == "2026-04"


# ---------------- 去重（核心 bug 修复）----------------
def _row(name, store, period="2026-05", emp_no=None, **money):
    return SalaryRow(
        period=period,
        name=name,
        store_name=store,
        emp_no=emp_no,
        money={k: (Decimal(str(v)) if v is not None else None) for k, v in money.items()},
    )


def test_dedupe_same_name_different_store_both_kept():
    # 关键修复：两个不同门店的同名员工不再被误删
    rows = [
        _row("张伟", "广州店", 综合薪资=5000),
        _row("张伟", "深圳店", 综合薪资=6000),
    ]
    result = dedupe_rows(rows)
    assert len(result) == 2
    stores = {r.store_name for r in result}
    assert stores == {"广州店", "深圳店"}


def test_dedupe_single_zero_record_kept():
    # 关键修复：唯一一条记录即便看似影子行也保留（如全月请假实发0的员工）
    rows = [_row("李四", "广州店", 应发工资=0, 合计工资=0, 实发工资=0)]
    result = dedupe_rows(rows)
    assert len(result) == 1
    assert result[0].name == "李四"


def test_dedupe_multi_row_shadow_eliminated():
    # 同一人同店多条：影子行被淘汰，保留有金额的真实行
    rows = [
        _row("王五", "广州店", 应发工资=0),  # 影子占位
        _row("王五", "广州店", 综合薪资=5000, 合计工资=5000),
    ]
    result = dedupe_rows(rows)
    assert len(result) == 1
    assert result[0].money["综合薪资"] == Decimal("5000")


def test_dedupe_by_emp_no_when_present():
    # 有工号时按工号归并（同工号跨表重复取最优）
    rows = [
        _row("张三", "广州店", emp_no="E1", 合计工资=3000),
        _row("张三", "广州店", emp_no="E1", 合计工资=5000),
    ]
    result = dedupe_rows(rows)
    assert len(result) == 1
    assert result[0].money["合计工资"] == Decimal("5000")


def test_is_shadow_row():
    assert is_shadow_row(_row("A", "店", 应发工资=0, 合计工资=0)) is True
    assert is_shadow_row(_row("A", "店", 综合薪资=5000)) is False


# ---------------- 复核修复：占位工号不作身份键 ----------------
def test_placeholder_emp_no_normalized_to_none():
    from app.importing.parser import normalize_emp_no

    for token in ("无", "-", "／", "N/A", "0", "", "  "):
        assert normalize_emp_no(token) is None
    assert normalize_emp_no("E1001") == "E1001"


def test_placeholder_emp_no_does_not_collapse_distinct_people():
    # 三个不同人工号都填占位符"无"→ 归一为 None → 按 (姓名,门店) 区分，不被合并
    rows = [
        _row("张三", "广州店", emp_no=None, 合计工资=5000),
        _row("李四", "广州店", emp_no=None, 合计工资=6000),
        _row("王五", "广州店", emp_no=None, 合计工资=7000),
    ]
    assert len(dedupe_rows(rows)) == 3


def test_same_emp_no_cross_store_kept_separate():
    # 同工号跨店（调店拆分发薪）是两条真实记录，不能合并
    rows = [
        _row("张三", "广州店", emp_no="E1", 合计工资=3000),
        _row("张三", "深圳店", emp_no="E1", 合计工资=2500),
    ]
    result = dedupe_rows(rows)
    assert len(result) == 2
    assert sum(r.money["合计工资"] for r in result) == Decimal("5500")


def test_full_width_minus_parsed():
    assert parse_money("－150") == Decimal("-150")


# ---------------- 复核修复：has_month_salary 丢弃普通综合薪资 ----------------
def test_has_month_salary_drops_plain_comprehensive():
    # 存在"5月综合薪资"当月列时，普通"综合薪资"/"底薪"列应丢弃避免碰撞
    assert normalize_header("综合薪资", month=5, has_month_salary=True) is None
    assert normalize_header("底薪", month=5, has_month_salary=True) is None
    assert normalize_header("5月综合薪资", month=5, has_month_salary=True) == "综合薪资"
    # 无当月列时普通综合薪资照常保留
    assert normalize_header("综合薪资", month=5, has_month_salary=False) == "综合薪资"


def test_numeric_header_dropped():
    assert normalize_header("3000") is None
    assert normalize_header("-150.5") is None
