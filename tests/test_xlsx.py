import io
from datetime import datetime
from typing import Any

import httpx2
import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import xlsx
from app.ingestion import CsvError


def build_xlsx(rows: list[list[Any]]) -> bytes:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def test_parse_converts_cells_to_pipeline_strings() -> None:
    data = build_xlsx(
        [
            ["order_id", "amount", "ordered_on", "shipped", "note"],
            [1, 9.99, datetime(2026, 1, 15), True, "first"],
            [2, 12.5, datetime(2026, 1, 16, 10, 30), False, None],
        ]
    )

    header, rows = xlsx.parse_xlsx(data)

    assert header == ["order_id", "amount", "ordered_on", "shipped", "note"]
    assert rows[0] == ["1", "9.99", "2026-01-15", "true", "first"]
    assert rows[1] == ["2", "12.5", "2026-01-16T10:30:00", "false", ""]


def test_integral_floats_become_integer_strings() -> None:
    header, rows = xlsx.parse_xlsx(build_xlsx([["n"], [3.0], [4.0]]))

    assert rows == [["3"], ["4"]]


def test_phantom_rows_and_columns_are_trimmed() -> None:
    data = build_xlsx(
        [
            ["a", "b", None],
            [1, None, None],
            [None, None, None],
        ]
    )

    header, rows = xlsx.parse_xlsx(data)

    assert header == ["a", "b"]
    assert rows == [["1", ""]]


def test_chartsheet_workbooks_never_crash() -> None:
    workbook = openpyxl.Workbook()
    sheet = workbook.active
    sheet.append(["a"])
    sheet.append([1])
    workbook.active = workbook.create_chartsheet()
    buffer = io.BytesIO()
    workbook.save(buffer)

    # openpyxl 3.1.x cannot load chartsheet-bearing workbooks (reader bug);
    # the contract is that such files surface as a 400-bound CsvError, never
    # an unhandled exception. If a future openpyxl loads them, the sheet
    # fallback must pick the data worksheet.
    try:
        header, rows = xlsx.parse_xlsx(buffer.getvalue())
    except CsvError:
        return
    assert header == ["a"]
    assert rows == [["1"]]


def test_invalid_bytes_are_rejected() -> None:
    with pytest.raises(CsvError, match="not a valid XLSX"):
        xlsx.parse_xlsx(b"definitely not a workbook")


def test_empty_sheet_is_rejected() -> None:
    with pytest.raises(CsvError, match="empty"):
        xlsx.parse_xlsx(build_xlsx([]))


def test_header_only_sheet_is_rejected() -> None:
    with pytest.raises(CsvError, match="no data rows"):
        xlsx.parse_xlsx(build_xlsx([["a", "b"]]))


def upload_xlsx(client: TestClient, filename: str, rows: list[list[Any]]) -> httpx2.Response:
    return client.post(
        "/datasets",
        files={
            "file": (
                filename,
                build_xlsx(rows),
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        },
    )


def test_xlsx_upload_end_to_end(client: TestClient) -> None:
    response = upload_xlsx(
        client,
        "Sales Report.XLSX",
        [
            ["order_id", "amount", "ordered_on"],
            [1, 9.99, datetime(2026, 1, 15)],
            [2, 12.5, datetime(2026, 1, 16)],
        ],
    )

    assert response.status_code == 201
    assert response.json() == {
        "name": "sales_report",
        "table": "ds_sales_report",
        "columns": [
            {"name": "order_id", "type": "integer"},
            {"name": "amount", "type": "real"},
            {"name": "ordered_on", "type": "date"},
        ],
        "row_count": 2,
    }

    rows = client.get("/datasets/sales_report/rows").json()["rows"]
    assert rows[0]["ordered_on"] == "2026-01-15"


def test_corrupt_xlsx_upload_returns_400(client: TestClient) -> None:
    response = client.post(
        "/datasets", files={"file": ("bad.xlsx", b"PK\x03\x04 broken", "application/zip")}
    )

    assert response.status_code == 400
    assert "not a valid XLSX" in response.json()["detail"]
