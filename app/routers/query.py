import sqlite3

from fastapi import APIRouter, HTTPException

from app import db
from app.models import QueryRequest, QueryResult

router = APIRouter(prefix="/query", tags=["query"])


@router.post("")
async def run_query(request: QueryRequest, conn: db.QueryConnection) -> QueryResult:
    # Safety lives in the connection itself (PRAGMA query_only plus the
    # read-only authorizer), never in inspecting the SQL string here.
    try:
        cursor = conn.execute(request.sql)
        rows = cursor.fetchall()
    except sqlite3.Error as exc:
        raise HTTPException(400, f"query rejected: {exc}") from exc
    columns = [description[0] for description in cursor.description or []]
    return QueryResult(columns=columns, rows=[list(row) for row in rows])
