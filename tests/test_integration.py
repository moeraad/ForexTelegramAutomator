import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app
from src.orchestrator import process_message
from src.promoter import promote_due_actions
from src.ai import AICallResult
from src.validators import AIResponse, OpenAction


def _ai_returning(actions):
    client = MagicMock()
    client.call.return_value = AICallResult(
        response=AIResponse(actions=actions, reasoning=""),
        raw_text="{}", usage={"input_tokens":1,"output_tokens":1,
                              "cache_read_tokens":0,"cache_creation_tokens":0},
        latency_ms=1,
    )
    return client


def test_full_pipeline_open_to_executed(tmp_path):
    conn = connect(str(tmp_path / "i.db"))
    init_schema(conn)
    ai = _ai_returning([OpenAction(symbol="XAUUSD", side="BUY",
                                   entry_low=4864, entry_high=4866,
                                   tps=[4880], sl=4855)])

    # 1. Listener processes a Telegram message. Use a positive delay so
    # the action lands in 'pending' (the orchestrator short-circuits
    # delay<=0 actions straight to 'sent', bypassing the promoter — which
    # would skip this stage of the test entirely).
    ids = process_message(conn, ai, tg_message_id=1, chat_id=42,
                          sender="Yusuf", text="BUY GOLD",
                          ai_log_path=tmp_path / "ai.jsonl",
                          auto_execute_delay_sec=30)
    assert len(ids) == 1
    aid = ids[0]
    # Force the promoter to flip pending → sent immediately (the
    # production promoter waits for execute_after, but for the test
    # we want to advance state in this thread).
    from src.db import set_setting
    from datetime import datetime, timezone
    conn.execute(
        "UPDATE actions SET execute_after=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), aid),
    )
    n = promote_due_actions(conn)
    assert n == 1

    # 2. EA polls API
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/actions?status=sent")
    actions = r.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["id"] == aid

    # 3. EA claims first (sent → claimed) — the API rejects result POSTs
    # from non-owning states (`sent` doesn't own the action yet; only
    # `claimed` or `watching` may close it). See api.py post_result.
    r = client.post(f"/actions/{aid}/claim")
    assert r.status_code == 200

    # 4. EA reports executed
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "mt5_ticket": 12345,
        "snapshot": {"symbol":"XAUUSD","side":"BUY","volume":0.10,
                     "entry_price":4865.0,"sl":4855.0,"tp":4880.0},
    })
    assert r.status_code == 200

    # 4. State settles
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=12345").fetchone()
    assert pos["status"] == "open"

    # 5. EA reconciles — position closed in MT5
    r = client.post("/positions/12345/close", json={"reason": "tp"})
    assert r.status_code == 200
    pos = conn.execute("SELECT * FROM positions WHERE mt5_ticket=12345").fetchone()
    assert pos["status"] == "closed"
    assert pos["close_reason"] == "tp"


def test_claim_then_bundled_legs_executes_action(tmp_path):
    """Full claim/execute/confirm protocol: pending -> sent -> claimed -> executed
    with multiple positions inserted from one bundled-legs result."""
    conn = connect(str(tmp_path / "c.db"))
    init_schema(conn)
    ai = _ai_returning([OpenAction(symbol="XAUUSD", side="BUY",
                                   entry_low=4864, entry_high=4866,
                                   tps=[4880, 4890], sl=4855)])
    # Positive delay → 'pending'; otherwise the orchestrator short-
    # circuits to 'sent' and the promoter has nothing to flip.
    ids = process_message(conn, ai, 1, 42, "Y", "BUY", tmp_path / "a.jsonl", 30)
    aid = ids[0]
    from datetime import datetime, timezone
    conn.execute(
        "UPDATE actions SET execute_after=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), aid),
    )
    assert promote_due_actions(conn) == 1

    app = build_app(conn)
    client = TestClient(app)

    # First claim wins
    r1 = client.post(f"/actions/{aid}/claim")
    assert r1.status_code == 200
    # Second claim (simulating duplicate EA tick) loses
    r2 = client.post(f"/actions/{aid}/claim")
    assert r2.status_code == 409

    # EA reports both legs in one body
    r = client.post(f"/actions/{aid}/result", json={
        "status": "executed",
        "legs": [
            {"mt5_ticket": 7001, "snapshot": {
                "symbol": "XAUUSD", "side": "BUY", "volume": 0.05,
                "entry_price": 4865.0, "sl": 4855.0, "tp": 4880.0}},
            {"mt5_ticket": 7002, "snapshot": {
                "symbol": "XAUUSD", "side": "BUY", "volume": 0.05,
                "entry_price": 4865.0, "sl": 4855.0, "tp": 4890.0}},
        ],
    })
    assert r.status_code == 200

    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "executed"
    tickets = [r["mt5_ticket"] for r in conn.execute(
        "SELECT mt5_ticket FROM positions WHERE action_id=? ORDER BY mt5_ticket", (aid,)
    ).fetchall()]
    assert tickets == [7001, 7002]


def test_kill_switch_blocks_promotion(tmp_path):
    conn = connect(str(tmp_path / "k.db"))
    init_schema(conn)
    from src.db import set_setting
    set_setting(conn, "kill_switch", "on")
    ai = _ai_returning([OpenAction(symbol="XAUUSD", side="BUY",
                                   entry_low=4864, entry_high=4866,
                                   tps=[4880], sl=4855)])
    # Positive delay → 'pending' (the path the promoter actually gates
    # via kill_switch). delay=0 would short-circuit to 'sent', defeating
    # the test's purpose.
    ids = process_message(conn, ai, 1, 42, "Y", "BUY", tmp_path / "a.jsonl", 30)
    from datetime import datetime, timezone
    conn.execute(
        "UPDATE actions SET execute_after=? WHERE id=?",
        (datetime.now(timezone.utc).isoformat(), ids[0]),
    )
    assert promote_due_actions(conn) == 0
    set_setting(conn, "kill_switch", "off")
    assert promote_due_actions(conn) == 1
