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


def todays_cost_per_route_usd(ai_calls_log: Path) -> dict[str, float]:
    """Sum today's ``cost`` field grouped by ``route_id``.

    Step 18: backs the per-route budget alerts. Uses the same scan as
    ``todays_cost_usd`` (one pass over the jsonl file). Rows without a
    ``route_id`` bucket under the literal key ``"(unattributed)"`` so the
    operator sees cost that isn't being attributed (legacy rows or
    pre-Step-18 entries).

    Cost extraction: prefers a literal ``cost`` field (kept for
    forward-compat — orchestrator doesn't write it today); falls back to
    estimating from tokens using the same reference rates as
    ``ai_costs.DEFAULT_*``. This duplicates the rate constants on
    purpose: ``cost_guard`` runs in the bot process while ``ai_costs``
    is GUI-only — circular import would otherwise force a refactor.
    """
    if not ai_calls_log.exists():
        return {}
    today = _today_iso()
    # Reference rates (mirror src/gui/services/ai_costs.py DEFAULT_*).
    input_per_m, output_per_m, cache_read_per_m = 3.0, 15.0, 0.30
    totals: dict[str, float] = {}
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
                # Prefer literal cost; fall back to token-derived estimate.
                cost = row.get("cost") or row.get("estimated_cost")
                if cost is None:
                    try:
                        cost = (
                            (int(row.get("input_tokens") or 0) * input_per_m)
                            + (int(row.get("output_tokens") or 0) * output_per_m)
                            + (int(row.get("cache_read_tokens") or 0) * cache_read_per_m)
                        ) / 1_000_000.0
                    except (TypeError, ValueError):
                        continue
                try:
                    c = float(cost)
                except (TypeError, ValueError):
                    continue
                key = str(row.get("route_id") or "(unattributed)") or "(unattributed)"
                totals[key] = totals.get(key, 0.0) + c
    except OSError:
        return {}
    return totals


def check_per_route_alerts(
    conn: sqlite3.Connection,
    ai_calls_log: Path,
) -> list[tuple[str, float, float]]:
    """Return ``(route_id, today_spend, budget)`` tuples for breached routes.

    Step 18: per-route budgets live in a single settings row keyed
    ``cost_route_budgets_json``, holding ``{"route_id": budget_usd, ...}``.
    The caller (cost_guard_loop) DMs the operator once per breach event.

    Unlike the global cost guard, route breaches DO NOT flip the
    ``kill_switch`` — too disruptive. Operators wanting per-route auto-
    halt can layer ``Route.halted`` (Step 15) on top via a future
    automation; this step delivers visibility only.
    """
    cur = conn.execute(
        "SELECT value FROM settings WHERE key = 'cost_route_budgets_json'"
    ).fetchone()
    if cur is None or not cur[0]:
        return []
    try:
        budgets = json.loads(cur[0])
        if not isinstance(budgets, dict):
            return []
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    if not budgets:
        return []
    per_route = todays_cost_per_route_usd(ai_calls_log)
    breached: list[tuple[str, float, float]] = []
    for route_id, budget_raw in budgets.items():
        try:
            budget = float(budget_raw)
        except (TypeError, ValueError):
            continue
        if budget <= 0:
            continue
        spent = float(per_route.get(route_id, 0.0))
        if spent > budget:
            breached.append((route_id, spent, budget))
    return breached


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
