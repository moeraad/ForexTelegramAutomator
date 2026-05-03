"""Live-AI regression for the Phase-2/3 management action types.

Loads fixtures/management_messages.jsonl, seeds an in-memory DB so that
state_summary.render_open_positions(conn) yields the SYSTEM STATE block
each fixture expects, then runs the message through the live AIClient
and asserts the emitted action types match.

Skipped unless ANTHROPIC_API_KEY (or OPENAI_API_KEY when AI_PROVIDER=openai)
is set — same gating pattern as tests/test_replay.py. Costs money to run.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.ai import AIClient
from src.db import connect, init_schema
from src.state_summary import render_open_positions


FIXTURE_PATH = Path(__file__).resolve().parent.parent / "fixtures" / "management_messages.jsonl"


def _provider_key_present() -> bool:
    """True if either provider's key is set — match the AI_PROVIDER switch."""
    if os.getenv("ANTHROPIC_API_KEY"):
        return True
    if os.getenv("OPENAI_API_KEY"):
        return True
    return False


pytestmark = pytest.mark.skipif(
    not _provider_key_present(),
    reason="No AI provider key set; skipping live management replay",
)


def _load_fixtures() -> list[dict]:
    if not FIXTURE_PATH.exists():
        return []
    with open(FIXTURE_PATH, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _seed_state(conn, state: dict) -> None:
    """Populate positions, actions, settings rows so render_open_positions
    produces a SYSTEM STATE block consistent with the fixture's `state`.
    """
    now = datetime.now(timezone.utc)

    # ---- Open position ----
    op = state.get("open_position")
    if op is not None:
        signal_payload = json.dumps({
            "type": "OPEN", "symbol": "XAUUSD", "side": op["side"],
            "entry_low": op["entry_price"] - 1.0,
            "entry_high": op["entry_price"] + 1.0,
            "sl": op["sl"], "tps": [op["tp"]],
        })
        cur = conn.execute(
            "INSERT INTO actions(action_type, payload_json, status) "
            "VALUES('OPEN', ?, 'executed')",
            (signal_payload,),
        )
        action_id = cur.lastrowid
        sl_moved_at = now.isoformat() if op.get("sl_moved") else None
        conn.execute(
            "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
            "original_volume, partial_close_count, entry_price, sl, tp, "
            "sl_moved_at, status, opened_at) "
            "VALUES(?, ?, 'XAUUSD', ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?)",
            (action_id, 90001, op["side"], op["volume"], op["original_volume"],
             int(op["partials_taken"]), op["entry_price"], op["sl"], op["tp"],
             sl_moved_at, now.isoformat()),
        )

    # ---- Last closed position ----
    lc = state.get("last_closed")
    if lc is not None:
        signal_payload = json.dumps({
            "type": "OPEN", "symbol": "XAUUSD", "side": lc["side"],
            "entry_low": lc["entry_price"] - 1.0,
            "entry_high": lc["entry_price"] + 1.0,
            "sl": lc["sl"], "tps": lc["tps"],
        })
        cur = conn.execute(
            "INSERT INTO actions(action_type, payload_json, status) "
            "VALUES('OPEN', ?, 'executed')",
            (signal_payload,),
        )
        action_id = cur.lastrowid
        closed_at = (now - timedelta(minutes=int(lc["closed_minutes_ago"]))).isoformat()
        opened_at = (now - timedelta(minutes=int(lc["closed_minutes_ago"]) + 30)).isoformat()
        conn.execute(
            "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
            "original_volume, partial_close_count, entry_price, sl, tp, "
            "status, opened_at, closed_at, close_reason) "
            "VALUES(?, ?, 'XAUUSD', ?, ?, ?, ?, ?, ?, ?, 'closed', ?, ?, ?)",
            (action_id, 90100, lc["side"], 0.04, 0.08, 1,
             lc["entry_price"], lc["sl"], lc["tps"][0],
             opened_at, closed_at, lc["close_reason"]),
        )

    # ---- Market price ----
    market = state.get("market")
    if market is not None:
        for key, val in (
            ("market_XAUUSD_bid", str(market["bid"])),
            ("market_XAUUSD_ask", str(market["ask"])),
            ("market_XAUUSD_at", now.isoformat()),
        ):
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, val),
            )


@pytest.mark.parametrize("fx", _load_fixtures(), ids=lambda fx: fx["id"])
def test_management_message_replay(fx, tmp_path) -> None:
    conn = connect(str(tmp_path / "replay.db"))
    init_schema(conn)
    _seed_state(conn, fx["state"])

    state_block = render_open_positions(conn)
    client = AIClient()
    result = client.call(
        recent_chat="(none — single-message replay)",
        open_positions_block=state_block,
        new_message=fx["message"],
    )

    actual_types = sorted(a.type for a in result.response.actions)
    expected_types = sorted(fx["expected_action_types"])

    # Action types must match exactly. ALERTs are tolerated as additions
    # for borderline cases — drop them before comparison so a model that
    # adds a defensive ALERT alongside the right action still passes.
    actual_non_alert = [t for t in actual_types if t != "ALERT"]
    expected_non_alert = [t for t in expected_types if t != "ALERT"]

    assert actual_non_alert == expected_non_alert, (
        f"\n  fixture: {fx['id']}\n"
        f"  message: {fx['message']!r}\n"
        f"  expected (non-alert): {expected_non_alert}\n"
        f"  actual   (non-alert): {actual_non_alert}\n"
        f"  category got={result.response.category} expected={fx.get('expected_category')}\n"
        f"  reasoning: {result.response.reasoning}\n"
        f"  raw: {result.raw_text[:500]}\n"
    )

    # Category check is informational — log mismatches but don't fail on
    # them, since a model that reasons differently but emits the right
    # actions is still functionally correct. Promote to assert later
    # once the prompt is stable.
    if fx.get("expected_category") and result.response.category != fx["expected_category"]:
        print(
            f"\n[category-soft-fail] {fx['id']}: "
            f"expected={fx['expected_category']} got={result.response.category}"
        )
