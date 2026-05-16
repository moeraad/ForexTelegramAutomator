"""Daily AI-cost watchdog.

Runs inside the bot service. Once per minute it sums today's spend
from ``logs/ai_calls.jsonl`` and compares to
``cost_daily_budget_usd * cost_cap_multiplier``. On breach it flips
``kill_switch`` to ``on`` and DMs the operator. Once the breach clears
naturally (next UTC midnight or budget raised), the operator clicks
RESUME to re-enable trading.

Purpose: prevent a prompt-drift loop from running up a real bill
unattended.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


_log = logging.getLogger("cost_guard")


def _today_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def todays_cost_usd(ai_calls_log: Path) -> float:
    """Sum the ``cost`` field of every log row whose ``ts`` falls today
    (UTC). Cheap to compute; jsonl is small and only grows ~1KB/call."""
    if not ai_calls_log.exists():
        return 0.0
    today = _today_iso()
    total = 0.0
    try:
        with ai_calls_log.open("r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ts = str(row.get("ts", ""))
                if not ts.startswith(today):
                    continue
                cost = row.get("cost") or row.get("estimated_cost") or 0.0
                try:
                    total += float(cost)
                except (TypeError, ValueError):
                    continue
    except OSError:
        return 0.0
    return total


def check_and_enforce(
    conn: sqlite3.Connection,
    ai_calls_log: Path,
) -> tuple[str, str]:
    """Return ``(event, reason)`` where ``event`` is one of
    ``""``, ``"halted"``, ``"resumed"``. The caller DMs the operator
    once per non-empty event so a single restart-resume cycle yields
    exactly two messages (REVIEW.md Q7).

    Resume is automatic when today's UTC spend falls back under cap AND
    the halt was set by this guard (kill_switch_reason='cost_cap').
    Operator-set halts (kill_switch_reason='operator') stay halted
    until the operator explicitly resumes.
    """
    cur = conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('kill_switch', 'kill_switch_reason', "
        " 'cost_daily_budget_usd', 'cost_cap_multiplier')"
    )
    rows = dict(cur.fetchall())
    kill_switch = (rows.get("kill_switch") or "off").lower()
    kill_reason = (rows.get("kill_switch_reason") or "").lower()
    try:
        budget = float(rows.get("cost_daily_budget_usd") or 0.0)
    except ValueError:
        budget = 0.0
    try:
        multiplier = float(rows.get("cost_cap_multiplier") or 1.2)
    except ValueError:
        multiplier = 1.2

    # Budget disabled (0 means "no cap") — never halt or auto-resume.
    if budget <= 0:
        return "", ""

    cap = budget * multiplier
    today_cost = todays_cost_usd(ai_calls_log)

    if today_cost > cap:
        if kill_switch == "on":
            return "", ""
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) "
            "VALUES('kill_switch', 'on')"
        )
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) "
            "VALUES('kill_switch_reason', 'cost_cap')"
        )
        conn.commit()
        reason = (
            f"AI spend ${today_cost:.2f} exceeded daily cap "
            f"${cap:.2f} (budget ${budget:.2f} × {multiplier:.1f}). "
            "Trading auto-halted."
        )
        _log.warning("cost_guard tripped: %s", reason)
        return "halted", reason

    # Today's spend is under the cap. Auto-resume only if WE set the halt.
    if kill_switch == "on" and kill_reason == "cost_cap":
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) "
            "VALUES('kill_switch', 'off')"
        )
        conn.execute("DELETE FROM settings WHERE key='kill_switch_reason'")
        conn.commit()
        reason = (
            f"Daily AI spend back under cap "
            f"(${today_cost:.2f} ≤ ${cap:.2f}); trading auto-resumed. "
            "Likely UTC midnight roll-over or budget raise."
        )
        _log.info("cost_guard auto-resumed: %s", reason)
        return "resumed", reason

    return "", ""
