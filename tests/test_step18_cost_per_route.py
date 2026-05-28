"""Cost-per-route tracking tests (Step 18 of multi-channel plan).

Covers:
  - orchestrator passes source_channel_id + route_id into every AI call
    log row via the closure helper
  - ai_costs.CallRecord parses the new fields
  - summarize_by_channel / summarize_by_route bucket records correctly
  - todays_cost_per_route_usd sums today's spend per route_id
  - check_per_route_alerts returns breached routes (and not unbreached)
  - Per-route alerts do NOT touch the global kill_switch
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src.ai import AICallResult, AIClient
from src.cost_guard import check_per_route_alerts, todays_cost_per_route_usd
from src.db import connect, init_schema
from src.gui.services.ai_costs import (
    CallRecord,
    load_records,
    summarize_by_channel,
    summarize_by_route,
)
from src.profile_context import ProfileContext
from src.validators import AIResponse, OpenAction


# ---- orchestrator tags every log_call ------------------------------------


def _make_profile(tmp_path: Path) -> ProfileContext:
    from src.ai import _render_system_prompt_from_data
    from src.ai_triage import _render_triage_prompt_from_data
    data = {
        "header": "H", "vocabulary_table": "v", "compound_messages": "c",
        "commentary_filter": "f", "directional_command_flow": "d",
        "worked_examples": "e", "shorthand_decode_example": "s",
        "promo_indicators": "", "noise_patterns": "", "triage_keep_triggers": "",
        "symbol": "XAUUSD",
    }
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return ProfileContext(
        name="p", path=p, data=data,
        system_prompt=_render_system_prompt_from_data(data),
        triage_prompt=_render_triage_prompt_from_data(data),
        symbol="XAUUSD",
    )


def test_orchestrator_tags_interpret_log_with_channel_and_route(tmp_path: Path):
    """The AI interpret-stage log row carries source_channel_id + route_id
    so cost-per-route analytics can attribute the spend."""
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"

    ai = MagicMock(spec=AIClient)
    ai.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol="XAUUSD", side="BUY",
            entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 100, "output_tokens": 50,
               "cache_read_tokens": 200, "cache_creation_tokens": 0},
        latency_ms=42,
    )

    from src.orchestrator import process_message
    process_message(
        conn, ai,
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
    )

    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    # At least one row should have the tags (the interpret row).
    interpret_rows = [r for r in rows if r.get("stage") == "interpret"]
    assert interpret_rows, "expected at least one interpret stage row"
    for r in interpret_rows:
        assert r["source_channel_id"] == "ch_a"
        assert r["route_id"] == "route_ax"


def test_orchestrator_halt_log_carries_tags(tmp_path: Path):
    """Halt-stage log row also gets tagged (Step 15 already wired it,
    but Step 18's closure shouldn't have regressed that)."""
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"

    from src.orchestrator import process_message
    process_message(
        conn, MagicMock(spec=AIClient),
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
        halted=True,
    )
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    halt_rows = [r for r in rows if r.get("stage") == "halt"]
    assert len(halt_rows) == 1
    assert halt_rows[0]["source_channel_id"] == "ch_a"
    assert halt_rows[0]["route_id"] == "route_ax"


def test_orchestrator_blank_channel_id_does_not_inject_empty(tmp_path: Path):
    """When source_channel_id is empty (legacy/back-compat call), the tag
    should NOT be added to the log row (no spurious '""' field)."""
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"

    from src.orchestrator import process_message
    process_message(
        conn, MagicMock(spec=AIClient),
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        halted=True,
    )
    rows = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
    for r in rows:
        # Closure only adds the key when the value is truthy.
        assert "source_channel_id" not in r or r["source_channel_id"]


# ---- ai_costs.CallRecord parsing -----------------------------------------


def test_load_records_parses_new_tags(tmp_path: Path):
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    log_path.write_text(
        json.dumps({
            "ts": now, "msg_id": 1, "stage": "interpret",
            "input_tokens": 100, "output_tokens": 50,
            "source_channel_id": "ch_a", "route_id": "route_ax",
        }) + "\n",
        encoding="utf-8",
    )
    records = load_records(log_path, days=None)
    assert len(records) == 1
    assert records[0].source_channel_id == "ch_a"
    assert records[0].route_id == "route_ax"


def test_load_records_handles_legacy_rows_without_tags(tmp_path: Path):
    """Pre-Step-18 rows still parse — tags default to empty string."""
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    log_path.write_text(
        json.dumps({
            "ts": now, "msg_id": 1, "stage": "interpret",
            "input_tokens": 100, "output_tokens": 50,
        }) + "\n",
        encoding="utf-8",
    )
    records = load_records(log_path, days=None)
    assert records[0].source_channel_id == ""
    assert records[0].route_id == ""


# ---- summarize_by_channel / summarize_by_route ---------------------------


def _record(
    *, ts: datetime | None = None,
    input_tokens: int = 100, output_tokens: int = 50,
    source_channel_id: str = "", route_id: str = "",
    stage: str = "interpret",
) -> CallRecord:
    return CallRecord(
        ts=ts or datetime.now(timezone.utc),
        msg_id=1, stage=stage, decision=None, latency_ms=10,
        input_tokens=input_tokens, output_tokens=output_tokens,
        cache_read_tokens=0, cache_creation_tokens=0, error=None,
        source_channel_id=source_channel_id, route_id=route_id,
    )


def test_summarize_by_channel_buckets_correctly():
    records = [
        _record(source_channel_id="ch_a", input_tokens=100, output_tokens=50),
        _record(source_channel_id="ch_a", input_tokens=200, output_tokens=100),
        _record(source_channel_id="ch_b", input_tokens=50, output_tokens=25),
    ]
    summary = summarize_by_channel(records)
    assert set(summary.keys()) == {"ch_a", "ch_b"}
    assert summary["ch_a"].calls == 2
    assert summary["ch_a"].input_tokens == 300
    assert summary["ch_b"].calls == 1


def test_summarize_by_route_buckets_correctly():
    records = [
        _record(route_id="route_ax"),
        _record(route_id="route_ay"),
        _record(route_id="route_ax"),
    ]
    summary = summarize_by_route(records)
    assert set(summary.keys()) == {"route_ax", "route_ay"}
    assert summary["route_ax"].calls == 2
    assert summary["route_ay"].calls == 1


def test_summarize_by_channel_unattributed_bucket():
    """Records without a channel tag bucket under '(unattributed)' so
    operators can see cost that's not being attributed."""
    records = [
        _record(source_channel_id="ch_a"),
        _record(source_channel_id=""),
        _record(source_channel_id=""),
    ]
    summary = summarize_by_channel(records)
    assert summary["(unattributed)"].calls == 2
    assert summary["ch_a"].calls == 1


def test_summarize_by_channel_empty_records_returns_empty_dict():
    assert summarize_by_channel([]) == {}


def test_summarize_by_channel_cost_propagates():
    records = [
        _record(source_channel_id="ch_a",
                input_tokens=1_000_000, output_tokens=500_000),
    ]
    summary = summarize_by_channel(records)
    # 1M input @ $3 + 500k output @ $15 = $3 + $7.5 = $10.5
    assert summary["ch_a"].estimated_cost_usd == pytest.approx(10.5, rel=1e-6)


# ---- todays_cost_per_route_usd -------------------------------------------


def _write_log(log_path: Path, rows: list[dict]) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")


def test_todays_cost_per_route_sums_by_route_id(tmp_path: Path):
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_tokens": 0},
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 500_000, "output_tokens": 0,
         "cache_read_tokens": 0},
        {"ts": now, "route_id": "route_ay",
         "input_tokens": 1_000_000, "output_tokens": 0,
         "cache_read_tokens": 0},
    ])
    totals = todays_cost_per_route_usd(log_path)
    # route_ax: 1.5M input × $3/M = $4.5
    # route_ay: 1.0M input × $3/M = $3.0
    assert totals["route_ax"] == pytest.approx(4.5, rel=1e-6)
    assert totals["route_ay"] == pytest.approx(3.0, rel=1e-6)


def test_todays_cost_per_route_skips_yesterday(tmp_path: Path):
    log_path = tmp_path / "ai.jsonl"
    yesterday = "2025-01-01T12:00:00+00:00"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": yesterday, "route_id": "route_ax",
         "input_tokens": 1_000_000, "output_tokens": 0},
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 500_000, "output_tokens": 0},
    ])
    totals = todays_cost_per_route_usd(log_path)
    # Only today's 500k counts → $1.5
    assert totals["route_ax"] == pytest.approx(1.5, rel=1e-6)


def test_todays_cost_per_route_unattributed_bucket(tmp_path: Path):
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": now, "input_tokens": 1_000_000, "output_tokens": 0},
    ])
    totals = todays_cost_per_route_usd(log_path)
    assert "(unattributed)" in totals
    assert totals["(unattributed)"] == pytest.approx(3.0, rel=1e-6)


def test_todays_cost_per_route_returns_empty_when_file_missing(tmp_path: Path):
    assert todays_cost_per_route_usd(tmp_path / "absent.jsonl") == {}


# ---- check_per_route_alerts ----------------------------------------------


def _seed_settings(conn: sqlite3.Connection, budgets: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES "
        "('cost_route_budgets_json', ?)",
        (json.dumps(budgets),),
    )
    conn.commit()


def test_check_per_route_alerts_returns_breached(tmp_path: Path):
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 1_000_000, "output_tokens": 0},
    ])
    _seed_settings(conn, {"route_ax": 1.0, "route_ay": 10.0})
    breaches = check_per_route_alerts(conn, log_path)
    assert len(breaches) == 1
    route_id, spent, budget = breaches[0]
    assert route_id == "route_ax"
    assert spent == pytest.approx(3.0, rel=1e-6)
    assert budget == 1.0


def test_check_per_route_alerts_no_breach_when_under_budget(tmp_path: Path):
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 1_000_000, "output_tokens": 0},
    ])
    _seed_settings(conn, {"route_ax": 10.0})
    assert check_per_route_alerts(conn, log_path) == []


def test_check_per_route_alerts_returns_empty_when_no_budgets(tmp_path: Path):
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"
    # No setting row at all.
    assert check_per_route_alerts(conn, log_path) == []


def test_check_per_route_alerts_does_not_flip_kill_switch(tmp_path: Path):
    """Per-route alerts are informational — global kill_switch is NEVER
    touched. Operators wanting per-route auto-halt layer Route.halted
    (Step 15) via a future automation."""
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 1_000_000, "output_tokens": 0},
    ])
    _seed_settings(conn, {"route_ax": 0.5})
    # kill_switch starts at 'off' from schema defaults.
    before = conn.execute(
        "SELECT value FROM settings WHERE key='kill_switch'"
    ).fetchone()
    assert before[0] == "off"
    breaches = check_per_route_alerts(conn, log_path)
    assert len(breaches) == 1
    after = conn.execute(
        "SELECT value FROM settings WHERE key='kill_switch'"
    ).fetchone()
    assert after[0] == "off"


def test_check_per_route_alerts_ignores_zero_budget(tmp_path: Path):
    """budget=0 means 'no cap' (matches global cost_guard contract)."""
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"
    now = datetime.now(timezone.utc).isoformat()
    _write_log(log_path, [
        {"ts": now, "route_id": "route_ax",
         "input_tokens": 1_000_000, "output_tokens": 0},
    ])
    _seed_settings(conn, {"route_ax": 0.0})
    assert check_per_route_alerts(conn, log_path) == []
