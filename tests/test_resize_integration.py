"""End-to-end-ish: a watching OPEN gets resized; a mock EA loop polls the
action, observes the override, and 're-places' by POSTing watching with a new
ticket. Asserts the action row stays a single continuous 'watching' lifecycle.
"""
import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _setup(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn


def test_resize_then_mock_ea_replace_keeps_single_lifecycle(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    payload = {
        "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4501.49, "entry_high": 4501.49,
        "tps": [4544.0], "sl": 4494.26, "pending": True, "pending_type": "limit",
    }
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'watching')",
        (json.dumps(payload),),
    )
    conn.commit()
    aid = cur.lastrowid

    # Operator resizes.
    r = client.post(f"/actions/{aid}/resize_pending", json={"lots": 0.43})
    assert r.status_code == 200 and r.json()["resize_seq"] == 1

    # Mock EA poll: read the action, see the override.
    got = client.get(f"/actions/{aid}").json()
    assert got["status"] == "watching"
    assert got["payload"]["pending_lot_override"] == 0.43
    assert got["payload"]["resize_seq"] == 1

    # Mock EA re-places: POST watching with a NEW broker ticket.
    rep = client.post(
        f"/actions/{aid}/result",
        json={"status": "watching", "error": "pending_order_ticket=222222"},
    )
    assert rep.status_code == 200

    # Still exactly one row, still watching, ticket updated.
    rows = conn.execute("SELECT status, ea_response FROM actions WHERE id=?", (aid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "watching"
    assert "222222" in (rows[0]["ea_response"] or "")
