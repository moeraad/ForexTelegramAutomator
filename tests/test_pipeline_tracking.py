"""Tests for pipeline-decision tracking on the messages table.

Verifies that process_message writes the correct (decided_stage,
decided_outcome) tuple at each terminal stage exit, and that the
Pipeline API endpoints surface those rows correctly.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src import trigger_matcher
from src.ai import AICallResult
from src.ai_triage import TriageResult
from src.db import connect, init_schema
from src.orchestrator import process_message
from src.validators import AIResponse, OpenAction


def _ai(actions, category=None):
    client = MagicMock()
    client.call.return_value = AICallResult(
        response=AIResponse(actions=actions, reasoning="", category=category),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )
    return client


def _triage(decision: str):
    tri = MagicMock()
    tri.classify.return_value = TriageResult(
        decision=decision, raw_text=f'{{"decision":"{decision}"}}',
        usage={"input_tokens": 40, "output_tokens": 3,
               "cache_read_tokens": 0, "cache_creation_tokens": 40},
        latency_ms=12,
    )
    return tri


def _stage(conn, msg_text: str) -> tuple[str | None, str | None]:
    row = conn.execute(
        "SELECT decided_stage, decided_outcome FROM messages WHERE text=?",
        (msg_text,),
    ).fetchone()
    assert row is not None, f"no message row for {msg_text!r}"
    return row["decided_stage"], row["decided_outcome"]


def test_interpreted_signal_records_signal_bucket(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _ai([OpenAction(symbol="XAUUSD", side="BUY",
                          entry_low=4864, entry_high=4866,
                          tps=[4880], sl=4855)])
    process_message(
        conn, ai, tg_message_id=1, chat_id=42,
        sender="Y", text="BUY GOLD now",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
    )
    stage, outcome = _stage(conn, "BUY GOLD now")
    assert stage == "interpreted_signal"
    assert "OPEN" in (outcome or "")


def test_interpreted_ignore_records_ignore_bucket(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _ai([], category="ignore")
    process_message(
        conn, ai, tg_message_id=2, chat_id=42,
        sender="Y", text="generic chit-chat",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
    )
    stage, outcome = _stage(conn, "generic chit-chat")
    assert stage == "interpreted_ignore"
    assert outcome == "ignore"


def test_triage_ignored_short_circuits_to_triage_bucket(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _ai([])  # would emit nothing — but triage should fire first
    tri = _triage("ignore")
    process_message(
        conn, ai, tg_message_id=3, chat_id=42,
        sender="Y", text="ads ads ads",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        triage=tri,
    )
    stage, outcome = _stage(conn, "ads ads ads")
    assert stage == "triage_ignored"
    assert outcome == "ignore"
    # Crucially, the expensive interpreter was NOT called.
    assert ai.call.call_count == 0


def test_deterministic_ignore_records_trigger_ignore_bucket(
    tmp_path, monkeypatch,
):
    """A message that equals a curated IGNORE sample is dropped at the
    trigger stage — before triage AND before the interpreter."""
    profile = tmp_path / "profile.json"
    profile.write_text(json.dumps({
        "symbol": "XAUUSD",
        "triggers": [{
            "phrase": "thanks",
            "action_type": "IGNORE",
            "samples": ["thanks for the trust"],
            "context_tokens": [],
        }],
    }), encoding="utf-8")
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    trigger_matcher._cache = None

    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _ai([])           # interpreter must NOT be reached
    tri = _triage("keep")  # triage must NOT be reached either
    process_message(
        conn, ai, tg_message_id=99, chat_id=42,
        sender="Y", text="Thanks for the trust! 🙏",  # same text, decorated
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        triage=tri,
    )
    stage, outcome = _stage(conn, "Thanks for the trust! 🙏")
    assert stage == "trigger_ignore"
    assert outcome == "curated-noise"
    assert ai.call.call_count == 0
    assert tri.classify.call_count == 0
    trigger_matcher._cache = None


def test_pipeline_meta_json_carries_triage_telemetry(tmp_path):
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    ai = _ai([])
    process_message(
        conn, ai, tg_message_id=4, chat_id=42,
        sender="Y", text="more noise",
        ai_log_path=tmp_path / "ai.jsonl",
        auto_execute_delay_sec=30,
        triage=_triage("ignore"),
    )
    row = conn.execute(
        "SELECT pipeline_meta_json FROM messages WHERE text=?",
        ("more noise",),
    ).fetchone()
    meta = json.loads(row["pipeline_meta_json"])
    assert "latency_ms" in meta
    assert "raw_response" in meta


def test_migration_is_idempotent(tmp_path):
    """Re-running init_schema on an existing DB must not raise or
    duplicate columns. Operators upgrading mid-run hit this path on
    every service restart."""
    conn = connect(str(tmp_path / "o.db"))
    init_schema(conn)
    init_schema(conn)  # second pass must be a no-op
    cols = {r["name"] for r in conn.execute(
        "PRAGMA table_info(messages)"
    ).fetchall()}
    assert {"decided_stage", "decided_outcome",
            "decided_at", "pipeline_meta_json"}.issubset(cols)


# ---- API endpoint tests --------------------------------------------


def _seed(db_path):
    conn = connect(str(db_path))
    init_schema(conn)
    # Seed three messages, each at a different bucket. received_at
    # defaults to CURRENT_TIMESTAMP so all fall in the 24h window.
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text, "
        "decided_stage, decided_outcome, decided_at) "
        "VALUES(?,?,?,?,?,?,datetime('now'))",
        (1, 42, "Y", "msg-A", "interpreted_signal", "OPEN"),
    )
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text, "
        "decided_stage, decided_outcome, decided_at) "
        "VALUES(?,?,?,?,?,?,datetime('now'))",
        (2, 42, "Y", "msg-B", "triage_ignored", "ignore"),
    )
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, sender, text, "
        "decided_stage, decided_outcome, decided_at) "
        "VALUES(?,?,?,?,?,?,datetime('now'))",
        (3, 42, "Y", "msg-C", "prefilter_drop", "symbol-mismatch"),
    )
    conn.commit()
    return conn


def test_api_pipeline_stats_returns_per_bucket_counts(tmp_path):
    db = tmp_path / "api.db"
    conn = _seed(db)
    from src.api import build_app
    client = TestClient(build_app(conn))
    r = client.get("/pipeline/stats?since_hours=24")
    assert r.status_code == 200
    counts = r.json()["counts"]
    assert counts["interpreted_signal"] == 1
    assert counts["triage_ignored"] == 1
    assert counts["prefilter_drop"] == 1
    assert counts["interpreted_ignore"] == 0


def test_api_pipeline_messages_filter_by_stage(tmp_path):
    db = tmp_path / "api.db"
    conn = _seed(db)
    from src.api import build_app
    client = TestClient(build_app(conn))
    r = client.get("/pipeline/messages?since_hours=24&stage=triage_ignored")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 1
    assert msgs[0]["text"] == "msg-B"
    assert msgs[0]["decided_stage"] == "triage_ignored"


def test_api_pipeline_messages_no_filter_returns_all(tmp_path):
    db = tmp_path / "api.db"
    conn = _seed(db)
    from src.api import build_app
    client = TestClient(build_app(conn))
    r = client.get("/pipeline/messages?since_hours=24")
    assert r.status_code == 200
    msgs = r.json()["messages"]
    assert len(msgs) == 3
