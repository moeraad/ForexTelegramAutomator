# tests/test_api_manual.py
import pytest
from pydantic import ValidationError
from src.api_models import ManualOpenBody


def test_manual_open_body_minimal_market():
    b = ManualOpenBody(side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05)
    assert b.symbol == "XAUUSD"
    assert b.pending is False
    assert b.comment == "manual"


def test_manual_open_body_rejects_nonpositive_lot():
    with pytest.raises(ValidationError):
        ManualOpenBody(side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.0)


def test_manual_open_body_rejects_bad_side():
    with pytest.raises(ValidationError):
        ManualOpenBody(side="LONG", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05)


import json
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _app(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn, build_app(conn)


def test_post_manual_inserts_sent_open_with_flag(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/actions/manual", json={
        "side": "BUY", "entry": 4500.0, "sl": 4490.0, "tp": 4530.0, "lot": 0.05,
    })
    assert r.status_code == 200, r.text
    action_id = r.json()["action_id"]
    assert r.json()["status"] == "sent"
    row = conn.execute(
        "SELECT action_type, status, source_msg_id, payload_json FROM actions WHERE id=?",
        (action_id,),
    ).fetchone()
    assert row["action_type"] == "OPEN"
    assert row["status"] == "sent"
    assert row["source_msg_id"] is None
    p = json.loads(row["payload_json"])
    assert p["manual"] is True
    assert p["source"] == "manual_gui"
    assert p["lot"] == 0.05
    assert p["entry_low"] == 4500.0 and p["entry_high"] == 4500.0
    assert p["tps"] == [4530.0]
    assert p["pending"] is False


def test_post_manual_rejects_sl_wrong_side(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    # BUY with SL above entry -> OpenAction geometry validator rejects.
    r = client.post("/actions/manual", json={
        "side": "BUY", "entry": 4500.0, "sl": 4510.0, "tp": 4530.0, "lot": 0.05,
    })
    assert r.status_code == 422


def test_post_manual_rejects_bad_lot(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/actions/manual", json={
        "side": "BUY", "entry": 4500.0, "sl": 4490.0, "tp": 4530.0, "lot": 0.0,
    })
    assert r.status_code == 422
