import json
from unittest.mock import MagicMock
from src.db import connect, init_schema
from src.orchestrator import process_message
from src.ai import AICallResult
from src.ai_triage import TriageResult
from src.validators import AIResponse, OpenAction, AlertAction


def _make_triage(decision: str):
    tri = MagicMock()
    tri.classify.return_value = TriageResult(
        decision=decision, raw_text=f'{{"decision":"{decision}"}}',
        usage={"input_tokens": 40, "output_tokens": 3,
               "cache_read_tokens": 0, "cache_creation_tokens": 40},
        latency_ms=12,
    )
    return tri


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


def test_process_message_skips_duplicate_message_when_action_emitted(tmp_path):
    """Dedup via messages.UNIQUE only protects messages that produced an
    action. Under the action-only-persistence policy, an action-emitting
    message stays in the table, so a redelivered duplicate short-circuits.
    """
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([OpenAction(symbol="XAUUSD", side="BUY",
                              entry_low=4864, entry_high=4866,
                              tps=[4880], sl=4855)])
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 1
    assert ai.call.call_count == 1  # second call short-circuited on dedup


def test_no_action_message_is_not_persisted(tmp_path):
    """Per project policy: only action-producing messages stay in the DB."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([])  # AI returns zero actions
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    n = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert n == 0


def test_no_action_message_repeated_re_processes(tmp_path):
    """Side effect of cleanup: same tg_message_id with no action gets
    processed each time it arrives. Operator should know this trade-off."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([])
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    process_message(conn, ai, 5, 42, "Y", "hi", tmp_path / "a.jsonl", 30)
    assert ai.call.call_count == 2
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_process_message_ai_exception_writes_alert(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = MagicMock()
    ai.call.side_effect = RuntimeError("boom")
    ids = process_message(conn, ai, 9, 42, "Y", "x", tmp_path / "a.jsonl", 30)
    assert len(ids) == 1
    row = conn.execute("SELECT * FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["action_type"] == "ALERT"
    assert row["status"] == "pending"
    assert row["execute_after"] is None
    payload = json.loads(row["payload_json"])
    assert "AI error" in payload["text"]
    assert payload["level"] == "warning"


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


def test_fingerprint_stored_on_valid_open(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([OpenAction(symbol="XAUUSD", side="BUY",
                              entry_low=4864, entry_high=4866,
                              tps=[4880], sl=4855)])
    ids = process_message(conn, ai, 1, 42, "Y", "BUY", tmp_path / "a.jsonl", 30)
    row = conn.execute("SELECT fingerprint FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["fingerprint"] is not None
    assert "XAUUSD|BUY" in row["fingerprint"]


def test_duplicate_open_signal_rejected(tmp_path):
    """Second identical OPEN from a later message collapses onto the first."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    signal = OpenAction(symbol="XAUUSD", side="BUY",
                        entry_low=4864, entry_high=4866,
                        tps=[4880], sl=4855)
    ai = _make_ai([signal])

    ids1 = process_message(conn, ai, 1, 42, "Y", "BUY 4865", tmp_path / "a.jsonl", 30)
    ids2 = process_message(conn, ai, 2, 42, "Y", "re: BUY 4865", tmp_path / "a.jsonl", 30)

    r1 = conn.execute("SELECT status FROM actions WHERE id=?", (ids1[0],)).fetchone()
    r2 = conn.execute("SELECT status, ea_response FROM actions WHERE id=?",
                      (ids2[0],)).fetchone()
    assert r1["status"] == "pending"
    assert r2["status"] == "rejected"
    assert r2["ea_response"] == "duplicate_signal"


def test_near_miss_within_band_also_rejected(tmp_path):
    """A quoted signal with slightly tweaked prices still collapses."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    first = OpenAction(symbol="XAUUSD", side="BUY",
                       entry_low=4864, entry_high=4866,
                       tps=[4880], sl=4855)
    process_message(conn, _make_ai([first]), 1, 42, "Y", "BUY",
                    tmp_path / "a.jsonl", 30)

    near = OpenAction(symbol="XAUUSD", side="BUY",
                      entry_low=4864.5, entry_high=4866.5,
                      tps=[4881], sl=4854.2)
    ids2 = process_message(conn, _make_ai([near]), 2, 42, "Y", "BUY again",
                           tmp_path / "a.jsonl", 30)
    r2 = conn.execute("SELECT status, ea_response FROM actions WHERE id=?",
                      (ids2[0],)).fetchone()
    assert r2["status"] == "rejected"
    assert r2["ea_response"] == "duplicate_signal"


def test_different_signal_not_rejected(tmp_path):
    """A distinct entry zone should not collapse."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866,
                   tps=[4880], sl=4855)
    b = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4900, entry_high=4902,
                   tps=[4920], sl=4890)
    process_message(conn, _make_ai([a]), 1, 42, "Y", "x", tmp_path / "a.jsonl", 30)
    ids2 = process_message(conn, _make_ai([b]), 2, 42, "Y", "y",
                           tmp_path / "a.jsonl", 30)
    r2 = conn.execute("SELECT status FROM actions WHERE id=?", (ids2[0],)).fetchone()
    assert r2["status"] == "pending"


def test_triage_ignore_short_circuits(tmp_path):
    """Triage 'ignore' must skip the Sonnet call and write no actions."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([OpenAction(symbol="XAUUSD", side="BUY",
                              entry_low=4864, entry_high=4866,
                              tps=[4880], sl=4855)])
    tri = _make_triage("ignore")
    ids = process_message(
        conn, ai, tg_message_id=1, chat_id=42,
        sender="Y", text="good morning traders",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        triage=tri,
    )
    assert ids == []
    assert ai.call.call_count == 0
    assert conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM signal_memory").fetchone()[0] == 0
    # Per action-only-persistence policy: triage-ignored messages are not stored.
    assert conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0


def test_triage_keep_proceeds_to_sonnet(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([OpenAction(symbol="XAUUSD", side="BUY",
                              entry_low=4864, entry_high=4866,
                              tps=[4880], sl=4855)])
    tri = _make_triage("keep")
    ids = process_message(
        conn, ai, 1, 42, "Y", "BUY GOLD",
        tmp_path / "ai.jsonl", 30, triage=tri,
    )
    assert ai.call.call_count == 1
    assert len(ids) == 1
    row = conn.execute("SELECT status FROM actions WHERE id=?", (ids[0],)).fetchone()
    assert row["status"] == "pending"


def test_triage_exception_falls_through_to_sonnet(tmp_path):
    """Never drop a message on triage failure — fall through to the full model."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _make_ai([AlertAction(level="warning", text="watch NFP")])
    tri = MagicMock()
    tri.classify.side_effect = RuntimeError("triage down")
    ids = process_message(
        conn, ai, 1, 42, "Y", "careful NFP",
        tmp_path / "ai.jsonl", 30, triage=tri,
    )
    assert ai.call.call_count == 1
    assert len(ids) == 1


def test_duplicate_allowed_after_first_is_cancelled(tmp_path):
    """Once the original is cancelled, a new identical signal is not a duplicate."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    sig = OpenAction(symbol="XAUUSD", side="BUY",
                     entry_low=4864, entry_high=4866,
                     tps=[4880], sl=4855)
    ids1 = process_message(conn, _make_ai([sig]), 1, 42, "Y", "x",
                           tmp_path / "a.jsonl", 30)
    conn.execute("UPDATE actions SET status='cancelled' WHERE id=?", (ids1[0],))

    ids2 = process_message(conn, _make_ai([sig]), 2, 42, "Y", "y",
                           tmp_path / "a.jsonl", 30)
    r2 = conn.execute("SELECT status FROM actions WHERE id=?", (ids2[0],)).fetchone()
    assert r2["status"] == "pending"
