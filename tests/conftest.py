import pytest
import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent.parent / "src" / "schema.sql"


@pytest.fixture
def db():
    """In-memory SQLite DB with schema loaded."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA_PATH.read_text())
    yield conn
    conn.close()
