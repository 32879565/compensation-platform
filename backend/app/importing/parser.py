"""薪资 Excel 解析纯函数（从旧系统移植并修复其正确性缺陷）。

修复的旧 bug（对照蓝图不变量）：
- 不变量1：金额一律 Decimal，禁 float。
- 不变量2：金额无法解析时返回 None 并由上层标记为错误，绝不静默归零。
- 不变量3：去重键含门店；无工号时按 (月份,姓名,门店)，绝不跨门店按姓名合并。
- 影子行判定只在「同键多条」组内做淘汰，绝不删除唯一真实记录。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

from app.importing.header_rules import (
    DROP_HEADERS,
    MERGED_FIELD_LABELS,
    POST_ALIASES,
    RENAME_HEADERS,
    SHADOW_MONEY_FIELDS,
)

_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩"
_CURRENCY = ("¥", "￥", "元", "人民币")
_DATE_RE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}")
# 占位符：工号列常填这些表示"无工号"，不能当作真实身份键
_PLACEHOLDER_TOKENS = frozenset({"", "-", "—", "/", "／", "无", "NA", "N/A", "n/a", "0"})
# 归一化为普通"综合薪资"的表头（用于 has_month_salary 存在时丢弃）
_PLAIN_COMPREHENSIVE = frozenset({"综合薪资", "底薪", "现综合薪资", "现薪资"})


def clean_text(value: Any) -> str:
    """规整单元格文本：None→''，换行/多空白压成单空格。"""
    if value is None:
        return ""
    from datetime import date, datetime

    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, date):
        return value.isoformat()
    text = str(value).replace("\n", " ").strip()
    return re.sub(r"\s+", " ", text)


def parse_money(raw: Any) -> Decimal | None:
    """把单元格值解析为 Decimal 金额；无法解析返回 None（上层据此报错，不归零）。"""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError):
            return None
        return value if value.is_finite() else None
    s = str(raw).strip()
    for sym in _CURRENCY:
        s = s.replace(sym, "")
    s = s.replace(",", "").replace("，", "").replace(" ", "").replace("　", "")
    s = s.replace("－", "-").replace("﹣", "-")  # 全角/小型负号归一
    if s in ("", "-", "—", "/", "无", "NA", "N/A"):
        return None
    # 会计括号负数 (200) → -200
    m = re.fullmatch(r"\(([\d.]+)\)", s)
    if m:
        s = "-" + m.group(1)
    try:
        value = Decimal(s)
    except InvalidOperation:
        return None
    return value if value.is_finite() else None


def normalize_emp_no(raw: object) -> str | None:
    """把工号占位符（无/-/／/N/A/0 等）归一化为 None，避免误当身份键。"""
    if raw is None:
        return None
    if isinstance(raw, (int, float, Decimal)):
        try:
            numeric = Decimal(str(raw))
        except InvalidOperation:
            return None
        if not numeric.is_finite() or numeric == 0:
            return None
    s = str(raw).strip()
    if s in _PLACEHOLDER_TOKENS:
        return None
    return s


def normalize_header(
    header: str,
    *,
    month: int | None = None,
    column_index: int | None = None,
    has_month_salary: bool = False,
) -> str | None:
    """归一化表头；返回 None 表示丢弃该列。month 为该文件所属月份数字。

    has_month_salary=True 时（同表存在『N月综合薪资』当月列），丢弃普通『综合薪资/底薪』
    等列，避免两列都映射综合薪资而相互覆盖（忠实移植旧 has_month_salary_header）。
    """
    header = header.strip()
    if not header:
        return None
    if _DATE_RE.fullmatch(header):
        return None
    if re.fullmatch(r"-?\d+(?:\.\d+)?", header):  # 纯数字表头（右侧汇总块）丢弃
        return None
    if has_month_salary and header in _PLAIN_COMPREHENSIVE:
        return None
    if header in DROP_HEADERS:
        return None

    compact = re.sub(r"\s+", "", header)
    compact = compact.lstrip(_CIRCLED)

    # 上下文相关规则（依赖列号/月份）
    if column_index is not None and column_index <= 3 and header.endswith("战队"):
        return "区域"
    if column_index is not None and column_index <= 4 and header.endswith("店"):
        return None
    if header == "区域" and column_index is not None and column_index >= 50:
        return None
    if header == "法定":
        return "法定出勤" if (column_index is not None and column_index >= 40) else "法定补贴"
    if header == "法定补贴" and column_index is not None and column_index >= 40:
        return "法定出勤"

    if header in RENAME_HEADERS:
        return RENAME_HEADERS[header]

    # N月综合薪资 / N月(薪资|工资|底薪)：仅当 N 等于文件月份才认作综合薪资，否则丢弃
    if month is not None:
        m = re.fullmatch(r"(\d{1,2})月\s*综合薪资", compact)
        if m:
            return "综合薪资" if int(m.group(1)) == month else None
        m = re.fullmatch(r"(\d{1,2})月\s*(薪资|工资|底薪)", compact)
        if m:
            return "综合薪资" if int(m.group(1)) == month else None

    if header in POST_ALIASES:
        return POST_ALIASES[header]
    if compact in POST_ALIASES:
        return POST_ALIASES[compact]
    if compact in MERGED_FIELD_LABELS:
        return compact
    return header


def infer_month(text: str, default_month: str | None = None) -> str | None:
    """从路径/文本推断月份（YYYY-MM）。文件名月份优先于目录（修复旧目录优先的错账）。"""
    ym = re.findall(r"(20\d{2})年\D*?(1[0-2]|0?[1-9])月", text)
    if ym:
        year, month = ym[-1]
        return f"{year}-{int(month):02d}"
    months = re.findall(r"(1[0-2]|0?[1-9])月", text)
    if months and default_month and re.fullmatch(r"20\d{2}-\d{2}", default_month):
        year = default_month.split("-", 1)[0]
        return f"{year}-{int(months[-1]):02d}"
    return default_month


def standard_store_name(store_name: str, aliases: Mapping[str, str]) -> str:
    """Resolve a store alias to its canonical name without allowing alias loops."""
    canonical = store_name
    seen: set[str] = set()
    while canonical in aliases and canonical not in seen:
        seen.add(canonical)
        canonical = aliases[canonical]
    return canonical


def normalize_store_name(store_name: Any) -> str:
    name = clean_text(store_name)
    name = re.sub(r"\d+月工资表.*$", "", name).strip()
    name = re.sub(r"月工资表.*$", "", name).strip()
    name = re.sub(r"[（(][^）)]*[ABC]\d(?:\.\d)?[^）)]*[）)]$", "", name).strip()
    name = re.sub(r"[ABC]\d(?:\.\d)?$", "", name).strip()
    name = re.sub(r"[（(]天[）)]$", "", name).strip()
    return name


def auto_store_aliases(store_names: set[str]) -> dict[str, str]:
    """把不带'店'的名字映射到已存在的'X店'（如 万科→万科店）。"""
    aliases: dict[str, str] = {}
    for name in store_names:
        if name.endswith("店"):
            continue
        target = f"{name}店"
        if target in store_names:
            aliases[name] = target
    return aliases


@dataclass
class SalaryRow:
    """一条待入库的薪资记录。money 字段为 Decimal|None（None=原值无法解析）。"""

    period: str
    name: str
    store_name: str
    emp_no: str | None = None
    fields: dict[str, Any] = field(default_factory=dict)
    money: dict[str, Decimal | None] = field(default_factory=dict)

    def identity_key(self) -> tuple:
        # 不变量3：员工工号在全局唯一，当前导入的身份只能是 (计薪周期, 工号)。
        # 门店是归属/展示字段，不能被混入身份键而让同一个员工在同一周期有两条工资。
        if self.emp_no:
            return (self.period, self.emp_no)
        # 缺工号行绝不能自动按姓名归并；这个回退键仅供诊断，dedupe_rows 会保留每行。
        return (self.period, None, self.name, self.store_name)


def is_shadow_row(row: SalaryRow) -> bool:
    """影子行：无综合薪资且核心金额字段全为空/零（员工调店产生的占位行）。"""
    if row.money.get("综合薪资"):
        return False
    for f in SHADOW_MONEY_FIELDS:
        v = row.money.get(f)
        if v is not None and v != 0:
            return False
    return bool(row.name and row.store_name)


def _best_sort_key(row: SalaryRow) -> tuple:
    def amt(name: str) -> Decimal:
        v = row.money.get(name)
        return v if isinstance(v, Decimal) else Decimal(0)

    return (
        1 if row.money.get("综合薪资") else 0,
        amt("合计工资"),
        amt("实发工资"),
        amt("应发工资"),
    )


def dedupe_rows(rows: list[SalaryRow]) -> list[SalaryRow]:
    """按 ``(period, emp_no)`` 去重。

    无工号的实际薪资行不按姓名去重，保留给暂存校验/人工认领，避免静默丢失
    来源行。仅当同名同店组中存在可识别的影子行时，才安全地移除该影子行。
    有工号时同一周期的重复行做影子行淘汰并按金额取最优一条。
    """
    groups: dict[tuple, list[SalaryRow]] = {}
    for row in rows:
        # 无工号时这个分组只用于识别可安全丢弃的影子行，绝不能把两个实际薪资行合并。
        key = (
            row.identity_key()
            if row.emp_no
            else ("unidentified", row.period, row.name, row.store_name)
        )
        groups.setdefault(key, []).append(row)

    cleaned: list[SalaryRow] = []
    for group in groups.values():
        if len(group) == 1:
            cleaned.append(group[0])
            continue
        real = [r for r in group if not is_shadow_row(r)]
        if not group[0].emp_no:
            # Retain every non-shadow unidentified line for manual review.  If the group is an
            # obvious shadow + actual-row pair, retaining the shadow adds no information.
            cleaned.extend(real if real else group)
            continue
        candidates = real or group
        candidates.sort(key=_best_sort_key, reverse=True)
        cleaned.append(candidates[0])
    return cleaned
