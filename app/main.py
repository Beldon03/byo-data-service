import os
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse

from app import db
from app.ingestion import CsvError
from app.routers import datasets, query, rows


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DATABASE_PATH", "/data/app.db")
    if path == ":memory:":
        # A plain :memory: database is per-connection; the read-only /query
        # connection must see the same data, so tests get a shared-cache URI.
        path = f"file:{uuid.uuid4().hex}?mode=memory&cache=shared"

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(path)
        try:
            db.init_registry(conn)
            app.state.db = conn
            query_conn = db.connect_query_only(path)
            try:
                app.state.query_db = query_conn
                yield
            finally:
                query_conn.close()
        finally:
            conn.close()

    app = FastAPI(
        title="Bring Your Own Data Service",
        description="Upload CSV files as independent datasets and manage them via a REST API.",
        version="1.0.0",
        lifespan=lifespan,
    )
    app.include_router(datasets.router)
    app.include_router(rows.router)
    app.include_router(query.router)

    @app.get("/", include_in_schema=False)
    async def index() -> FileResponse:
        return FileResponse(Path(__file__).parent / "static" / "index.html")

    @app.exception_handler(CsvError)
    async def csv_error_handler(request: Request, exc: CsvError) -> JSONResponse:
        # A failed ingest must not leave a half-created table behind.
        request.app.state.db.rollback()
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        # Any unexpected failure (disk full mid-insert, ...) would otherwise
        # leave an open transaction on the shared connection whose half-done
        # work the next successful request commits.
        request.app.state.db.rollback()
        return JSONResponse(status_code=500, content={"detail": "internal server error"})

    return app


app = create_app()
