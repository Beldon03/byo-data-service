import sqlite3
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app import db
from app.main import create_app


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    connection = db.connect(":memory:")
    db.init_registry(connection)
    yield connection
    connection.close()


@pytest.fixture
def client() -> Iterator[TestClient]:
    app = create_app(db_path=":memory:")
    with TestClient(app) as test_client:
        yield test_client
