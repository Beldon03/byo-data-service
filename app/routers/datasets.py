import sqlite3

from fastapi import APIRouter, HTTPException, UploadFile

from app import db, ingestion, xlsx
from app.models import ColumnSchema, DatasetCreated, DatasetSchema, DatasetSummary

router = APIRouter(prefix="/datasets", tags=["datasets"])


def get_dataset_or_404(conn: sqlite3.Connection, name: str) -> db.Dataset:
    dataset = db.get_dataset(conn, name)
    if dataset is None:
        raise HTTPException(404, f"dataset {name!r} does not exist")
    return dataset


@router.post("", status_code=201)
async def upload_dataset(file: UploadFile, conn: db.Connection) -> DatasetCreated:
    name = ingestion.dataset_slug(file.filename or "")
    if db.get_dataset(conn, name) is not None:
        raise HTTPException(409, f"dataset {name!r} already exists")

    data = await file.read()
    if (file.filename or "").lower().endswith(".xlsx"):
        header, rows = xlsx.parse_xlsx(data)
        dataset = ingestion.ingest_rows(conn, name, header, rows)
    else:
        dataset = ingestion.ingest_csv(conn, name, data)
    conn.commit()
    return DatasetCreated(
        name=dataset.name,
        table=dataset.table_name,
        columns=[ColumnSchema(name=c.name, type=c.type) for c in dataset.columns],
        row_count=dataset.row_count,
    )


@router.get("")
async def list_datasets(conn: db.Connection) -> list[DatasetSummary]:
    return [
        DatasetSummary(name=d.name, row_count=d.row_count, created_at=d.created_at)
        for d in db.list_datasets(conn)
    ]


@router.get("/{name}/schema")
async def get_schema(name: str, conn: db.Connection) -> DatasetSchema:
    dataset = get_dataset_or_404(conn, name)
    return DatasetSchema(
        columns=[ColumnSchema(name=c.name, type=c.type) for c in dataset.columns]
    )


@router.delete("/{name}", status_code=204)
async def delete_dataset(name: str, conn: db.Connection) -> None:
    dataset = get_dataset_or_404(conn, name)
    # table_name comes from the registry, the only identifier source the
    # design allows to be interpolated into SQL.
    conn.execute(f'DROP TABLE "{dataset.table_name}"')
    db.unregister_dataset(conn, name)
    conn.commit()
