import sqlite3
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from app import db
from app.models import QueryRequest, QueryResult

router = APIRouter(prefix="/query", tags=["query"])

MAX_ROWS = 10_000
TIMEOUT_SECONDS = 5.0
_PROGRESS_INTERVAL_OPS = 10_000


def _jsonable(value: Any) -> Any:
    # BLOBs (reachable via functions like randomblob) are not JSON; hex is
    # lossless and unambiguous.
    if isinstance(value, bytes):
        return value.hex()
    return value


@router.post("")
async def run_query(request: QueryRequest, conn: db.QueryConnection) -> QueryResult:
    # Safety lives in the connection itself (PRAGMA query_only plus the
    # read-only authorizer), never in inspecting the SQL string here. The
    # progress handler bounds runtime so one runaway query (e.g. an
    # unbounded recursive CTE) cannot hang the whole single-threaded service.
    deadline = time.monotonic() + TIMEOUT_SECONDS
    conn.set_progress_handler(lambda: time.monotonic() > deadline, _PROGRESS_INTERVAL_OPS)
    try:
        cursor = conn.execute(request.sql)
        rows = cursor.fetchmany(MAX_ROWS + 1)
    except sqlite3.Error as exc:
        message = str(exc)
        # sqlite reports a progress-handler abort as exactly "interrupted";
        # substring matching would mislabel errors that merely contain it.
        if message == "interrupted":
            message = f"execution exceeded the {TIMEOUT_SECONDS:g} second budget"
        raise HTTPException(400, f"query rejected: {message}") from exc
    finally:
        conn.set_progress_handler(None, 0)
    if len(rows) > MAX_ROWS:
        raise HTTPException(400, f"query returned more than {MAX_ROWS} rows; add a LIMIT clause")
    columns = [description[0] for description in cursor.description or []]
    return QueryResult(columns=columns, rows=[[_jsonable(v) for v in row] for row in rows])
