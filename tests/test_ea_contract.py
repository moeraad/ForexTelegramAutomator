"""EA wire-contract tests (Day-5 cleanup).

``tests/test_api.py`` covers most endpoints with focused unit tests.
This module fills two gaps:

  1. Endpoints that test_api.py undercovers — ``/health`` and
     ``/market/snapshot`` (the latter only had ``/market/price``
     coverage despite being the richer EA payload).
  2. A full EA lifecycle test that walks through poll → claim → result
     → update → close in one cohesive flow against TestClient, using
     EA-realistic payloads with the directional-rubric extension
     fields.

If the EA's serialization format drifts, these tests catch the wire
contract break before it reaches a real broker. They DO NOT validate
broker execution — only that the API accepts the EA's request shapes
and persists the documented side effects.
"""
from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from src.api import build_app
from src.db import connect, init_schema


def _client(tmp_path: Path) -> tuple[TestClient, "sqlite3.Connection"]:
    """Return (TestClient, conn) for an isolated DB. No auth gate."""
    import sqlite3  # noqa: F401  — imported for the typing hint above
    conn = connect(str(tmp_path / "ea_contract.db"))
    init_schema(conn)
    app = build_app(conn)
    return TestClient(app), conn


def _seed_sent_action(conn, *, action_type: str = "OPEN") -> int:
    """Insert an action in 'sent' state — what the EA polls and claims."""
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES (?, ?, 'sent')",
        (action_type,
         json.dumps({"symbol": "XAUUSD", "side": "BUY",
                     "entry_low": 4000.0, "entry_high": 4002.0,
                     "sl": 3990.0, "tps": [4010.0]})),
    )
    return cur.lastrowid


# ---- Single-endpoint coverage gaps ----------------------------------------


def test_health_returns_ok(tmp_path: Path) -> None:
    """The EA pings /health on startup as a connectivity check."""
    client, _ = _client(tmp_path)
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"ok": True}


def test_market_snapshot_minimal_payload(tmp_path: Path) -> None:
    """Older EA builds POST only the m15/h1/h4 blocks (no directional
    extensions). The endpoint must accept that shape."""
    client, conn = _client(tmp_path)
    tf = {"open": 4000.0, "high": 4010.0, "low": 3995.0,
          "close": 4005.0, "atr14": 15.0}
    r = client.post("/market/snapshot", json={
        "symbol": "XAUUSD",
        "m15": tf, "h1": tf, "h4": tf,
    })
    assert r.status_code == 200, r.text
    # Persisted in settings as JSON.
    row = conn.execute(
        "SELECT value FROM settings WHERE key='market_snapshot_XAUUSD'"
    ).fetchone()
    assert row is not None
    persisted = json.loads(row["value"])
    assert "m15" in persisted and "h1" in persisted and "h4" in persisted
    # Optional fields absent → not persisted.
    assert "d1" not in persisted
    assert "adr20" not in persisted


def test_market_snapshot_full_directional_rubric_payload(tmp_path: Path) -> None:
    """Newer EA builds POST the directional-rubric extensions. The
    endpoint MUST persist them so the evaluator can read them."""
    client, conn = _client(tmp_path)
    tf = {"open": 4000.0, "high": 4010.0, "low": 3995.0,
          "close": 4005.0, "atr14": 15.0}
    d1 = {"open": 3990.0, "high": 4020.0, "low": 3985.0,
          "close": 4005.0, "atr14": 30.0,
          "sma50": 4001.0, "sma200": 3950.0}
    r = client.post("/market/snapshot", json={
        "symbol": "XAUUSD",
        "m15": tf, "h1": tf, "h4": tf,
        "d1": d1, "d1_prev": d1,
        "adr20": 28.5,
        "adx_h1": 22.0,
        "h1_recent_closes": [4001.0, 4003.0, 4004.0, 4005.0, 4006.0],
    })
    assert r.status_code == 200, r.text
    row = conn.execute(
        "SELECT value FROM settings WHERE key='market_snapshot_XAUUSD'"
    ).fetchone()
    persisted = json.loads(row["value"])
    assert persisted["d1"]["sma50"] == 4001.0
    assert persisted["adr20"] == 28.5
    assert persisted["adx_h1"] == 22.0
    assert persisted["h1_recent_closes"] == [4001.0, 4003.0, 4004.0, 4005.0, 4006.0]


def test_market_snapshot_rejects_unknown_symbol(tmp_path: Path) -> None:
    client, _ = _client(tmp_path)
    tf = {"open": 1.0, "high": 1.0, "low": 1.0, "close": 1.0, "atr14": 0.1}
    r = client.post("/market/snapshot", json={
        "symbol": "BOGUSPAIR",
        "m15": tf, "h1": tf, "h4": tf,
    })
    assert r.status_code == 400


# ---- Full EA lifecycle ----------------------------------------------------


def test_full_ea_lifecycle_realistic_payloads(tmp_path: Path) -> None:
    """Walk a single action through the entire EA contract:

        1. EA polls GET /actions?status=sent
        2. EA claims (POST /actions/{id}/claim)
        3. EA executes on broker, POSTs result with snapshot
        4. EA receives a partial-close event, POSTs /positions/{ticket}/update
        5. EA closes the position fully, POSTs /positions/{ticket}/close

    Each step uses payloads that match the EA's serialization format.
    Side-effects on the DB are verified at each step.
    """
    client, conn = _client(tmp_path)
    action_id = _seed_sent_action(conn)

    # ---- Step 1: poll ----
    r = client.get("/actions?status=sent")
    assert r.status_code == 200
    sent_actions = r.json()["actions"]
    assert any(a["id"] == action_id for a in sent_actions)

    # ---- Step 2: claim ----
    r = client.post(f"/actions/{action_id}/claim")
    assert r.status_code == 200
    row = conn.execute(
        "SELECT status, claimed_at FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "claimed"
    assert row["claimed_at"] is not None

    # ---- Step 3: result with broker snapshot ----
    ticket = 999111
    snapshot = {
        "mt5_ticket": ticket,
        "symbol": "XAUUSD",
        "side": "BUY",
        "volume": 0.10,
        "entry_price": 4001.5,
        "sl": 3990.0,
        "tp": 4010.0,
    }
    r = client.post(f"/actions/{action_id}/result", json={
        "status": "executed",
        "mt5_ticket": ticket,
        "snapshot": snapshot,
    })
    assert r.status_code == 200, r.text
    row = conn.execute(
        "SELECT status FROM actions WHERE id=?", (action_id,)
    ).fetchone()
    assert row["status"] == "executed"
    pos = conn.execute(
        "SELECT mt5_ticket, symbol, side, volume, original_volume, "
        "       entry_price, sl, tp, status "
        "FROM positions WHERE mt5_ticket=?",
        (ticket,),
    ).fetchone()
    assert pos["status"] == "open"
    assert pos["entry_price"] == 4001.5
    # original_volume snapshot taken at insert — never updated.
    assert pos["original_volume"] == 0.10

    # ---- Step 4: partial close (vol drop, realized_pnl delta) ----
    r = client.post(f"/positions/{ticket}/update", json={
        "volume": 0.05,
        "sl": 4001.5,  # SL moved to break-even
        "realized_pnl_delta": 25.0,
    })
    assert r.status_code == 200, r.text
    pos = conn.execute(
        "SELECT volume, partial_close_count, sl, sl_moved_at, realized_pnl "
        "FROM positions WHERE mt5_ticket=?",
        (ticket,),
    ).fetchone()
    assert pos["volume"] == 0.05
    assert pos["partial_close_count"] == 1  # bumped on volume decrease
    assert pos["sl"] == 4001.5
    assert pos["sl_moved_at"] is not None
    assert pos["realized_pnl"] == 25.0

    # ---- Step 5: full close ----
    r = client.post(f"/positions/{ticket}/close", json={
        "reason": "ai_close_full",
        "exit_price": 4008.0,
        "realized_pnl": 65.0,
    })
    assert r.status_code == 200, r.text
    pos = conn.execute(
        "SELECT status, closed_at, close_reason, exit_price, realized_pnl "
        "FROM positions WHERE mt5_ticket=?",
        (ticket,),
    ).fetchone()
    assert pos["status"] == "closed"
    assert pos["closed_at"] is not None
    assert pos["close_reason"] == "ai_close_full"
    assert pos["exit_price"] == 4008.0
    assert pos["realized_pnl"] == 65.0


def test_market_price_heartbeat_full_round_trip(tmp_path: Path) -> None:
    """The EA's HeartbeatMarketPrice() POSTs every 15s. AI prompt's
    MARKET block reads via GET /market/price."""
    client, _ = _client(tmp_path)
    r = client.post("/market/price", json={
        "symbol": "XAUUSD", "bid": 4000.50, "ask": 4000.80,
    })
    assert r.status_code == 200, r.text

    r = client.get("/market/price?symbol=XAUUSD")
    assert r.status_code == 200
    body = r.json()
    assert body["bid"] == 4000.50
    assert body["ask"] == 4000.80
    assert body["mid"] == (4000.50 + 4000.80) / 2.0
    assert "recorded_at" in body  # ISO timestamp


def test_alert_lifecycle_creates_action_row(tmp_path: Path) -> None:
    """EA POSTs /alerts when a staged partial gives up after retries.
    The endpoint inserts an ALERT action row (the legacy notification
    dispatcher OR the v2 outbox tailer DMs the operator)."""
    client, conn = _client(tmp_path)
    r = client.post("/alerts", json={
        "level": "warning",
        "text": "stage1 giveup ticket=12345 after 3 attempts; partial abandoned",
    })
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    aid = r.json()["id"]
    row = conn.execute(
        "SELECT action_type, status, payload_json FROM actions WHERE id=?",
        (aid,),
    ).fetchone()
    assert row["action_type"] == "ALERT"
    assert row["status"] == "executed"
    payload = json.loads(row["payload_json"])
    assert payload["level"] == "warning"
    assert "stage1 giveup" in payload["text"]


# ---- Reconciliation contract — GET /positions ----------------------------


def test_get_positions_open_returns_only_open_rows(tmp_path: Path) -> None:
    """The EA's ReconcileClosedPositions queries this on every tick to
    discover positions the broker doesn't know about. The contract:
    only `status='open'` rows are returned."""
    client, conn = _client(tmp_path)
    # Insert one OPEN and one CLOSED position.
    aid = _seed_sent_action(conn)
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "  status) VALUES (?, 1, 'XAUUSD', 'BUY', 0.10, 'open')",
        (aid,),
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "  status, closed_at) "
        "VALUES (?, 2, 'XAUUSD', 'BUY', 0.10, 'closed', "
        "        '2026-05-01T12:00:00+00:00')",
        (aid,),
    )
    r = client.get("/positions?status=open")
    assert r.status_code == 200
    positions = r.json()["positions"]
    tickets = [p["mt5_ticket"] for p in positions]
    assert 1 in tickets
    assert 2 not in tickets


def test_get_positions_no_status_returns_all(tmp_path: Path) -> None:
    client, conn = _client(tmp_path)
    aid = _seed_sent_action(conn)
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "  status) VALUES (?, 1, 'XAUUSD', 'BUY', 0.10, 'open')",
        (aid,),
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "  status, closed_at) "
        "VALUES (?, 2, 'XAUUSD', 'BUY', 0.10, 'closed', "
        "        '2026-05-01T12:00:00+00:00')",
        (aid,),
    )
    r = client.get("/positions")
    assert r.status_code == 200
    positions = r.json()["positions"]
    tickets = sorted(p["mt5_ticket"] for p in positions)
    assert tickets == [1, 2]
