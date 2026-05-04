import json
from datetime import datetime, timezone
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


# ---- Phase 1: position-state plumbing -----------------------------------

def _open_position(conn, ticket: int, *, volume: float = 0.30, sl: float = 4855.0,
                   tp: float = 4890.0, side: str = "BUY", entry: float = 4865.0,
                   payload: str = "{}") -> int:
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'executed')", (payload,)
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, sl, tp, status) "
        "VALUES(?,?, 'XAUUSD', ?, ?, ?, ?, ?, ?, 'open')",
        (aid, ticket, side, volume, volume, entry, sl, tp),
    )
    return aid


def test_post_result_populates_original_volume(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'sent')"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 4001,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.08,
            "entry_price": 4700.0, "sl": 4690.0, "tp": 4720.0,
        },
    })
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT volume, original_volume, partial_close_count, sl_moved_at "
        "FROM positions WHERE mt5_ticket=4001"
    ).fetchone()
    assert pos["volume"] == 0.08
    assert pos["original_volume"] == 0.08
    assert pos["partial_close_count"] == 0
    assert pos["sl_moved_at"] is None


def test_update_position_increments_partial_close_count_on_volume_decrease(tmp_path):
    conn = _setup(tmp_path)
    _open_position(conn, 4002, volume=0.30)
    client = TestClient(build_app(conn))
    r = client.post("/positions/4002/update", json={"volume": 0.20})
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT volume, partial_close_count FROM positions WHERE mt5_ticket=4002"
    ).fetchone()
    assert pos["volume"] == 0.20
    assert pos["partial_close_count"] == 1
    # second partial -> count goes to 2
    r2 = client.post("/positions/4002/update", json={"volume": 0.10})
    assert r2.status_code == 200
    pos = conn.execute(
        "SELECT volume, partial_close_count FROM positions WHERE mt5_ticket=4002"
    ).fetchone()
    assert pos["partial_close_count"] == 2


def test_update_position_no_increment_on_volume_unchanged_or_increased(tmp_path):
    conn = _setup(tmp_path)
    _open_position(conn, 4003, volume=0.10)
    client = TestClient(build_app(conn))
    r = client.post("/positions/4003/update", json={"volume": 0.10})
    assert r.status_code == 200
    r = client.post("/positions/4003/update", json={"volume": 0.15})
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT partial_close_count FROM positions WHERE mt5_ticket=4003"
    ).fetchone()
    assert pos["partial_close_count"] == 0


def test_update_position_sets_sl_moved_at_on_first_change(tmp_path):
    conn = _setup(tmp_path)
    _open_position(conn, 4004, sl=4855.0)
    client = TestClient(build_app(conn))
    r = client.post("/positions/4004/update", json={"sl": 4865.0})
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT sl, sl_moved_at FROM positions WHERE mt5_ticket=4004"
    ).fetchone()
    assert pos["sl"] == 4865.0
    assert pos["sl_moved_at"] is not None


def test_update_position_does_not_overwrite_sl_moved_at(tmp_path):
    conn = _setup(tmp_path)
    _open_position(conn, 4005, sl=4855.0)
    client = TestClient(build_app(conn))
    client.post("/positions/4005/update", json={"sl": 4865.0})
    first = conn.execute(
        "SELECT sl_moved_at FROM positions WHERE mt5_ticket=4005"
    ).fetchone()["sl_moved_at"]
    client.post("/positions/4005/update", json={"sl": 4870.0})
    second = conn.execute(
        "SELECT sl_moved_at FROM positions WHERE mt5_ticket=4005"
    ).fetchone()["sl_moved_at"]
    assert first == second


def test_update_position_no_sl_moved_at_when_sl_unchanged(tmp_path):
    conn = _setup(tmp_path)
    _open_position(conn, 4006, sl=4855.0)
    client = TestClient(build_app(conn))
    r = client.post("/positions/4006/update", json={"sl": 4855.0, "tp": 4900.0})
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT sl_moved_at FROM positions WHERE mt5_ticket=4006"
    ).fetchone()
    assert pos["sl_moved_at"] is None


def test_last_closed_position_returns_most_recent_in_window(tmp_path):
    conn = _setup(tmp_path)
    payload = json.dumps({
        "type": "OPEN", "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4699, "entry_high": 4701,
        "sl": 4690, "tps": [4710, 4720, 4735],
    })
    _open_position(conn, 4007, payload=payload)
    conn.execute(
        "UPDATE positions SET status='closed', closed_at=?, close_reason='tp1_hit', "
        "volume=0.15, partial_close_count=1 WHERE mt5_ticket=4007",
        (datetime.now(timezone.utc).isoformat(),),
    )
    client = TestClient(build_app(conn))
    r = client.get("/positions/last_closed?symbol=XAUUSD&within_hours=24")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"] == 4007
    assert body["side"] == "BUY"
    assert body["volume_at_close"] == 0.15
    assert body["original_volume"] == 0.30
    assert body["partial_close_count"] == 1
    assert body["close_reason"] == "tp1_hit"
    assert body["signal"]["sl"] == 4690
    assert body["signal"]["tps"] == [4710, 4720, 4735]


def test_last_closed_position_404_when_none(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/positions/last_closed?symbol=XAUUSD&within_hours=24")
    assert r.status_code == 404


def test_last_closed_position_404_when_only_open(tmp_path):
    conn = _setup(tmp_path)
    _open_position(conn, 4008)
    client = TestClient(build_app(conn))
    r = client.get("/positions/last_closed?symbol=XAUUSD&within_hours=24")
    assert r.status_code == 404


def test_market_price_post_then_get_round_trip(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/market/price",
                    json={"symbol": "XAUUSD", "bid": 4823.45, "ask": 4823.85})
    assert r.status_code == 200
    assert r.json()["ok"] is True
    g = client.get("/market/price?symbol=XAUUSD")
    assert g.status_code == 200
    body = g.json()
    assert body["bid"] == 4823.45
    assert body["ask"] == 4823.85
    assert body["mid"] == (4823.45 + 4823.85) / 2.0
    assert body["recorded_at"]


def test_market_price_get_404_when_unset(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/market/price?symbol=XAUUSD")
    assert r.status_code == 404


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
