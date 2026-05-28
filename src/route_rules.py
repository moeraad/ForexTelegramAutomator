"""Per-route rule evaluation (Step 20 of multi-channel plan).

Two API-side rules and three EA-side rules. This module only owns the
API-side ones:

  - ``allowed_action_types``: drop the action if the route restricts to
    a different subset (e.g. an "entries-only" mirror leg rejects
    management actions)
  - ``time_of_day_filter``: drop the action when current UTC time is
    outside ``HH:MM-HH:MM``

The EA-side rules (``max_lots`` / ``min_account_balance`` /
``skip_if_drawdown_pct``) are propagated via the OPEN action payload
so the EA can refuse with a precise reason at execute time. Centralizing
their evaluation in the EA avoids a dual source of truth (the API would
otherwise need to poll MT5 balance, which it doesn't today).

The evaluator is pure: takes a Route + the smallest context required,
returns ``(allowed, reason)``. Callers decide what to do with the
rejection (silent drop, ALERT row, log, etc.).
"""
from __future__ import annotations

from datetime import datetime, time, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.config_v2 import Route


def _parse_time_window(window: str) -> tuple[time, time]:
    """Parse ``"HH:MM-HH:MM"`` into a (start, end) tuple of ``datetime.time``.

    Raises ValueError on malformed input. ``"23:00-01:00"`` is the
    overnight case — caller's evaluation handles wrap-around.
    """
    if "-" not in window:
        raise ValueError(
            f"time_of_day_filter must be 'HH:MM-HH:MM'; got {window!r}"
        )
    raw_start, raw_end = window.split("-", 1)
    return _parse_hhmm(raw_start.strip()), _parse_hhmm(raw_end.strip())


def _parse_hhmm(raw: str) -> time:
    if ":" not in raw:
        raise ValueError(f"time_of_day_filter component must be HH:MM; got {raw!r}")
    h_str, m_str = raw.split(":", 1)
    try:
        h, m = int(h_str), int(m_str)
    except ValueError:
        raise ValueError(f"time_of_day_filter component must be HH:MM; got {raw!r}") from None
    if not (0 <= h <= 23 and 0 <= m <= 59):
        raise ValueError(f"time_of_day_filter HH:MM out of range; got {raw!r}")
    return time(hour=h, minute=m)


def _in_time_window(now_utc: datetime, window: str) -> bool:
    """True iff ``now_utc.time()`` falls within ``window``.

    Empty / malformed window → True (don't gate). Overnight windows
    (start > end) wrap around midnight, so ``23:00-01:00`` matches
    23:30 and 00:30 alike.
    """
    if not window:
        return True
    try:
        start, end = _parse_time_window(window)
    except ValueError:
        return True  # malformed — fail-open
    now = now_utc.time().replace(second=0, microsecond=0)
    if start <= end:
        return start <= now < end
    # Wrap-around case.
    return now >= start or now < end


def evaluate_pre_persistence_rules(
    route: "Route",
    *,
    action_type: str,
    now_utc: datetime | None = None,
) -> tuple[bool, str]:
    """Evaluate the API-side rules (``allowed_action_types`` +
    ``time_of_day_filter``) for one action on one route.

    Returns ``(allowed, reason)``:
      - ``allowed=True, reason=""`` when the action may proceed
      - ``allowed=False, reason="<route_rule_*>"`` when the rule rejects;
        caller persists an ALERT (or logs + drops silently) and DOES NOT
        emit the underlying action.

    Reason codes are stable and grep-friendly:
      - ``route_rule_action_type_filtered``
      - ``route_rule_time_window``

    ``now_utc`` defaults to ``datetime.now(timezone.utc)`` for ergonomic
    calls from the orchestrator; tests pass an explicit time.
    """
    if route.allowed_action_types:
        if action_type not in route.allowed_action_types:
            allowed = ", ".join(route.allowed_action_types)
            return (
                False,
                f"route_rule_action_type_filtered: "
                f"action={action_type} allowed={allowed}",
            )
    if route.time_of_day_filter:
        now = now_utc or datetime.now(timezone.utc)
        if not _in_time_window(now, route.time_of_day_filter):
            return (
                False,
                f"route_rule_time_window: now_utc={now.strftime('%H:%M')} "
                f"window={route.time_of_day_filter}",
            )
    return True, ""
