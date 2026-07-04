import httpx
from fastapi.testclient import TestClient

from app.main import create_app

SALES_CSV = b"order_id,amount,ordered_on,note\n1,9.99,2026-01-15,first\n2,12.50,2026-01-16,\n"


def upload(
    client: TestClient, filename: str = "sales.csv", data: bytes = SALES_CSV
) -> httpx.Response:
    return client.post("/datasets", files={"file": (filename, data, "text/csv")})


def test_upload_returns_created_dataset(client: TestClient) -> None:
    response = upload(client)

    assert response.status_code == 201
    assert response.json() == {
        "name": "sales",
        "table": "ds_sales",
        "columns": [
            {"name": "order_id", "type": "integer"},
            {"name": "amount", "type": "real"},
            {"name": "ordered_on", "type": "date"},
            {"name": "note", "type": "text"},
        ],
        "row_count": 2,
    }


def test_upload_duplicate_name_returns_409(client: TestClient) -> None:
    upload(client)

    response = upload(client)

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_upload_empty_file_returns_400(client: TestClient) -> None:
    response = upload(client, data=b"")

    assert response.status_code == 400
    assert "empty" in response.json()["detail"]


def test_upload_header_only_returns_400(client: TestClient) -> None:
    response = upload(client, data=b"a,b,c\n")

    assert response.status_code == 400
    assert "no data rows" in response.json()["detail"]


def test_upload_ragged_long_row_returns_400_and_rolls_back(client: TestClient) -> None:
    response = upload(client, data=b"a,b\n1,2,3\n")

    assert response.status_code == 400
    assert upload(client).status_code == 201


def test_upload_unusable_filename_returns_400(client: TestClient) -> None:
    response = upload(client, filename="###.csv")

    assert response.status_code == 400
    assert "cannot derive" in response.json()["detail"]


def test_list_datasets(client: TestClient) -> None:
    upload(client)
    upload(client, filename="customers.csv", data=b"customer_id,name\n1,ann\n")

    response = client.get("/datasets")

    assert response.status_code == 200
    listed = response.json()
    assert [d["name"] for d in listed] == ["sales", "customers"]
    assert listed[0]["row_count"] == 2
    assert "created_at" in listed[0]


def test_list_datasets_empty(client: TestClient) -> None:
    assert client.get("/datasets").json() == []


def test_get_schema(client: TestClient) -> None:
    upload(client)

    response = client.get("/datasets/sales/schema")

    assert response.status_code == 200
    assert response.json() == {
        "columns": [
            {"name": "order_id", "type": "integer"},
            {"name": "amount", "type": "real"},
            {"name": "ordered_on", "type": "date"},
            {"name": "note", "type": "text"},
        ]
    }


def test_get_schema_unknown_dataset_returns_404(client: TestClient) -> None:
    response = client.get("/datasets/missing/schema")

    assert response.status_code == 404
    assert "does not exist" in response.json()["detail"]


def test_delete_dataset(client: TestClient) -> None:
    upload(client)

    assert client.delete("/datasets/sales").status_code == 204
    assert client.get("/datasets/sales/schema").status_code == 404
    assert client.get("/datasets").json() == []


def test_delete_frees_the_name_for_reupload(client: TestClient) -> None:
    upload(client)
    client.delete("/datasets/sales")

    assert upload(client).status_code == 201


def test_delete_unknown_dataset_returns_404(client: TestClient) -> None:
    assert client.delete("/datasets/missing").status_code == 404


def test_binary_upload_returns_400(client: TestClient) -> None:
    response = upload(client, filename="image.csv", data=b"\x89PNG\r\n\x1a\n\x00\x00")

    assert response.status_code == 400
    assert "binary" in response.json()["detail"]


def test_unhandled_error_rolls_back_the_transaction(monkeypatch) -> None:
    import sqlite3

    from app import db, ingestion

    def explode(conn: sqlite3.Connection, name: str, data: bytes) -> db.Dataset:
        conn.execute('CREATE TABLE "ds_boom" (x)')
        raise RuntimeError("boom")

    monkeypatch.setattr(ingestion, "ingest_csv", explode)
    with TestClient(create_app(db_path=":memory:"), raise_server_exceptions=False) as client:
        response = upload(client, filename="boom.csv")

        assert response.status_code == 500
        assert response.json() == {"detail": "internal server error"}
        assert not client.app.state.db.in_transaction
