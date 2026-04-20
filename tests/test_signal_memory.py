import sqlite3
import time

import pytest

from src import signal_memory
from src.db import init_schema
from src.validators import AIResponse, AlertAction, OpenAction


@pytest.fixture
def conn():
    c = sqlite3.connect(":memory:", isolation_level=None)
    c.row_factory = sqlite3.Row
    init_schema(c)
    c.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text) "
        "VALUES(1, 100, 'alice', 'hello')"
    )
    yield c
    c.close()


def _msg_id(conn, tg_id: int) -> int:
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text) VALUES(?,100,'x','y')",
        (tg_id,),
    )
    row = conn.execute(
        "SELECT id FROM messages WHERE tg_message_id=?", (tg_id,)
    ).fetchone()
    return row["id"]


def test_summarize_open_action():
    resp = AIResponse(
        category="signal",
        actions=[
            OpenAction(
                symbol="XAUUSD", side="SELL",
                entry_low=4794, entry_high=4795,
                tps=[4785, 4765], sl=4808,
            )
        ],
        reasoning="clear",
    )
    s = signal_memory.summarize(resp)
    assert "SELL" in s and "4794-4795" in s and "4808" in s and "4785,4765" in s
    assert s.startswith("signal:")


def test_summarize_alert_action():
    resp = AIResponse(
        category="partial_signal",
        actions=[AlertAction(level="warning", text="[partial] missing SL")],
        reasoning="unsure",
    )
    s = signal_memory.summarize(resp)
    assert s.startswith("partial_signal:")
    assert "missing SL" in s


def test_summarize_no_actions_uses_reasoning():
    resp = AIResponse(
        category="context", actions=[],
        reasoning="Gold is bullish above 4800. Watch the retracement.",
    )
    s = signal_memory.summarize(resp)
    assert s == "context: Gold is bullish above 4800"


def test_record_and_load_active(conn):
    signal_memory.record(conn, 1, "context", "context: bullish bias")
    signal_memory.record(conn, 1, "partial_signal", "partial_signal: missing SL")
    rows = signal_memory.load_active(conn, max_entries=10, max_age_hours=4)
    assert len(rows) == 2
    assert rows[0]["summary"] == "context: bullish bias"
    assert rows[1]["summary"] == "partial_signal: missing SL"


def test_record_ignore_is_noop(conn):
    new_id = signal_memory.record(conn, 1, "ignore", "ignored")
    assert new_id == 0
    assert signal_memory.load_active(conn, 10, 4) == []


def test_record_rejects_unknown_category(conn):
    with pytest.raises(ValueError):
        signal_memory.record(conn, 1, "garbage", "x")


def test_load_active_cap_keeps_newest(conn):
    for i in range(15):
        signal_memory.record(conn, 1, "context", f"entry-{i}")
    rows = signal_memory.load_active(conn, max_entries=5, max_age_hours=4)
    summaries = [r["summary"] for r in rows]
    assert summaries == [f"entry-{i}" for i in range(10, 15)]


def test_load_active_respects_age(conn):
    conn.execute(
        "INSERT INTO signal_memory(message_id, category, summary, created_at) "
        "VALUES(1, 'context', 'stale', datetime('now', '-5 hours'))"
    )
    signal_memory.record(conn, 1, "context", "fresh")
    rows = signal_memory.load_active(conn, max_entries=10, max_age_hours=4)
    assert [r["summary"] for r in rows] == ["fresh"]


def test_render_empty():
    out = signal_memory.render([])
    assert "DELIBERATION MEMORY" in out
    assert "(empty" in out


def test_render_entries(conn):
    signal_memory.record(conn, 1, "context", "context: bullish")
    time.sleep(0.01)
    signal_memory.record(conn, 1, "signal", "signal: SELL XAUUSD 4794")
    entries = signal_memory.load_active(conn, 10, 4)
    out = signal_memory.render(entries)
    assert "context: bullish" in out
    assert "signal: SELL XAUUSD 4794" in out
    assert out.index("context: bullish") < out.index("signal: SELL XAUUSD 4794")


def test_clear_on_open_marks_all_active(conn):
    m1 = _msg_id(conn, 2)
    signal_memory.record(conn, 1, "context", "bias")
    signal_memory.record(conn, m1, "partial_signal", "partial")
    cleared = signal_memory.clear_on_open(conn)
    assert cleared == 2
    assert signal_memory.load_active(conn, 10, 4) == []


def test_clear_on_open_leaves_already_cleared(conn):
    signal_memory.record(conn, 1, "context", "old")
    signal_memory.clear_on_open(conn)
    signal_memory.record(conn, 1, "context", "fresh")
    cleared = signal_memory.clear_on_open(conn)
    assert cleared == 1
    assert signal_memory.load_active(conn, 10, 4) == []
