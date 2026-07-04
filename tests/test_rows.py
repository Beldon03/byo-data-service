import pytest
from fastapi.testclient import TestClient

SALES_CSV = (
    b"order_id,amount,ordered_on,note\n"
    b"1,9.99,2026-01-15,first\n"
    b"2,12.50,2026-01-16,second\n"
    b"3,5.00,2026-01-17,third\n"
)


@pytest.fixture
def sales(client: TestClient) -> TestClient:
    response = client.post("/datasets", files={"file": ("sales.csv", SALES_CSV, "text/csv")})
    assert response.status_code == 201
    return client


def row_count(client: TestClient) -> int:
    return client.get("/datasets").json()[0]["row_count"]


def test_browse_returns_rows_with_row_id(sales: TestClient) -> None:
    body = sales.get("/datasets/sales/rows").json()

    assert (body["total"], body["limit"], body["offset"]) == (3, 100, 0)
    assert body["rows"][0] == {
        "_row_id": 1,
        "order_id": 1,
        "amount": 9.99,
        "ordered_on": "2026-01-15",
        "note": "first",
    }


def test_browse_pagination(sales: TestClient) -> None:
    body = sales.get("/datasets/sales/rows", params={"limit": 1, "offset": 1}).json()

    assert [r["_row_id"] for r in body["rows"]] == [2]
    assert body["total"] == 3


def test_browse_unknown_dataset_returns_404(client: TestClient) -> None:
    assert client.get("/datasets/missing/rows").status_code == 404


def test_browse_rejects_invalid_limit(sales: TestClient) -> None:
    assert sales.get("/datasets/sales/rows", params={"limit": 0}).status_code == 422


def test_get_single_row(sales: TestClient) -> None:
    row = sales.get("/datasets/sales/rows/2").json()

    assert row["_row_id"] == 2
    assert row["note"] == "second"


def test_get_unknown_row_returns_404(sales: TestClient) -> None:
    response = sales.get("/datasets/sales/rows/99")

    assert response.status_code == 404
    assert "row 99" in response.json()["detail"]


def test_insert_returns_created_row(sales: TestClient) -> None:
    response = sales.post(
        "/datasets/sales/rows",
        json={"order_id": 4, "amount": 3, "ordered_on": "2026-02-01", "note": "new"},
    )

    assert response.status_code == 201
    assert response.json() == {
        "_row_id": 4,
        "order_id": 4,
        "amount": 3.0,
        "ordered_on": "2026-02-01",
        "note": "new",
    }
    assert row_count(sales) == 4


def test_insert_missing_columns_become_null(sales: TestClient) -> None:
    response = sales.post("/datasets/sales/rows", json={"order_id": 4})

    assert response.status_code == 201
    assert response.json()["note"] is None


def test_insert_empty_payload_creates_all_null_row(sales: TestClient) -> None:
    response = sales.post("/datasets/sales/rows", json={})

    assert response.status_code == 201
    assert response.json()["order_id"] is None


def test_insert_unknown_column_returns_422(sales: TestClient) -> None:
    response = sales.post("/datasets/sales/rows", json={"bogus": 1})

    assert response.status_code == 422
    assert "unknown column" in response.json()["detail"]


def test_insert_cannot_set_row_id(sales: TestClient) -> None:
    response = sales.post("/datasets/sales/rows", json={"_row_id": 99})

    assert response.status_code == 422
    assert "_row_id" in response.json()["detail"]


@pytest.mark.parametrize(
    "payload",
    [
        {"order_id": "abc"},
        {"order_id": True},
        {"order_id": 2**63},
        {"amount": "not a number"},
        {"ordered_on": "not-a-date"},
        {"ordered_on": 20260101},
        {"note": 5},
    ],
)
def test_insert_type_invalid_value_returns_422(sales: TestClient, payload: dict) -> None:
    response = sales.post("/datasets/sales/rows", json=payload)

    assert response.status_code == 422
    assert "expects" in response.json()["detail"]


def test_insert_nonfinite_real_returns_422(sales: TestClient) -> None:
    # Compliant JSON cannot express Infinity, but Python's json.loads accepts
    # the literal, so a raw body can smuggle it past the parser.
    response = sales.post(
        "/datasets/sales/rows",
        content=b'{"amount": Infinity}',
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 422
    assert "expects" in response.json()["detail"]


def test_insert_null_values_are_allowed(sales: TestClient) -> None:
    response = sales.post("/datasets/sales/rows", json={"order_id": None, "note": None})

    assert response.status_code == 201


def test_update_changes_subset_and_returns_full_row(sales: TestClient) -> None:
    response = sales.patch("/datasets/sales/rows/2", json={"amount": 20.0})

    assert response.status_code == 200
    assert response.json() == {
        "_row_id": 2,
        "order_id": 2,
        "amount": 20.0,
        "ordered_on": "2026-01-16",
        "note": "second",
    }


def test_update_empty_payload_returns_row_unchanged(sales: TestClient) -> None:
    response = sales.patch("/datasets/sales/rows/2", json={})

    assert response.status_code == 200
    assert response.json()["amount"] == 12.50


def test_update_unknown_row_returns_404(sales: TestClient) -> None:
    assert sales.patch("/datasets/sales/rows/99", json={"note": "x"}).status_code == 404


def test_update_unknown_dataset_returns_404(client: TestClient) -> None:
    assert client.patch("/datasets/missing/rows/1", json={}).status_code == 404


def test_update_unknown_column_returns_422(sales: TestClient) -> None:
    assert sales.patch("/datasets/sales/rows/1", json={"bogus": 1}).status_code == 422


def test_update_type_invalid_value_returns_422(sales: TestClient) -> None:
    assert sales.patch("/datasets/sales/rows/1", json={"order_id": "abc"}).status_code == 422


def test_delete_row(sales: TestClient) -> None:
    assert sales.delete("/datasets/sales/rows/2").status_code == 204
    assert sales.get("/datasets/sales/rows/2").status_code == 404
    assert sales.get("/datasets/sales/rows").json()["total"] == 2
    assert row_count(sales) == 2


def test_delete_unknown_row_returns_404(sales: TestClient) -> None:
    assert sales.delete("/datasets/sales/rows/99").status_code == 404
