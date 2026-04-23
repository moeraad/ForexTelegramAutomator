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


def test_claim_moves_sent_to_claimed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'sent')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/claim")
    assert r.status_code == 200
    row = conn.execute("SELECT status, claimed_at FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_at"] is not None


def test_claim_returns_409_if_not_sent(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'pending')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/claim")
    assert r.status_code == 409


def test_claim_race_only_one_winner(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'sent')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r1 = client.post(f"/actions/{aid}/claim")
    r2 = client.post(f"/actions/{aid}/claim")
    assert {r1.status_code, r2.status_code} == {200, 409}


def test_post_result_with_legs_inserts_all_positions(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'claimed')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "legs": [
            {"mt5_ticket": 1001, "snapshot": {
                "symbol": "XAUUSD", "side": "BUY", "volume": 0.05,
                "entry_price": 4865.0, "sl": 4855.0, "tp": 4880.0}},
            {"mt5_ticket": 1002, "snapshot": {
                "symbol": "XAUUSD", "side": "BUY", "volume": 0.05,
                "entry_price": 4865.0, "sl": 4855.0, "tp": 4890.0}},
        ],
    })
    assert r.status_code == 200
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    positions = conn.execute(
        "SELECT mt5_ticket, tp FROM positions WHERE action_id=? ORDER BY mt5_ticket", (aid,)
    ).fetchall()
    assert [p["mt5_ticket"] for p in positions] == [1001, 1002]
    assert [p["tp"] for p in positions] == [4880.0, 4890.0]


def test_post_result_does_not_resurrect_closed_position(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, closed_at, close_reason) VALUES("
        "?, 777, 'XAUUSD', 'BUY', 0.10, 4865, 4855, 4880, 'closed', "
        "'2026-04-20T00:00:00+00:00', 'tp')",
        (aid,),
    )
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 777,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.10,
            "entry_price": 4865.0, "sl": 4855.0, "tp": 4880.0,
        },
    })
    assert r.status_code == 200
    pos = conn.execute("SELECT status, close_reason FROM positions WHERE mt5_ticket=777").fetchone()
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "tp"


def test_update_position_applies_partial_fields(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) VALUES(?, 321, 'XAUUSD', 'BUY', "
        "0.30, 4865, 4855, 4890, 'open')",
        (cur.lastrowid,),
    )
    client = TestClient(build_app(conn))
    r = client.post("/positions/321/update", json={"volume": 0.20, "sl": 4865.0})
    assert r.status_code == 200
    assert r.json() == {"ok": True, "updated": 2}
    pos = conn.execute(
        "SELECT volume, sl, tp FROM positions WHERE mt5_ticket=321"
    ).fetchone()
    assert pos["volume"] == 0.20
    assert pos["sl"] == 4865.0
    assert pos["tp"] == 4890.0


def test_update_position_rejects_closed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, closed_at, close_reason) VALUES("
        "?, 654, 'XAUUSD', 'BUY', 0.10, 4865, 4855, 4880, 'closed', "
        "'2026-04-20T00:00:00+00:00', 'tp')",
        (cur.lastrowid,),
    )
    client = TestClient(build_app(conn))
    r = client.post("/positions/654/update", json={"sl": 4870.0})
    assert r.status_code == 409


def test_update_position_404_unknown_ticket(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/positions/999999/update", json={"sl": 4870.0})
    assert r.status_code == 404


def test_post_watching_persists_watch_and_expires_at(tmp_path):
    """EA reports it is watching a zone: DB records watch_json + expires_at,
    status flips to watching, and executed_at stays NULL (non-terminal)."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'claimed')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "watching",
        "watch": {
            "symbol": "XAUUSD", "side": "BUY",
            "zone_low": 4864.0, "zone_high": 4866.0,
            "sl": 4855.0, "tps": [4880.0, 4890.0], "volume": 0.10,
        },
        "expires_at": "2026-04-20T23:00:00+00:00",
    })
    assert r.status_code == 200
    row = conn.execute(
        "SELECT status, watch_json, expires_at, executed_at FROM actions WHERE id=?",
        (aid,),
    ).fetchone()
    assert row["status"] == "watching"
    assert row["executed_at"] is None
    assert row["expires_at"] == "2026-04-20T23:00:00+00:00"
    watch = json.loads(row["watch_json"])
    assert watch["zone_low"] == 4864.0
    assert watch["tps"] == [4880.0, 4890.0]


def test_post_watching_requires_watch_and_expires_at(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'claimed')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={"status": "watching"})
    assert r.status_code == 422


def test_get_watching_returns_watch_fields(tmp_path):
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, watch_json, expires_at) "
        "VALUES('OPEN', '{}', 'watching', ?, ?)",
        (json.dumps({"zone_low": 4864, "zone_high": 4866, "sl": 4855, "tps": [4880]}),
         "2026-04-20T23:00:00+00:00"),
    )
    client = TestClient(build_app(conn))
    r = client.get("/actions?status=watching")
    assert r.status_code == 200
    actions = r.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["watch"]["zone_low"] == 4864
    assert actions[0]["expires_at"] == "2026-04-20T23:00:00+00:00"


def test_post_watching_then_executed_transition(tmp_path):
    """Price enters the zone: EA POSTs executed with a ticket and the position row lands."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'claimed')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r1 = client.post(f"/actions/{aid}/result", json={
        "status": "watching",
        "watch": {"zone_low": 4864, "zone_high": 4866, "sl": 4855, "tps": [4880]},
        "expires_at": "2026-04-20T23:00:00+00:00",
    })
    assert r1.status_code == 200
    r2 = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 777,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.10,
            "entry_price": 4865.0, "sl": 4855.0, "tp": 4880.0,
        },
    })
    assert r2.status_code == 200
    row = conn.execute("SELECT status, executed_at FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    assert row["executed_at"] is not None
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=777").fetchone()
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
