import pytest
from src.db import connect, init_schema


@pytest.fixture(autouse=True)
def _clear_ea_shared_token(monkeypatch):
    """Disable API auth_gate for every test by default.

    Tests inherit `.env` at import time, so when the operator has set
    EA_SHARED_TOKEN for production every test_api request would 401.
    Tests that specifically exercise the auth path (the
    test_auth_gate_* tests) set the token explicitly via their own
    monkeypatch.setattr, which takes precedence over this autouse fixture.
    """
    from src import config
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")


@pytest.fixture
def db():
    """In-memory SQLite DB with schema loaded, using production connection settings."""
    conn = connect(":memory:")
    init_schema(conn)
    yield conn
    conn.close()
