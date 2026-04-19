import json
from unittest.mock import MagicMock
from src.db import connect, init_schema
from src.orchestrator import process_message
from src.ai import AICallResult
from src.validators import AIResponse, OpenAction, AlertAction


def _make_ai(actions, reasoning=""):
    client = MagicMock()
    client.call.return_value = AICallResult(
        response=AIResponse(actions=actions, reasoning=reasoning),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )
    return client


def test_process_message_persists_valid_open(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([OpenAction(symbol="XAUUSD", side="BUY",
                              entry_low=4864, entry_high=4866,
                              tps=[4880], sl=4855)])
    ids = process_message(
        conn, ai, tg_message_id=1, chat_id=42,
        sender="Yusuf", text="BUY GOLD",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
    )
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["status"] == "pending"
    assert row["action_type"] == "OPEN"
    assert row["execute_after"] is not None
    payload = json.loads(row["payload_json"])
    assert payload["entry_low"] == 4864


def test_process_message_alerts_have_no_execute_after(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([AlertAction(level="warning", text="NFP coming")])
    ids = process_message(
        conn, ai, tg_message_id=2, chat_id=42,
        sender="Yusuf", text="careful, NFP",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
    )
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["action_type"] == "ALERT"
    assert row["execute_after"] is None


def test_process_message_skips_duplicate_message(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([])
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 1


def test_process_message_rejects_invalid_action_writes_rejected_row(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    # AI returns a CLOSE on a ticket that doesn't exist
    from src.validators import CloseAction
    ai = _make_ai([CloseAction(mt5_ticket=12345)])
    ids = process_message(conn, ai, 7, 42, "Y", "close", tmp_path / "a.jsonl", 30)
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["status"] == "rejected"
    assert row["execute_after"] is None
