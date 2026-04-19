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

    # 1. Listener processes a Telegram message
    ids = process_message(conn, ai, tg_message_id=1, chat_id=42,
                          sender="Yusuf", text="BUY GOLD",
                          ai_log_path=tmp_path / "ai.jsonl",
                          auto_execute_delay_sec=0)
    assert len(ids) == 1
    aid = ids[0]
    # execute_after is now (delay=0), so promotion should run
    n = promote_due_actions(conn)
    assert n == 1

    # 2. EA polls API
    app = build_app(conn)
    client = TestClient(app)
    r = client.get("/actions?status=sent")
    actions = r.json()["actions"]
    assert len(actions) == 1
    assert actions[0]["id"] == aid

    # 3. EA reports executed
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


def test_kill_switch_blocks_promotion(tmp_path):
    conn = connect(str(tmp_path / "k.db"))
    init_schema(conn)
    from src.db import set_setting
    set_setting(conn, "kill_switch", "on")
    ai = _ai_returning([OpenAction(symbol="XAUUSD", side="BUY",
                                   entry_low=4864, entry_high=4866,
                                   tps=[4880], sl=4855)])
    process_message(conn, ai, 1, 42, "Y", "BUY", tmp_path / "a.jsonl", 0)
    assert promote_due_actions(conn) == 0
    set_setting(conn, "kill_switch", "off")
    assert promote_due_actions(conn) == 1
