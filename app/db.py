import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Request

# SQLite INTEGER columns are 64-bit; Python ints that exceed this range
# cannot be bound and must be rejected or demoted by callers.
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_REGISTRY_DDL = """
CREATE TABLE IF NOT EXISTS _registry (
    name TEXT PRIMARY KEY,
    table_name TEXT NOT NULL UNIQUE,
    columns TEXT NOT NULL,
    row_count INTEGER NOT NULL,
    created_at TEXT NOT NULL
)
"""


@dataclass(frozen=True)
class Column:
    name: str
    type: str


@dataclass(frozen=True)
class Dataset:
    name: str
    table_name: str
    columns: list[Column]
    row_count: int
    created_at: str


def connect(path: str) -> sqlite3.Connection:
    if path != ":memory:":
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    # Single shared connection; the assessment explicitly requires no
    # concurrency guarantees, and one connection keeps :memory: databases
    # usable across requests in tests.
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_registry(conn: sqlite3.Connection) -> None:
    conn.execute(_REGISTRY_DDL)
    conn.commit()


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


# Registry writers do not commit; callers own the transaction so that a
# dataset's table DDL, bulk insert, and registry row land atomically.


def register_dataset(conn: sqlite3.Connection, dataset: Dataset) -> None:
    conn.execute(
        "INSERT INTO _registry (name, table_name, columns, row_count, created_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            dataset.name,
            dataset.table_name,
            json.dumps([{"name": c.name, "type": c.type} for c in dataset.columns]),
            dataset.row_count,
            dataset.created_at,
        ),
    )


def unregister_dataset(conn: sqlite3.Connection, name: str) -> None:
    conn.execute("DELETE FROM _registry WHERE name = ?", (name,))


def adjust_row_count(conn: sqlite3.Connection, name: str, delta: int) -> None:
    conn.execute(
        "UPDATE _registry SET row_count = row_count + ? WHERE name = ?", (delta, name)
    )


def get_dataset(conn: sqlite3.Connection, name: str) -> Dataset | None:
    row = conn.execute("SELECT * FROM _registry WHERE name = ?", (name,)).fetchone()
    return _to_dataset(row) if row else None


def list_datasets(conn: sqlite3.Connection) -> list[Dataset]:
    rows = conn.execute("SELECT * FROM _registry ORDER BY created_at, name").fetchall()
    return [_to_dataset(row) for row in rows]


def _to_dataset(row: sqlite3.Row) -> Dataset:
    return Dataset(
        name=row["name"],
        table_name=row["table_name"],
        columns=[Column(name=c["name"], type=c["type"]) for c in json.loads(row["columns"])],
        row_count=row["row_count"],
        created_at=row["created_at"],
    )


def get_db(request: Request) -> sqlite3.Connection:
    return request.app.state.db


Connection = Annotated[sqlite3.Connection, Depends(get_db)]
