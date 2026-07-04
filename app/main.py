import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app import db


def create_app(db_path: str | None = None) -> FastAPI:
    path = db_path or os.environ.get("DATABASE_PATH", "/data/app.db")

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        conn = db.connect(path)
        db.init_registry(conn)
        app.state.db = conn
        try:
            yield
        finally:
            conn.close()

    return FastAPI(
        title="Bring Your Own Data Service",
        description="Upload CSV files as independent datasets and manage them via a REST API.",
        version="1.0.0",
        lifespan=lifespan,
    )


app = create_app()
