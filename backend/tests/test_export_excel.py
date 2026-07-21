from io import BytesIO

import pytest
from fastapi import HTTPException
from openpyxl import load_workbook

from app.exporting.excel import payroll_workbook, safe_excel_text
from app.routers.export import _valid_period_or_422


def test_excel_formula_prefixes_are_neutralized_even_after_whitespace() -> None:
    assert safe_excel_text("=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert safe_excel_text(" +cmd") == "' +cmd"
    assert safe_excel_text("-1") == "'-1"
    assert safe_excel_text("@mention") == "'@mention"
    assert safe_excel_text("\x01=SUM(A1:A2)") == "'=SUM(A1:A2)"
    assert safe_excel_text("ordinary employee") == "ordinary employee"


def test_payroll_workbook_writes_text_as_text_and_numbers_as_numbers() -> None:
    content = payroll_workbook(
        [
            (
                "2026-07",
                "+EMP-1",
                '\x01=HYPERLINK("https://example.test")',
                "@STORE",
                "-Store",
                "DINING",
                22,
                1000.5,
                0,
                1000.5,
                0,
            )
        ]
    )
    sheet = load_workbook(BytesIO(content), data_only=False).active

    assert sheet["B2"].value == "'+EMP-1"
    assert sheet["C2"].value == '\'=HYPERLINK("https://example.test")'
    assert sheet["D2"].value == "'@STORE"
    assert sheet["E2"].value == "'-Store"
    assert sheet["H2"].value == 1000.5


def test_export_period_requires_a_real_calendar_month() -> None:
    _valid_period_or_422("2026-07")
    with pytest.raises(HTTPException) as exc_info:
        _valid_period_or_422("2026-13")
    assert exc_info.value.status_code == 422
