import sqlite3

import pytest
from fastapi.testclient import TestClient

from app import db

SALES = db.Dataset(
    name="sales",
    table_name="ds_sales",
    columns=[db.Column("order_id", "INTEGER"), db.Column("amount", "REAL")],
    row_count=3,
    created_at="2026-07-04T00:00:00+00:00",
)


def test_init_registry_is_idempotent(conn: sqlite3.Connection) -> None:
    db.init_registry(conn)
    db.init_registry(conn)


def test_register_and_get_roundtrip(conn: sqlite3.Connection) -> None:
    db.register_dataset(conn, SALES)
    conn.commit()

    assert db.get_dataset(conn, "sales") == SALES


def test_get_unknown_dataset_returns_none(conn: sqlite3.Connection) -> None:
    assert db.get_dataset(conn, "missing") is None


def test_duplicate_name_raises_integrity_error(conn: sqlite3.Connection) -> None:
    db.register_dataset(conn, SALES)
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError):
        db.register_dataset(conn, SALES)


def test_list_datasets_returns_all(conn: sqlite3.Connection) -> None:
    other = db.Dataset(
        name="customers",
        table_name="ds_customers",
        columns=[db.Column("name", "TEXT")],
        row_count=1,
        created_at="2026-07-04T01:00:00+00:00",
    )
    db.register_dataset(conn, SALES)
    db.register_dataset(conn, other)
    conn.commit()

    assert [d.name for d in db.list_datasets(conn)] == ["sales", "customers"]


def test_unregister_removes_dataset(conn: sqlite3.Connection) -> None:
    db.register_dataset(conn, SALES)
    conn.commit()

    db.unregister_dataset(conn, "sales")
    conn.commit()

    assert db.get_dataset(conn, "sales") is None


def test_adjust_row_count(conn: sqlite3.Connection) -> None:
    db.register_dataset(conn, SALES)
    conn.commit()

    db.adjust_row_count(conn, "sales", 1)
    db.adjust_row_count(conn, "sales", -2)
    conn.commit()

    dataset = db.get_dataset(conn, "sales")
    assert dataset is not None
    assert dataset.row_count == 2


def test_app_lifespan_opens_database(client: TestClient) -> None:
    assert client.get("/openapi.json").status_code == 200
