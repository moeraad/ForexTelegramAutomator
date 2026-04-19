import pytest
from src.db import connect, init_schema


@pytest.fixture
def db():
    """In-memory SQLite DB with schema loaded, using production connection settings."""
    conn = connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()
