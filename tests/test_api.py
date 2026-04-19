import json
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _setup(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn


def test_get_actions_returns_only_sent(tmp_path):
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'sent')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4864, "entry_high": 4866,
                     "tps": [4880], "sl": 4855}),),
    )
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'pending')"
    )
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/actions")
    assert r.status_code == 200
    body = r.json()
    assert len(body["actions"]) == 1
    assert body["actions"][0]["action_type"] == "OPEN"


def test_get_kill_switch(tmp_path):
    conn = _setup(tmp_path)
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/settings/kill_switch")
    assert r.status_code == 200
    assert r.json() == {"key": "kill_switch", "value": "off"}


def test_post_result_executed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'sent')"
    )
    aid = cur.lastrowid
    app = build_app(conn)
    client = TestClient(app)
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 99001,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.10,
            "entry_price": 4865.0, "sl": 4855.0, "tp": 4880.0,
        },
    })
    assert r.status_code == 200
    row = conn.execute("SELECT * FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=99001").fetchone()
    assert pos is not None
    assert pos["status"] == "open"


def test_post_result_close_marks_position_closed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) VALUES(?, 555, 'XAUUSD', 'BUY', "
        "0.10, 4865, 4855, 4880, 'open')",
        (cur.lastrowid,),
    )
    app = build_app(conn)
    client = TestClient(app)
    r = client.post("/positions/555/close", json={"reason": "tp"})
    assert r.status_code == 200
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=555").fetchone()
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "tp"
