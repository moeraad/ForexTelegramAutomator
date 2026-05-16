"""Cost-guard halt + auto-resume tests (REVIEW.md Q7).

The cost guard must:
  - halt and tag the halt 'cost_cap' when today's spend exceeds budget*multiplier
  - auto-resume only halts it set itself (kill_switch_reason='cost_cap'),
    leaving operator-set halts (reason='operator') alone
  - emit exactly one 'halted' event and one 'resumed' event per cycle so
    the bot DMs the operator without spamming
"""
import json
from datetime import datetime, timezone

import pytest

from src.cost_guard import check_and_enforce, todays_cost_usd
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "cg.db"))
    init_schema(conn)
    conn.execute("INSERT OR REPLACE INTO settings VALUES('cost_daily_budget_usd','5')")
    conn.execute("INSERT OR REPLACE INTO settings VALUES('cost_cap_multiplier','1.2')")
    return conn


def _write_log(path, *rows):
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _today(): return datetime.now(timezone.utc).date().isoformat()


def test_no_event_when_under_cap(tmp_path):
    conn = _setup(tmp_path)
    log = tmp_path / "ai.jsonl"
    _write_log(log, {"ts": _today() + "T01:00:00+00:00", "cost": 1.0})
    event, _ = check_and_enforce(conn, log)
    assert event == ""


def test_halts_when_over_cap_and_tags_reason(tmp_path):
    """Budget 5 * 1.2 = 6 cap. Today's spend 7 -> halt + reason=cost_cap."""
    conn = _setup(tmp_path)
    log = tmp_path / "ai.jsonl"
    _write_log(log, {"ts": _today() + "T01:00:00+00:00", "cost": 7.0})
    event, reason = check_and_enforce(conn, log)
    assert event == "halted"
    assert "exceeded" in reason
    ks = conn.execute("SELECT value FROM settings WHERE key='kill_switch'").fetchone()
    assert ks["value"] == "on"
    tag = conn.execute(
        "SELECT value FROM settings WHERE key='kill_switch_reason'"
    ).fetchone()
    assert tag["value"] == "cost_cap"


def test_idempotent_no_duplicate_halt_events(tmp_path):
    """Second call while already halted returns '' so the bot DMs only once."""
    conn = _setup(tmp_path)
    log = tmp_path / "ai.jsonl"
    _write_log(log, {"ts": _today() + "T01:00:00+00:00", "cost": 7.0})
    check_and_enforce(conn, log)
    event, _ = check_and_enforce(conn, log)
    assert event == ""


def test_auto_resume_when_spend_drops_below_cap(tmp_path):
    """REVIEW.md Q7 — at UTC midnight today's spend resets to 0,
    cost_guard auto-resumes the halt it set."""
    conn = _setup(tmp_path)
    log = tmp_path / "ai.jsonl"
    _write_log(log, {"ts": _today() + "T01:00:00+00:00", "cost": 7.0})
    check_and_enforce(conn, log)
    # Simulate midnight: empty log (or below-cap spend on the new UTC day).
    _write_log(log)
    event, reason = check_and_enforce(conn, log)
    assert event == "resumed"
    assert "back under cap" in reason
    ks = conn.execute("SELECT value FROM settings WHERE key='kill_switch'").fetchone()
    assert ks["value"] == "off"
    tag = conn.execute(
        "SELECT value FROM settings WHERE key='kill_switch_reason'"
    ).fetchone()
    assert tag is None  # cleared


def test_auto_resume_leaves_operator_halt_alone(tmp_path):
    """Operator-set halts (reason='operator') stay halted even when the
    spend is healthy — only the operator's /resume clears them."""
    conn = _setup(tmp_path)
    log = tmp_path / "ai.jsonl"
    _write_log(log)  # no spend today
    conn.execute(
        "INSERT OR REPLACE INTO settings VALUES('kill_switch','on')"
    )
    conn.execute(
        "INSERT OR REPLACE INTO settings VALUES('kill_switch_reason','operator')"
    )
    event, _ = check_and_enforce(conn, log)
    assert event == ""
    ks = conn.execute("SELECT value FROM settings WHERE key='kill_switch'").fetchone()
    assert ks["value"] == "on"


def test_zero_budget_disables_guard(tmp_path):
    """Budget=0 means no cap — never halt or resume regardless of spend."""
    conn = _setup(tmp_path)
    conn.execute("INSERT OR REPLACE INTO settings VALUES('cost_daily_budget_usd','0')")
    log = tmp_path / "ai.jsonl"
    _write_log(log, {"ts": _today() + "T01:00:00+00:00", "cost": 999.0})
    event, _ = check_and_enforce(conn, log)
    assert event == ""


def test_todays_cost_sums_today_only(tmp_path):
    log = tmp_path / "ai.jsonl"
    _write_log(log,
        {"ts": _today() + "T00:00:00+00:00", "cost": 1.5},
        {"ts": "2024-01-01T00:00:00+00:00", "cost": 99.0},  # yesterday's history
        {"ts": _today() + "T12:00:00+00:00", "cost": 2.0},
    )
    assert todays_cost_usd(log) == pytest.approx(3.5)
