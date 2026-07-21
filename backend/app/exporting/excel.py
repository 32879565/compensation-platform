"""XLSX generation with spreadsheet-formula neutralization."""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence, Set
from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font

_FORMULA_PREFIXES = ("=", "+", "-", "@")
_XML_10_ILLEGAL_CONTROLS = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")


def safe_excel_text(value: object | None) -> str | None:
    """Prevent user-controlled text from being interpreted as an Excel formula."""
    if value is None:
        return None
    # PostgreSQL accepts these control bytes while XLSX's XML payload does
    # not. Clean first because removing a leading control byte can expose a
    # formula prefix that needs escaping as well.
    sanitized = _XML_10_ILLEGAL_CONTROLS.sub("", str(value))
    if sanitized.lstrip().startswith(_FORMULA_PREFIXES):
        return f"'{sanitized}"
    return sanitized


def tabular_workbook(
    *,
    sheet_title: str,
    headers: Sequence[str],
    rows: Iterable[Sequence[object]],
    text_columns: Set[int],
) -> bytes:
    """Build a fixed-template workbook and neutralize untrusted text cells."""
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = sheet_title
    sheet.append(list(headers))
    for cell in sheet[1]:
        cell.font = Font(bold=True)
    sheet.freeze_panes = "A2"

    for row in rows:
        if len(row) != len(headers):
            raise ValueError("Workbook row does not match the fixed template columns")
        sheet.append(
            [
                safe_excel_text(value) if index in text_columns else value
                for index, value in enumerate(row)
            ]
        )

    stream = BytesIO()
    workbook.save(stream)
    return stream.getvalue()


def payroll_workbook(rows: Iterable[Sequence[object]]) -> bytes:
    """Build a compact payroll workbook from already-authorized export rows."""
    return tabular_workbook(
        sheet_title="Payroll",
        headers=(
            "Period",
            "Employee No",
            "Employee Name",
            "Store Code",
            "Store Name",
            "Department",
            "Attendance Days",
            "Gross Pay",
            "Deposit",
            "Net Pay",
            "Carry Forward",
        ),
        rows=rows,
        text_columns={0, 1, 2, 3, 4, 5},
    )
