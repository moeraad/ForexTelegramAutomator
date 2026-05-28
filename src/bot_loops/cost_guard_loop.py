"""Cost-cap watchdog loop moved out of ``src/bot.py``."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

from telegram.ext import Application


log = logging.getLogger("bot")


async def cost_guard_loop(app: Application):
    """Once a minute, compare today's AI spend against the configured
    daily cap. If breached, force kill_switch=on and DM the operator.
    The watchdog is silent on every clean tick so it doesn't spam the
    log when the budget is healthy.
    """
    from src.cost_guard import check_and_enforce, check_per_route_alerts
    from src.logging_setup import LOGS_DIR
    conn: sqlite3.Connection = app.bot_data["conn"]
    ai_calls_log = LOGS_DIR / "ai_calls.jsonl"
    # Step 18: per-route alerts use a sticky-set so the operator gets ONE
    # DM per breach (not one per tick). The set resets daily — when
    # today's spend drops back under budget OR UTC midnight rolls over
    # (next tick's totals start at zero), the route_id leaves the set
    # and re-alerts on the next breach.
    alerted_routes: set[str] = set()
    while True:
        try:
            event, reason = check_and_enforce(conn, ai_calls_log)
            if event == "halted":
                from src.notify import notify_owner
                notify_owner(
                    "⚠ Cost cap hit — auto-halted\n\n"
                    f"{reason}\n\n"
                    f"At UTC {datetime.now(timezone.utc).isoformat(timespec='seconds')}. "
                    "Investigate REJECTED/COST views, raise the budget if "
                    "warranted, then click RESUME."
                )
            elif event == "resumed":
                # REVIEW.md Q7 — UTC midnight (or a budget raise) brings
                # today's spend back under cap. cost_guard cleared its
                # own halt; tell the operator so they know trading is live.
                from src.notify import notify_owner
                notify_owner(
                    "▶ Cost cap cleared — auto-resumed\n\n"
                    f"{reason}\n\n"
                    f"At UTC {datetime.now(timezone.utc).isoformat(timespec='seconds')}."
                )

            # Step 18: per-route budget alerts (informational, no halt).
            try:
                breaches = check_per_route_alerts(conn, ai_calls_log)
            except Exception:  # noqa: BLE001 — never let route alerts kill the loop
                log.exception("check_per_route_alerts failed")
                breaches = []
            current = {route_id for (route_id, _, _) in breaches}
            for route_id, spent, budget in breaches:
                if route_id in alerted_routes:
                    continue
                alerted_routes.add(route_id)
                from src.notify import notify_owner
                notify_owner(
                    f"⚠ Route budget exceeded — {route_id}\n\n"
                    f"Today's spend on this route: ${spent:.2f}\n"
                    f"Configured budget: ${budget:.2f}\n\n"
                    "Per-route alerts are informational — trading on this "
                    "route is NOT auto-halted (use Routes view → Toggle "
                    "Halt to pause manually). Raise the budget or "
                    "investigate Cost view → Cost by route for the driver."
                )
            # Routes no longer breaching leave the sticky set so a fresh
            # breach later (after a midnight roll, a manual halt+unhalt,
            # or a budget bump) re-alerts.
            alerted_routes &= current
        except Exception as e:  # noqa: BLE001
            log.exception("cost_guard_loop error: %s", e)
        await asyncio.sleep(60.0)
