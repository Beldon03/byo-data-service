import httpx
import pytest
from fastapi.testclient import TestClient

SALES_CSV = b"order_id,amount\n1,9.99\n2,12.50\n"
CUSTOMERS_CSV = b"customer_id,country\n1,sg\n2,de\n"


@pytest.fixture
def seeded(client: TestClient) -> TestClient:
    for filename, data in (("sales.csv", SALES_CSV), ("customers.csv", CUSTOMERS_CSV)):
        response = client.post("/datasets", files={"file": (filename, data, "text/csv")})
        assert response.status_code == 201
    return client


def query(client: TestClient, sql: str) -> httpx.Response:
    return client.post("/query", json={"sql": sql})


def test_select_returns_columns_and_rows(seeded: TestClient) -> None:
    response = query(seeded, "SELECT order_id, amount FROM ds_sales ORDER BY _row_id")

    assert response.status_code == 200
    assert response.json() == {"columns": ["order_id", "amount"], "rows": [[1, 9.99], [2, 12.5]]}


def test_joins_across_datasets(seeded: TestClient) -> None:
    response = query(
        seeded,
        "SELECT s.order_id, c.country FROM ds_sales s"
        " JOIN ds_customers c ON c.customer_id = s.order_id ORDER BY s.order_id",
    )

    assert response.status_code == 200
    assert response.json()["rows"] == [[1, "sg"], [2, "de"]]


def test_aggregates_work(seeded: TestClient) -> None:
    response = query(seeded, "SELECT COUNT(*), SUM(amount) FROM ds_sales")

    [[count, total]] = response.json()["rows"]
    assert count == 2
    assert total == pytest.approx(22.49)


def test_recursive_cte_works(seeded: TestClient) -> None:
    response = query(
        seeded,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i < 3)"
        " SELECT i FROM n",
    )

    assert response.status_code == 200
    assert response.json()["rows"] == [[1], [2], [3]]


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO ds_sales (order_id) VALUES (99)",
        "UPDATE ds_sales SET amount = 0",
        "DELETE FROM ds_sales",
        "DROP TABLE ds_sales",
        "CREATE TABLE evil (x)",
        "ALTER TABLE ds_sales ADD COLUMN evil TEXT",
        "PRAGMA writable_schema = ON",
        "ATTACH DATABASE ':memory:' AS other",
        "VACUUM",
    ],
)
def test_write_and_admin_statements_are_rejected(seeded: TestClient, sql: str) -> None:
    response = query(seeded, sql)

    assert response.status_code == 400
    assert "query rejected" in response.json()["detail"]


def test_writes_are_rejected_without_side_effects(seeded: TestClient) -> None:
    query(seeded, "DELETE FROM ds_sales")

    assert seeded.get("/datasets/sales/rows").json()["total"] == 2


def test_registry_access_is_rejected(seeded: TestClient) -> None:
    response = query(seeded, "SELECT * FROM _registry")

    assert response.status_code == 400
    assert "query rejected" in response.json()["detail"]


def test_multiple_statements_are_rejected(seeded: TestClient) -> None:
    response = query(seeded, "SELECT 1; DELETE FROM ds_sales")

    assert response.status_code == 400
    assert seeded.get("/datasets/sales/rows").json()["total"] == 2


def test_syntax_error_returns_400(seeded: TestClient) -> None:
    response = query(seeded, "SELEC broken")

    assert response.status_code == 400


def test_unknown_table_returns_400(seeded: TestClient) -> None:
    response = query(seeded, "SELECT * FROM nope")

    assert response.status_code == 400
    assert "no such table" in response.json()["detail"]


def test_blob_values_are_hex_encoded(seeded: TestClient) -> None:
    response = query(seeded, "SELECT randomblob(4) AS b")

    assert response.status_code == 200
    value = response.json()["rows"][0][0]
    assert len(value) == 8
    int(value, 16)


def test_result_row_cap(seeded: TestClient) -> None:
    response = query(
        seeded,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n WHERE i <= 10000)"
        " SELECT i FROM n",
    )

    assert response.status_code == 400
    assert "add a LIMIT" in response.json()["detail"]


def test_runaway_query_hits_execution_budget(
    seeded: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import query as query_router

    monkeypatch.setattr(query_router, "TIMEOUT_SECONDS", 0.05)
    response = query(
        seeded,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i + 1 FROM n)"
        " SELECT count(*) FROM n",
    )

    assert response.status_code == 400
    assert "budget" in response.json()["detail"]


def test_query_sees_rows_inserted_via_api(seeded: TestClient) -> None:
    seeded.post("/datasets/sales/rows", json={"order_id": 3, "amount": 1.0})

    response = query(seeded, "SELECT COUNT(*) FROM ds_sales")

    assert response.json()["rows"] == [[3]]
