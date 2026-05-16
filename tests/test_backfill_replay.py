from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from src.ai import AICallResult
from src.db import connect, init_schema
from src.listener import MissedMessage, replay_missed_messages
from src.validators import AIResponse, AlertAction


def _make_ai():
    client = MagicMock()
    client.call.return_value = AICallResult(
        response=AIResponse(
            actions=[AlertAction(level="info", text="noted")],
            reasoning="",
        ),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )
    return client


def _setup(tmp_path):
    conn = connect(str(tmp_path / "b.db"))
    init_schema(conn)
    return conn


def test_replay_empty_is_noop(tmp_path):
    conn = _setup(tmp_path)
    ai = _make_ai()
    processed, skipped = replay_missed_messages(
        conn, ai, [], chat_id=42,
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        cap_minutes=30,
        now=datetime.now(timezone.utc),
    )
    assert (processed, skipped) == (0, 0)
    ai.call.assert_not_called()


def test_replay_within_cap_invokes_ai_and_marks_backfill(tmp_path):
    conn = _setup(tmp_path)
    ai = _make_ai()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    msg = MissedMessage(
        tg_message_id=101, sender="Yusuf", text="gold looking strong",
        date=now - timedelta(minutes=5),
    )
    processed, skipped = replay_missed_messages(
        conn, ai, [msg], chat_id=42,
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        cap_minutes=30,
        now=now,
    )
    assert (processed, skipped) == (1, 0)
    ai.call.assert_called_once()
    row = conn.execute(
        "SELECT is_backfill FROM messages WHERE tg_message_id=?", (101,)
    ).fetchone()
    assert row["is_backfill"] == 1


def test_replay_outside_cap_inserts_message_only(tmp_path):
    conn = _setup(tmp_path)
    ai = _make_ai()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    msg = MissedMessage(
        tg_message_id=202, sender="Yusuf", text="stale signal BUY 4865",
        date=now - timedelta(hours=2),
    )
    processed, skipped = replay_missed_messages(
        conn, ai, [msg], chat_id=42,
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        cap_minutes=30,
        now=now,
    )
    assert (processed, skipped) == (0, 1)
    ai.call.assert_not_called()
    row = conn.execute(
        "SELECT is_backfill FROM messages WHERE tg_message_id=?", (202,)
    ).fetchone()
    assert row["is_backfill"] == 1
    actions = conn.execute("SELECT COUNT(*) AS n FROM actions").fetchone()
    assert actions["n"] == 0


def test_replay_sorts_oldest_first(tmp_path):
    conn = _setup(tmp_path)
    ai = _make_ai()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    newer = MissedMessage(tg_message_id=300, sender="Y", text="newer",
                          date=now - timedelta(minutes=2))
    older = MissedMessage(tg_message_id=299, sender="Y", text="older",
                          date=now - timedelta(minutes=10))
    processed, skipped = replay_missed_messages(
        conn, ai, [newer, older], chat_id=42,
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        cap_minutes=30,
        now=now,
    )
    assert (processed, skipped) == (2, 0)
    rows = conn.execute(
        "SELECT tg_message_id FROM messages ORDER BY id ASC"
    ).fetchall()
    assert [r["tg_message_id"] for r in rows] == [299, 300]


def test_replay_mixed_cap_partitions_correctly(tmp_path):
    conn = _setup(tmp_path)
    ai = _make_ai()
    now = datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc)
    fresh = MissedMessage(tg_message_id=401, sender="Y", text="fresh",
                          date=now - timedelta(minutes=1))
    stale = MissedMessage(tg_message_id=402, sender="Y", text="stale",
                          date=now - timedelta(hours=3))
    processed, skipped = replay_missed_messages(
        conn, ai, [fresh, stale], chat_id=42,
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        cap_minutes=30,
        now=now,
    )
    assert (processed, skipped) == (1, 1)
    assert ai.call.call_count == 1


def test_process_message_flags_backfill_on_messages_row(tmp_path):
    from src.orchestrator import process_message
    conn = _setup(tmp_path)
    ai = _make_ai()
    process_message(
        conn, ai, tg_message_id=999, chat_id=42,
        sender="Y", text="hi",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        is_backfill=True,
    )
    row = conn.execute(
        "SELECT is_backfill FROM messages WHERE tg_message_id=?", (999,)
    ).fetchone()
    assert row["is_backfill"] == 1


def test_backfill_management_action_is_parked_for_review(tmp_path):
    """REVIEW.md P1 / Q5 — a REINFORCE arriving via backfill replay must
    NOT auto-fire (it would close+reopen the current position based on a
    stale channel message). Instead it lands `pending` with
    execute_after=NULL and ea_response='backfill_management_review_required'
    so the promoter skips it and the bot's notification dispatcher DMs
    the operator with an Execute/Ignore keyboard."""
    from unittest.mock import MagicMock
    from src.ai import AICallResult
    from src.validators import AIResponse, ReinforceAction
    from src.orchestrator import process_message
    conn = _setup(tmp_path)
    ai = MagicMock()
    ai.call.return_value = AICallResult(
        response=AIResponse(
            actions=[ReinforceAction(side="BUY")],
            reasoning="",
        ),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )
    ids = process_message(
        conn, ai, tg_message_id=500, chat_id=42,
        sender="Yusuf", text="عزز شراء",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        is_backfill=True,
    )
    assert len(ids) == 1
    row = conn.execute(
        "SELECT action_type, status, execute_after, ea_response "
        "FROM actions WHERE id=?", (ids[0],)
    ).fetchone()
    assert row["action_type"] == "REINFORCE"
    assert row["status"] == "pending"
    assert row["execute_after"] is None
    assert row["ea_response"] == "backfill_management_review_required"


def test_live_management_action_auto_executes(tmp_path):
    """The parking rule applies only to backfill. A management action
    arriving on the live path keeps the normal auto-execute flow."""
    from unittest.mock import MagicMock
    from src.ai import AICallResult
    from src.validators import AIResponse, ReinforceAction
    from src.orchestrator import process_message
    conn = _setup(tmp_path)
    ai = MagicMock()
    ai.call.return_value = AICallResult(
        response=AIResponse(
            actions=[ReinforceAction(side="BUY")],
            reasoning="",
        ),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )
    ids = process_message(
        conn, ai, tg_message_id=501, chat_id=42,
        sender="Yusuf", text="عزز شراء",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        is_backfill=False,
    )
    row = conn.execute(
        "SELECT status, execute_after, ea_response FROM actions WHERE id=?",
        (ids[0],),
    ).fetchone()
    assert row["status"] == "pending"
    assert row["execute_after"] is not None
    assert row["ea_response"] is None
