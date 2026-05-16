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


def test_auth_bind_policy_allows_loopback_without_token():
    """Empty token + loopback bind is fine (single-host install)."""
    from src.api import _enforce_auth_bind_policy
    _enforce_auth_bind_policy("127.0.0.1", "")
    _enforce_auth_bind_policy("localhost", "")
    _enforce_auth_bind_policy("::1", "")


def test_auth_bind_policy_allows_non_loopback_with_token():
    """A token covers non-loopback binds — operator opted in."""
    from src.api import _enforce_auth_bind_policy
    _enforce_auth_bind_policy("0.0.0.0", "shared-secret-xyz")


def test_auth_bind_policy_refuses_non_loopback_without_token():
    """REVIEW.md P2 — refuse to start when API would be unauthenticated
    AND bound to a routable interface. The default .env.example ships
    with a blank token; the moment someone exposes the port, the API
    must not silently allow it."""
    import pytest
    from src.api import _enforce_auth_bind_policy
    with pytest.raises(SystemExit):
        _enforce_auth_bind_policy("0.0.0.0", "")
    with pytest.raises(SystemExit):
        _enforce_auth_bind_policy("192.168.1.20", "   ")


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
        "INSERT INTO actions(action_type, payload_json, status, claimed_at) "
        "VALUES('OPEN', '{}', 'claimed', ?)",
        (datetime.now(timezone.utc).isoformat(),),
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


def test_post_result_after_release_returns_409_no_position_insert(tmp_path):
    """REVIEW.md P0 — release_stale_claims has flipped the action back to
    'sent' (NULLing claimed_at) while the EA was still executing. A late
    result POST from that stale executor must be refused so it can't
    overwrite a freshly re-claimed row and cause two broker orders."""
    conn = _setup(tmp_path)
    # Action was claimed, then released by promoter.release_stale_claims
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, claimed_at) "
        "VALUES('OPEN', '{}', 'sent', NULL)"
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 55501,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.10,
            "entry_price": 4700.0, "sl": 4690.0, "tp": 4720.0,
        },
    })
    assert r.status_code == 409
    detail = r.json().get("detail", {})
    assert detail.get("error") == "claim_expired"
    # No position row was created from the rejected POST
    n = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE mt5_ticket=55501"
    ).fetchone()[0]
    assert n == 0
    # Action status is unchanged (still 'sent', ready for re-claim)
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "sent"


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
    # Action is already terminal (executed) — the result-POST guard refuses
    # the duplicate so the closed position is not resurrected.
    assert r.status_code == 409
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
        "INSERT INTO actions(action_type, payload_json, status, claimed_at) "
        "VALUES('OPEN', '{}', 'claimed', ?)",
        (datetime.now(timezone.utc).isoformat(),),
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


def test_update_position_heals_low_original_volume(tmp_path):
    """REVIEW.md P2 — if the original_volume snapshot was stamped at a
    partial fill (e.g. 0.05), a subsequent /update reflecting the broker's
    completed fill (0.10) must lift original_volume so the AI prompt's
    "X of orig" reasoning stays accurate."""
    conn = _setup(tmp_path)
    _open_position(conn, 4099, volume=0.05)
    client = TestClient(build_app(conn))
    r = client.post("/positions/4099/update", json={"volume": 0.10})
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT volume, original_volume, partial_close_count "
        "FROM positions WHERE mt5_ticket=4099"
    ).fetchone()
    assert pos["volume"] == 0.10
    assert pos["original_volume"] == 0.10
    # Volume went up, not down — partial counter must not have moved.
    assert pos["partial_close_count"] == 0


def test_update_position_does_not_lower_original_volume(tmp_path):
    """A partial close lowering `volume` must NOT reduce original_volume."""
    conn = _setup(tmp_path)
    _open_position(conn, 4100, volume=0.10)
    client = TestClient(build_app(conn))
    r = client.post("/positions/4100/update", json={"volume": 0.05})
    assert r.status_code == 200
    pos = conn.execute(
        "SELECT volume, original_volume, partial_close_count "
        "FROM positions WHERE mt5_ticket=4100"
    ).fetchone()
    assert pos["volume"] == 0.05
    assert pos["original_volume"] == 0.10
    assert pos["partial_close_count"] == 1


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


def test_post_market_price_rejects_unknown_symbol(tmp_path):
    """Symbol whitelist (config.SUPPORTED_SYMBOLS) keeps the AI's price
    anchor from being poisoned by stray POSTs (e.g. BTCUSD when the
    system only trades XAUUSD)."""
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/market/price",
                    json={"symbol": "BTCUSD", "bid": 1.0, "ask": 1.0})
    assert r.status_code == 400
    assert "unsupported symbol" in r.json().get("detail", "")
    # And the settings table is unchanged.
    rows = conn.execute(
        "SELECT key FROM settings WHERE key LIKE 'market_BTCUSD_%'"
    ).fetchall()
    assert rows == []


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


# ---- Phase-3 backstop: refuse mt5_close in partial state -----------------


def _setup_partial_position(tmp_path):
    """Set up a position that has been partially closed: original_volume=1.18,
    volume=0.60 (after a partial), partial_close_count=1, status=open. Mirrors
    the failure mode observed on 2026-05-06 ticket 8506648007."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, partial_close_count, "
        "entry_price, sl, tp, status) VALUES(?, 777, 'XAUUSD', 'BUY', "
        "0.60, 1.18, 1, 4673.71, 4673.71, 4720, 'open')",
        (cur.lastrowid,),
    )
    return conn


def test_close_skipped_when_mt5_close_in_partial_state(tmp_path):
    """The buggy EA history scan POSTed mt5_close on every DEAL_ENTRY_OUT --
    including partial closes -- and prematurely flipped the DB row. The
    backstop refuses mt5_close while the position is mid-partial."""
    conn = _setup_partial_position(tmp_path)
    client = TestClient(build_app(conn))

    r = client.post("/positions/777/close", json={"reason": "mt5_close"})
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == 0
    assert body.get("skipped") == "partial_state_mt5_close"

    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=777").fetchone()
    assert pos["status"] == "open"
    assert pos["closed_at"] is None


def test_close_passes_through_mt5_not_found_in_partial_state(tmp_path):
    """The authoritative pass POSTs mt5_not_found after verifying the ticket
    is gone from MT5. That's unambiguous -- pass through even if the row was
    in partial state."""
    conn = _setup_partial_position(tmp_path)
    client = TestClient(build_app(conn))

    r = client.post("/positions/777/close", json={"reason": "mt5_not_found"})
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=777").fetchone()
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "mt5_not_found"


def test_close_passes_through_ai_close_full_in_partial_state(tmp_path):
    """AI-driven CLOSE_FULL on a partially-closed position is the operator's
    explicit intent -- pass through."""
    conn = _setup_partial_position(tmp_path)
    client = TestClient(build_app(conn))

    r = client.post("/positions/777/close", json={"reason": "ai_close_full"})
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=777").fetchone()
    assert pos["status"] == "closed"


def test_close_passes_through_mt5_close_when_no_partials_taken(tmp_path):
    """A position with partial_close_count=0 has volume == original_volume,
    so the suspicious-state guard does not trip. Legacy mt5_close paths on
    untouched positions still work."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, partial_close_count, "
        "entry_price, sl, tp, status) VALUES(?, 888, 'XAUUSD', 'BUY', "
        "1.00, 1.00, 0, 4673.71, 4660, 4720, 'open')",
        (cur.lastrowid,),
    )
    client = TestClient(build_app(conn))

    r = client.post("/positions/888/close", json={"reason": "mt5_close"})
    assert r.status_code == 200
    assert r.json()["updated"] == 1

    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=888").fetchone()
    assert pos["status"] == "closed"


# ---- auth_gate middleware (#10) -----------------------------------------
# When config.EA_SHARED_TOKEN is blank (the default in tests since the
# tests/conftest.py setup doesn't set it), the middleware is a no-op and
# every other test in this file passes. These tests temporarily set the
# token via monkeypatch and assert 401 without header / 200 with.

def test_auth_gate_blocks_missing_header_when_token_set(tmp_path, monkeypatch):
    from src import config
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "test-token-xyz")
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/actions")
    assert r.status_code == 401
    assert "X-EA-Token" in r.json()["error"]


def test_auth_gate_blocks_wrong_header_when_token_set(tmp_path, monkeypatch):
    from src import config
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "test-token-xyz")
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/actions", headers={"X-EA-Token": "wrong"})
    assert r.status_code == 401


def test_auth_gate_allows_correct_header_when_token_set(tmp_path, monkeypatch):
    from src import config
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "test-token-xyz")
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/actions", headers={"X-EA-Token": "test-token-xyz"})
    assert r.status_code == 200


def test_auth_gate_disabled_when_token_blank(tmp_path, monkeypatch):
    """Default dev-mode behavior: no token configured, no auth enforced."""
    from src import config
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/actions")  # no header
    assert r.status_code == 200


# ---- /alerts endpoint (#8) ---------------------------------------------

def test_post_alert_inserts_alert_row(tmp_path):
    """POST /alerts inserts an ALERT row that the bot's notification
    dispatcher will pick up and DM. ALERT rows have no execute_after so
    the promoter ignores them — they go straight to the operator."""
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/alerts",
                    json={"level": "warning", "text": "stage1 giveup ticket=999"})
    assert r.status_code == 200
    aid = r.json()["id"]
    row = conn.execute(
        "SELECT action_type, payload_json, status, source_msg_id, execute_after "
        "FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["action_type"] == "ALERT"
    # ALERTs are notification-only: they go in terminal so the bot's
    # notification_dispatcher (executed-only) DMs them on the next tick.
    # REVIEW.md P1 — previously inserted as 'pending' and silently swallowed.
    assert row["status"] == "executed"
    assert row["source_msg_id"] is None
    assert row["execute_after"] is None  # never auto-promoted
    payload = json.loads(row["payload_json"])
    assert payload["level"] == "warning"
    assert "stage1 giveup" in payload["text"]


def test_post_alert_default_level(tmp_path):
    """`level` defaults to 'warning' if the EA doesn't supply one."""
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/alerts", json={"text": "something happened"})
    assert r.status_code == 200
    aid = r.json()["id"]
    row = conn.execute(
        "SELECT payload_json FROM actions WHERE id=?", (aid,)
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["level"] == "warning"


# ---- /positions/by_ticket/{ticket} (#14 reinforce snapshot) -------------

def test_position_by_ticket_returns_open_with_signal(tmp_path):
    """REINFORCE snapshots the live position's signal payload BEFORE closing
    so the reopen uses the right params. This endpoint is what makes that
    snapshot possible."""
    conn = _setup(tmp_path)
    payload = json.dumps({
        "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4860, "entry_high": 4862,
        "sl": 4850, "tps": [4870, 4880, 4890],
    })
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'executed')",
        (payload,),
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, sl, tp, status) "
        "VALUES(?, 7777, 'XAUUSD', 'BUY', 0.10, 0.10, 4861, 4850, 4890, 'open')",
        (cur.lastrowid,),
    )
    client = TestClient(build_app(conn))
    r = client.get("/positions/by_ticket/7777")
    assert r.status_code == 200
    body = r.json()
    assert body["ticket"] == 7777
    assert body["status"] == "open"
    assert body["signal"]["entry_low"] == 4860
    assert body["signal"]["tps"] == [4870, 4880, 4890]


def test_position_by_ticket_404_when_closed(tmp_path):
    """Closed positions are not returned (use /positions/last_closed for those)."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, closed_at, close_reason) "
        "VALUES(?, 8888, 'XAUUSD', 'BUY', 0.10, 4861, 4850, 4890, "
        "'closed', '2026-04-20T00:00:00+00:00', 'tp')",
        (cur.lastrowid,),
    )
    client = TestClient(build_app(conn))
    r = client.get("/positions/by_ticket/8888")
    assert r.status_code == 404


def test_events_recent_returns_actions_newest_first(tmp_path):
    """LogPanel feed: GET /events/recent returns the last N actions
    newest-first with shape {id,type,status,summary,ea_response,created_at}.
    `summary` comes from telegram_format._payload_summary so the panel and
    bot DMs stay in sync wording-wise.
    """
    conn = _setup(tmp_path)
    # notified_at set on each row: /events/recent filters on
    # notified_at IS NOT NULL so the dashboard only surfaces actions the
    # bot actually DMed to the operator.
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, notified_at) "
        "VALUES('OPEN', ?, 'executed', CURRENT_TIMESTAMP)",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4710, "entry_high": 4712,
                     "tps": [4730, 4750, 4760], "sl": 4704}),),
    )
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, ea_response, notified_at) "
        "VALUES('CLOSE_PARTIAL', ?, 'executed', 'noop_partial_and_be_disabled', CURRENT_TIMESTAMP)",
        (json.dumps({"fraction": 0.5}),),
    )
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, notified_at) "
        "VALUES('ALERT', ?, 'pending', CURRENT_TIMESTAMP)",
        (json.dumps({"level": "warning", "text": "stage1 giveup"}),),
    )
    client = TestClient(build_app(conn))
    r = client.get("/events/recent?limit=10")
    assert r.status_code == 200
    body = r.json()
    events = body["events"]
    assert len(events) == 3
    # Newest-first: ALERT (id=3) then CLOSE_PARTIAL (id=2) then OPEN (id=1).
    assert [e["id"] for e in events] == [3, 2, 1]
    assert events[0]["type"] == "ALERT"
    assert "stage1 giveup" in events[0]["summary"]
    assert events[1]["type"] == "CLOSE_PARTIAL"
    assert events[1]["status"] == "executed"
    assert events[1]["ea_response"] == "noop_partial_and_be_disabled"
    assert events[2]["type"] == "OPEN"
    assert "BUY" in events[2]["summary"]


def test_events_recent_clamps_limit(tmp_path):
    """`limit` is clamped to [1, 200] so a junk value can't return zero
    rows or sweep the whole table."""
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, notified_at) "
        "VALUES('ALERT', '{}', 'pending', CURRENT_TIMESTAMP)"
    )
    client = TestClient(build_app(conn))
    assert client.get("/events/recent?limit=0").json()["events"] != []
    assert len(client.get("/events/recent?limit=10000").json()["events"]) == 1


# ---- realized P&L extension (Step 3 of AI_EVALUATOR_ROADMAP) ----------


def _seed_open_position(conn, ticket: int):
    """Helper: insert one OPEN action + one open position for it."""
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, sl, tp, status) "
        "VALUES(?, ?, 'XAUUSD', 'BUY', 1.0, 1.0, 4700.0, 4690.0, 4720.0, 'open')",
        (cur.lastrowid, ticket),
    )


def test_post_close_records_exit_price_and_pnl(tmp_path):
    """v2 EA POSTs exit_price + realized_pnl alongside reason. The columns
    are persisted for the calibration script."""
    conn = _setup(tmp_path)
    _seed_open_position(conn, ticket=5001)
    client = TestClient(build_app(conn))
    r = client.post("/positions/5001/close", json={
        "reason": "mt5_tp",
        "exit_price": 4720.0,
        "realized_pnl": 1850.0,
    })
    assert r.status_code == 200
    assert r.json() == {"ok": True, "updated": 1}
    row = conn.execute(
        "SELECT status, close_reason, exit_price, realized_pnl "
        "FROM positions WHERE mt5_ticket=?", (5001,),
    ).fetchone()
    assert row["status"] == "closed"
    assert row["close_reason"] == "mt5_tp"
    assert row["exit_price"] == 4720.0
    assert row["realized_pnl"] == 1850.0


def test_post_close_legacy_body_still_works(tmp_path):
    """Old EA builds POST just `reason` — exit_price/realized_pnl stay NULL."""
    conn = _setup(tmp_path)
    _seed_open_position(conn, ticket=5002)
    client = TestClient(build_app(conn))
    r = client.post("/positions/5002/close", json={"reason": "mt5_sl"})
    assert r.status_code == 200
    row = conn.execute(
        "SELECT exit_price, realized_pnl FROM positions WHERE mt5_ticket=?",
        (5002,),
    ).fetchone()
    assert row["exit_price"] is None
    assert row["realized_pnl"] is None


def test_post_position_update_increments_pnl_delta(tmp_path):
    """Each partial close adds its delta to the running realized_pnl.
    Two partials of +$300 and +$150 should leave realized_pnl == $450."""
    conn = _setup(tmp_path)
    _seed_open_position(conn, ticket=5003)
    client = TestClient(build_app(conn))

    r = client.post("/positions/5003/update",
                    json={"volume": 0.6, "realized_pnl_delta": 300.0})
    assert r.status_code == 200
    r = client.post("/positions/5003/update",
                    json={"volume": 0.3, "realized_pnl_delta": 150.0})
    assert r.status_code == 200

    row = conn.execute(
        "SELECT realized_pnl, partial_close_count FROM positions WHERE mt5_ticket=?",
        (5003,),
    ).fetchone()
    assert row["realized_pnl"] == 450.0
    assert row["partial_close_count"] == 2  # both volume drops counted


def test_post_position_update_no_delta_leaves_pnl_null(tmp_path):
    """SL move only — no realized_pnl_delta — leaves realized_pnl NULL."""
    conn = _setup(tmp_path)
    _seed_open_position(conn, ticket=5004)
    client = TestClient(build_app(conn))
    r = client.post("/positions/5004/update", json={"sl": 4695.0})
    assert r.status_code == 200
    row = conn.execute(
        "SELECT realized_pnl FROM positions WHERE mt5_ticket=?", (5004,),
    ).fetchone()
    assert row["realized_pnl"] is None


# ---- Phase 4: OPEN_INSTANT / ATTACH_SIGNAL -----------------------------

def test_post_result_naked_sets_is_naked(tmp_path):
    """A snapshot with is_naked=true marks the position row naked +
    stamps naked_opened_at."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, claimed_at) "
        "VALUES('OPEN_INSTANT', '{}', 'claimed', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 77001,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.30,
            "entry_price": 4690.0, "sl": 4680.0, "tp": 0.0,
            "is_naked": True,
        },
    })
    assert r.status_code == 200
    row = conn.execute(
        "SELECT is_naked, naked_opened_at FROM positions WHERE mt5_ticket=77001"
    ).fetchone()
    assert row["is_naked"] == 1
    assert row["naked_opened_at"] is not None


def test_attach_signal_clears_naked_and_sets_sl_tp(tmp_path):
    """ATTACH_SIGNAL endpoint clears is_naked, updates sl/tp, stamps
    sl_moved_at on first move."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN_INSTANT', '{}', 'executed')"
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, is_naked, naked_opened_at) "
        "VALUES(?, 77002, 'XAUUSD', 'BUY', 0.30, 4690, 4680, 0, 'open', 1, ?)",
        (aid, datetime.now(timezone.utc).isoformat()),
    )
    client = TestClient(build_app(conn))
    r = client.post("/positions/77002/attach_signal", json={
        "sl": 4685.0,
        "tp": 4720.0,
    })
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["was_naked"] is True
    row = conn.execute(
        "SELECT is_naked, naked_opened_at, sl, tp, sl_moved_at "
        "FROM positions WHERE mt5_ticket=77002"
    ).fetchone()
    assert row["is_naked"] == 0
    assert row["naked_opened_at"] is None
    assert row["sl"] == 4685.0
    assert row["tp"] == 4720.0
    assert row["sl_moved_at"] is not None


def test_attach_signal_404_when_ticket_unknown(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/positions/99999/attach_signal",
                    json={"sl": 4685.0, "tp": 4720.0})
    assert r.status_code == 404


def test_attach_signal_409_when_already_closed(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN_INSTANT', '{}', 'executed')"
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(?, 77003, 'XAUUSD', 'BUY', 0.30, 4690, 4680, 0, 'closed')",
        (aid,),
    )
    client = TestClient(build_app(conn))
    r = client.post("/positions/77003/attach_signal",
                    json={"sl": 4685.0, "tp": 4720.0})
    assert r.status_code == 409


# ---- naked-lineage walk-forward: /positions/last_closed + /by_ticket --

def _seed_naked_lineage(conn, ticket: int, side: str = "BUY") -> tuple[int, int]:
    """Insert OPEN_INSTANT action + matching naked position + the
    ATTACH_SIGNAL action that wired the signal in. Returns
    (open_instant_id, attach_signal_id). Mirrors the production sequence:
    OPEN_INSTANT fires first, position row gets is_naked=1, then
    ATTACH_SIGNAL runs 1-2 min later with full entry/SL/TPs.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, created_at) "
        "VALUES('OPEN_INSTANT', ?, 'executed', ?)",
        (json.dumps({"symbol": "XAUUSD", "side": side, "comment": "directional"}),
         now_iso),
    )
    open_id = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, sl, tp, status, is_naked, naked_opened_at, "
        "opened_at) "
        "VALUES(?,?, 'XAUUSD', ?, 1.00, 1.00, 4700.0, 4690.0, 0.0, 'open', 1, ?, ?)",
        (open_id, ticket, side, now_iso, now_iso),
    )
    cur2 = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, created_at) "
        "VALUES('ATTACH_SIGNAL', ?, 'executed', ?)",
        (json.dumps({
            "symbol": "XAUUSD", "side": side,
            "entry_low": 4699.0, "entry_high": 4701.0,
            "sl": 4694.0, "tps": [4710.0, 4720.0, 4730.0],
            "comment": "FXENGIN",
        }), now_iso),
    )
    attach_id = cur2.lastrowid
    conn.execute(
        "UPDATE positions SET is_naked=0, naked_opened_at=NULL, sl=4694.0, tp=4730.0 "
        "WHERE mt5_ticket=?", (ticket,),
    )
    return open_id, attach_id


def test_last_closed_walks_forward_to_attach_signal_for_naked_lineage(tmp_path):
    """Position opened via OPEN_INSTANT then ATTACH_SIGNAL — the joined
    action payload (OPEN_INSTANT) lacks entry/SL/TPs, so the endpoint
    must walk forward to the ATTACH_SIGNAL action and surface those
    fields in the `signal` block. Without this, the EA's
    BuildOpenPayloadFromLastClosed fails with last_closed_unparseable."""
    conn = _setup(tmp_path)
    _seed_naked_lineage(conn, 88001)
    conn.execute(
        "UPDATE positions SET status='closed', closed_at=?, close_reason='mt5_sl', "
        "volume=0.50 WHERE mt5_ticket=88001",
        (datetime.now(timezone.utc).isoformat(),),
    )
    client = TestClient(build_app(conn))
    r = client.get("/positions/last_closed?symbol=XAUUSD&within_hours=24")
    assert r.status_code == 200
    body = r.json()
    sig = body["signal"]
    assert sig is not None
    assert sig["entry_low"] == 4699.0
    assert sig["entry_high"] == 4701.0
    assert sig["sl"] == 4694.0
    assert sig["tps"] == [4710.0, 4720.0, 4730.0]
    assert sig["side"] == "BUY"


def test_by_ticket_walks_forward_to_attach_signal_for_naked_lineage(tmp_path):
    """REINFORCE path: GET /positions/by_ticket/{n} on a live naked-lineage
    position must also surface the ATTACH_SIGNAL params."""
    conn = _setup(tmp_path)
    _seed_naked_lineage(conn, 88002)
    client = TestClient(build_app(conn))
    r = client.get("/positions/by_ticket/88002")
    assert r.status_code == 200
    body = r.json()
    sig = body["signal"]
    assert sig is not None
    assert sig["entry_low"] == 4699.0
    assert sig["entry_high"] == 4701.0
    assert sig["sl"] == 4694.0
    assert sig["tps"] == [4710.0, 4720.0, 4730.0]


def test_last_closed_legacy_open_lineage_unchanged(tmp_path):
    """Regression: plain OPEN lineage must still resolve directly from
    the joined action without walking to ATTACH_SIGNAL."""
    conn = _setup(tmp_path)
    payload = json.dumps({
        "type": "OPEN", "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4699, "entry_high": 4701,
        "sl": 4690, "tps": [4710, 4720, 4735],
    })
    _open_position(conn, 88003, payload=payload)
    conn.execute(
        "UPDATE positions SET status='closed', closed_at=?, close_reason='mt5_sl' "
        "WHERE mt5_ticket=88003",
        (datetime.now(timezone.utc).isoformat(),),
    )
    client = TestClient(build_app(conn))
    r = client.get("/positions/last_closed?symbol=XAUUSD&within_hours=24")
    assert r.status_code == 200
    sig = r.json()["signal"]
    assert sig["sl"] == 4690
    assert sig["tps"] == [4710, 4720, 4735]


def test_last_closed_no_attach_signal_falls_back_to_sparse_payload(tmp_path):
    """Defensive: naked position whose ATTACH_SIGNAL never fired (orphan
    from before the bugfix) — the endpoint shouldn't 500. It returns the
    sparse OPEN_INSTANT payload so the EA can decide cleanly."""
    conn = _setup(tmp_path)
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, created_at) "
        "VALUES('OPEN_INSTANT', ?, 'executed', ?)",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY", "comment": "x"}), now_iso),
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, status, opened_at, closed_at, close_reason) "
        "VALUES(?, 88004, 'XAUUSD', 'BUY', 1.0, 4700, 'closed', ?, ?, 'mt5_not_found')",
        (aid, now_iso, now_iso),
    )
    client = TestClient(build_app(conn))
    r = client.get("/positions/last_closed?symbol=XAUUSD&within_hours=24")
    assert r.status_code == 200
    sig = r.json()["signal"]
    assert sig is not None
    assert "entry_low" not in sig
    assert sig["side"] == "BUY"


# ---- Phase 5: GET /actions/{id} + ResultBody.status=watching ----------

def test_get_action_returns_row_json(tmp_path):
    """EA's ManagePendingOrders() polls this to detect server-side
    cancellation of a pending limit (watching -> rejected). Returns
    enough to inspect status + ea_response without fetching the
    whole table."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'watching')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4666, "entry_high": 4666,
                     "tps": [4690], "sl": 4662, "pending": True}),),
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.get(f"/actions/{aid}")
    assert r.status_code == 200
    body = r.json()
    assert body["id"] == aid
    assert body["action_type"] == "OPEN"
    assert body["status"] == "watching"
    assert body["payload"]["pending"] is True


def test_get_action_404_for_unknown(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    assert client.get("/actions/99999").status_code == 404


def test_post_result_accepts_watching_status(tmp_path):
    """EA POSTs status='watching' after placing a broker BuyLimit. The
    action transitions to watching without a position row."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'claimed')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4666, "entry_high": 4666,
                     "tps": [4690], "sl": 4662, "pending": True}),),
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "watching",
        "error": "pending_order_ticket=987654321",
    })
    assert r.status_code == 200
    row = conn.execute(
        "SELECT status, ea_response FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "watching"
    assert "pending_order_ticket=987654321" in row["ea_response"]
    n = conn.execute("SELECT COUNT(*) FROM positions").fetchone()[0]
    assert n == 0


def test_post_result_watching_then_executed_creates_position(tmp_path):
    """Limit fires later -> EA POSTs status='executed' with the position
    snapshot. The action transitions watching->executed and the position
    row gets created."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'watching')",
        (json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4666, "entry_high": 4666,
                     "tps": [4690], "sl": 4662, "pending": True}),),
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 987654321,
        "snapshot": {
            "symbol": "XAUUSD", "side": "BUY", "volume": 0.5,
            "entry_price": 4666.0, "sl": 4662.0, "tp": 4690.0,
        },
    })
    assert r.status_code == 200
    row = conn.execute(
        "SELECT status FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "executed"
    pos = conn.execute(
        "SELECT mt5_ticket, entry_price, sl, tp FROM positions "
        "WHERE mt5_ticket=987654321"
    ).fetchone()
    assert pos is not None
    assert pos["entry_price"] == 4666.0
    assert pos["sl"] == 4662.0
    assert pos["tp"] == 4690.0
