import math
import sqlite3
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Query

from app import db, ingestion
from app.models import RowsPage
from app.routers.datasets import get_dataset_or_404

router = APIRouter(prefix="/datasets/{name}/rows", tags=["rows"])

# All table/column identifiers interpolated below come from the registry via
# get_dataset_or_404 or from payload keys validated against it; values are
# always bound as parameters.


def _validate_payload(dataset: db.Dataset, payload: dict[str, Any]) -> dict[str, Any]:
    types = {c.name: c.type for c in dataset.columns}
    validated: dict[str, Any] = {}
    for column, value in payload.items():
        if column == "_row_id":
            raise HTTPException(422, "_row_id is assigned by the service and cannot be set")
        if column not in types:
            raise HTTPException(422, f"unknown column {column!r}")
        validated[column] = _validate_value(column, types[column], value)
    return validated


def _validate_value(column: str, logical: str, value: Any) -> Any:
    def rejection() -> HTTPException:
        return HTTPException(422, f"column {column!r} expects {logical}, got {value!r}")

    if value is None:
        return None
    if logical == "integer":
        # bool is an int subclass but true/false in an integer column is a
        # type error; ints beyond 64 bits cannot be bound by SQLite.
        if isinstance(value, bool) or not isinstance(value, int):
            raise rejection()
        if not db.INT64_MIN <= value <= db.INT64_MAX:
            raise rejection()
        return value
    if logical == "real":
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise rejection()
        try:
            numeric = float(value)
        except OverflowError:
            # JSON ints can be arbitrarily large; float() (and even
            # math.isfinite) raises rather than saturating for them.
            raise rejection() from None
        if not math.isfinite(numeric):
            raise rejection()
        return numeric
    if logical == "date":
        if not isinstance(value, str) or not ingestion.is_iso_date(value):
            raise rejection()
        return value.strip()
    if not isinstance(value, str):
        raise rejection()
    return value


def _row_or_404(conn: sqlite3.Connection, name: str, table: str, row_id: int) -> dict[str, Any]:
    row = conn.execute(f'SELECT * FROM "{table}" WHERE _row_id = ?', (row_id,)).fetchone()
    if row is None:
        raise HTTPException(404, f"row {row_id} does not exist in dataset {name!r}")
    return dict(row)


@router.get("")
async def browse_rows(
    name: str,
    conn: db.Connection,
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> RowsPage:
    dataset = get_dataset_or_404(conn, name)
    rows = conn.execute(
        f'SELECT * FROM "{dataset.table_name}" ORDER BY _row_id LIMIT ? OFFSET ?',
        (limit, offset),
    ).fetchall()
    total = conn.execute(f'SELECT COUNT(*) FROM "{dataset.table_name}"').fetchone()[0]
    return RowsPage(rows=[dict(row) for row in rows], total=total, limit=limit, offset=offset)


@router.get("/{row_id}")
async def get_row(name: str, row_id: int, conn: db.Connection) -> dict[str, Any]:
    dataset = get_dataset_or_404(conn, name)
    return _row_or_404(conn, name, dataset.table_name, row_id)


@router.post("", status_code=201)
async def insert_row(name: str, payload: dict[str, Any], conn: db.Connection) -> dict[str, Any]:
    dataset = get_dataset_or_404(conn, name)
    values = _validate_payload(dataset, payload)
    table = dataset.table_name
    if values:
        columns = ", ".join(f'"{c}"' for c in values)
        placeholders = ", ".join("?" for _ in values)
        cursor = conn.execute(
            f'INSERT INTO "{table}" ({columns}) VALUES ({placeholders})',
            list(values.values()),
        )
    else:
        cursor = conn.execute(f'INSERT INTO "{table}" DEFAULT VALUES')
    db.adjust_row_count(conn, name, 1)
    conn.commit()
    return _row_or_404(conn, name, table, cursor.lastrowid or 0)


@router.patch("/{row_id}")
async def update_row(
    name: str, row_id: int, payload: dict[str, Any], conn: db.Connection
) -> dict[str, Any]:
    dataset = get_dataset_or_404(conn, name)
    values = _validate_payload(dataset, payload)
    current = _row_or_404(conn, name, dataset.table_name, row_id)
    if not values:
        return current
    assignments = ", ".join(f'"{c}" = ?' for c in values)
    conn.execute(
        f'UPDATE "{dataset.table_name}" SET {assignments} WHERE _row_id = ?',
        [*values.values(), row_id],
    )
    conn.commit()
    return _row_or_404(conn, name, dataset.table_name, row_id)


@router.delete("/{row_id}", status_code=204)
async def delete_row(name: str, row_id: int, conn: db.Connection) -> None:
    dataset = get_dataset_or_404(conn, name)
    # Existence check first: raising after DML would leave the shared
    # connection inside an open transaction.
    _row_or_404(conn, name, dataset.table_name, row_id)
    conn.execute(f'DELETE FROM "{dataset.table_name}" WHERE _row_id = ?', (row_id,))
    db.adjust_row_count(conn, name, -1)
    conn.commit()
