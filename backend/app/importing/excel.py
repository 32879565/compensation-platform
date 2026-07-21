"""Excel 工资表读取（openpyxl）→ SalaryRow 列表。

聚焦标准结构：某行含"姓名"表头，其下为数据行；门店名取工作表首行或 sheet 名。
表头经 normalize_header 归一化，金额经 parse_money（失败置 None，由上层报错）。
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import IO

from openpyxl import load_workbook

from app.importing.header_rules import (
    MONEY_FIELDS,
    SKIP_SHEET_TITLES,
    SUM_MERGED_FIELD_LABELS,
    SUMMARY_ROW_NAMES,
)
from app.importing.parser import (
    SalaryRow,
    auto_store_aliases,
    clean_text,
    normalize_emp_no,
    normalize_header,
    normalize_store_name,
    parse_money,
    standard_store_name,
)
from app.importing.store_aliases import STORE_ALIASES

_SUMMARY_KEYWORDS = ("汇总", "总表", "合计", "初版", "一厅面")
_INVALID_STORE_TITLES = {"", "序号", "门店", "Sheet1", "`", "姓名"}


@dataclass
class ReadResult:
    rows: list[SalaryRow]
    warnings: list[str]


def _find_header_row(sheet, max_scan: int = 5) -> int | None:
    for r in range(1, max_scan + 1):
        values = [clean_text(c.value) for c in sheet[r]]
        if "姓名" in values:
            return r
    return None


def _sheet_store_name(sheet) -> str:
    first = [clean_text(c.value) for c in sheet[1] if clean_text(c.value)]
    if first and first[0] not in _INVALID_STORE_TITLES:
        return normalize_store_name(first[0])
    return normalize_store_name(sheet.title)


def _has_month_salary(headers: list[str], month: int | None) -> bool:
    if month is None:
        return False
    pat = re.compile(rf"{month}月\s*(综合薪资|薪资|工资|底薪)")
    return any(pat.fullmatch(re.sub(r"\s+", "", h)) for h in headers)


def _merge_cell(
    fields: dict[str, str], money: dict[str, object], canonical: str, raw: object
) -> None:
    """把一列的值合入记录，正确处理同名列碰撞（移植旧 add_record_value 语义）。

    - 金额：空/无法解析(None) 绝不覆盖已有真实值；SUM 字段跨列累加，其余 last-non-empty。
    - 文本：last-non-empty。
    """
    text = clean_text(raw)
    if text:
        fields[canonical] = text
    elif canonical not in fields:
        fields[canonical] = text

    if canonical not in MONEY_FIELDS:
        return
    value = parse_money(raw)
    if value is None:
        return  # 空/不可解析的后列不得把前列真实金额覆盖成 None
    existing = money.get(canonical)
    if canonical in SUM_MERGED_FIELD_LABELS and isinstance(existing, Decimal):
        money[canonical] = existing + value
    else:
        money[canonical] = value


def read_salary_workbook(
    source: str | IO[bytes],
    *,
    period: str,
    aliases: Mapping[str, str] | None = None,
) -> ReadResult:
    # The legacy mappings are part of the import contract.  Explicit mappings are an overlay
    # so a deployment can add a newly approved alias without losing the baseline catalogue.
    aliases = {**STORE_ALIASES, **(aliases or {})}
    month = int(period.split("-")[1]) if "-" in period else None
    wb = load_workbook(source, read_only=True, data_only=True)
    rows: list[SalaryRow] = []
    warnings: list[str] = []

    for sheet in wb.worksheets:
        title = sheet.title
        if any(k in title for k in _SUMMARY_KEYWORDS) or title in SKIP_SHEET_TITLES:
            continue
        header_row = _find_header_row(sheet)
        if header_row is None:
            warnings.append(f"工作表『{title}』未找到姓名列，已跳过")
            continue

        headers = [clean_text(c.value) for c in sheet[header_row]]
        has_month = _has_month_salary(headers, month)
        col_map: dict[int, str] = {}
        name_col: int | None = None
        emp_no_col: int | None = None
        for idx, raw in enumerate(headers, start=1):
            if raw == "姓名":
                name_col = idx
                continue
            if raw in ("工号", "员工编号", "员工号"):
                emp_no_col = idx
                continue
            canonical = normalize_header(
                raw, month=month, column_index=idx, has_month_salary=has_month
            )
            if canonical:
                col_map[idx] = canonical
        if name_col is None:
            continue

        store_name = standard_store_name(_sheet_store_name(sheet), aliases)

        for excel_row in sheet.iter_rows(min_row=header_row + 1):
            name = clean_text(excel_row[name_col - 1].value) if name_col else ""
            if not name or name == "姓名" or name in SUMMARY_ROW_NAMES:
                continue
            emp_no = normalize_emp_no(excel_row[emp_no_col - 1].value) if emp_no_col else None
            fields: dict[str, str] = {}
            money: dict[str, object] = {}
            for idx, canonical in col_map.items():
                if idx - 1 >= len(excel_row):
                    continue
                _merge_cell(fields, money, canonical, excel_row[idx - 1].value)
            rows.append(
                SalaryRow(
                    period=period,
                    name=name,
                    store_name=store_name,
                    emp_no=emp_no,
                    fields=fields,
                    money=money,  # type: ignore[arg-type]
                )
            )
    # Preserve the old parser's low-risk automatic ``X`` → ``X店`` repair as well.  It runs
    # after all sheets are available, so an alias can be inferred from a canonical sibling.
    automatic_aliases = auto_store_aliases({row.store_name for row in rows if row.store_name})
    for row in rows:
        row.store_name = standard_store_name(
            automatic_aliases.get(row.store_name, row.store_name), aliases
        )

    wb.close()
    return ReadResult(rows=rows, warnings=warnings)
