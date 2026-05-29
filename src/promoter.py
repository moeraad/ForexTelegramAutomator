import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from src.db import get_setting

_trades_log = logging.getLogger("trades")

# Management actions that carry NO mt5_ticket and resolve their target from
# whatever the singleton open position happens to be AT EXECUTION TIME (the
# EA's FindSingletonOpenTicket). Their effect is therefore time-sensitive to
# the live position: if one is dispatched late — after the position it was
# reasoned against has closed and a *different* one has opened — it silently
# retargets the new trade.
#
# That is exactly the failure captured in diagnostics SMC 2026-05-29: a
# CLOSE_FULL meant for an already-SL'd position was stranded in 'claimed',
# resurrected by release_stale_claims 5 minutes later, re-claimed against the
# freshly opened singleton, and closed a brand-new trade for a fraction of its
# intended run. Legacy CLOSE/MODIFY/CLOSE_ALL are excluded — they carry an
# explicit mt5_ticket and cannot retarget. OPEN/OPEN_INSTANT are excluded —
# their duplicate-fire risk is handled by the api.py post_result claim_expired
# guard. ALERT/CANCEL_PENDING/ATTACH_SIGNAL never reach the EA dispatcher this
# way.
SINGLETON_MANAGEMENT_TYPES = frozenset({
    "MOVE_SL_BE", "MOVE_SL", "CLOSE_PARTIAL", "CLOSE_FULL",
    "REOPEN_LAST", "REINFORCE", "TIGHTEN_SL", "MODIFY_TPS",
})

# A singleton-management action should be applied within seconds of becoming
# executable (the EA polls /actions?status=sent at ~1Hz). One that has been
# 'sent' (or 'claimed') longer than this is far more likely to act on the
# wrong position than to do something useful, so it is failed rather than
# allowed to fire. Comfortably below release_stale_claims' 300s window.
MANAGEMENT_MAX_AGE_SEC = 180


def promote_due_actions(conn: sqlite3.Connection) -> int:
    """Promote pending actions whose execute_after has passed. Returns count promoted.

    Filter contract: any pending row with a non-null execute_after that has
    elapsed is promotable. ALERTs are excluded by construction because the
    orchestrator inserts them WITHOUT an execute_after (see orchestrator.py
    inside process_message — the ALERT INSERT omits the execute_after column).
    Don't whitelist action_types here: every time we add a new type, we'd have
    to update this list, and forgetting to do so silently strands the type
    in pending forever (which is exactly what happened to the 7 Phase-2
    management types: MOVE_SL_BE, MOVE_SL, CLOSE_PARTIAL, CLOSE_FULL,
    REOPEN_LAST, REINFORCE, TIGHTEN_SL).
    """
    if get_setting(conn, "kill_switch") == "on":
        return 0
    now_iso = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "UPDATE actions SET status='sent' "
        "WHERE status='pending' "
        "AND execute_after IS NOT NULL "
        "AND execute_after <= ?",
        (now_iso,),
    )
    return cur.rowcount


def release_stale_claims(conn: sqlite3.Connection, max_age_sec: int = 300) -> int:
    """Release claimed actions whose claimed_at is older than max_age_sec back to 'sent'.

    Why: if the EA crashes between /claim and /result, the action would be stranded
    in 'claimed' forever. This sweeper recovers it after a safe quiet period.

    The window was bumped from 120s to 300s to reduce the race against a slow
    EA: if the EA's POST /result is just slow (broker latency, large trade),
    a 120s reset led to the EA re-claiming and re-firing a duplicate broker
    order. 300s gives plenty of headroom for legitimately slow operations
    while still recovering reasonably fast from a real crash.

    Recycling to 'sent' is safe for OPEN (duplicate fire is blocked by the
    api.py post_result claim_expired guard) and for legacy ticketed types
    (they target an explicit mt5_ticket). It is NOT safe for the ticketless
    singleton-management types in ``SINGLETON_MANAGEMENT_TYPES``: re-arming
    one after a multi-minute gap lets it execute against whatever singleton
    is open then — which may be a different trade than the AI reasoned about.
    Diagnostics SMC 2026-05-29 is exactly this: a resurrected CLOSE_FULL
    closed a freshly opened position. Those types are therefore marked
    terminal 'failed' (``stale_claim_not_retried``) instead of recycled, so
    the EA never re-dispatches them. The channel re-sends if it still wants
    the action, and ReconcileClosedPositions converges DB state if a slow EA
    did complete the broker call after all.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    # Capture ids BEFORE the UPDATE so we can audit each released row.
    # Even one released claim is interesting: it's the precondition for
    # the duplicate-order risk that the api.py post_result guard now
    # blocks (REVIEW.md P3 #9). Without this line, a re-claim that fires
    # a second broker order leaves no breadcrumb tying the new claim to
    # the released one.
    released = conn.execute(
        "SELECT id, claimed_at, action_type FROM actions "
        "WHERE status='claimed' AND claimed_at IS NOT NULL AND claimed_at < ?",
        (cutoff,),
    ).fetchall()
    # Recycle the retry-safe types back to 'sent'.
    conn.execute(
        "UPDATE actions SET status='sent', claimed_at=NULL "
        "WHERE status='claimed' AND claimed_at IS NOT NULL AND claimed_at < ? "
        f"AND action_type NOT IN ({_in_placeholders(SINGLETON_MANAGEMENT_TYPES)})",
        (cutoff, *sorted(SINGLETON_MANAGEMENT_TYPES)),
    )
    # Fail the singleton-management types so a stale claim can never retarget.
    conn.execute(
        "UPDATE actions SET status='failed', ea_response='stale_claim_not_retried', "
        "executed_at=? "
        "WHERE status='claimed' AND claimed_at IS NOT NULL AND claimed_at < ? "
        f"AND action_type IN ({_in_placeholders(SINGLETON_MANAGEMENT_TYPES)})",
        (now_iso, cutoff, *sorted(SINGLETON_MANAGEMENT_TYPES)),
    )
    for r in released:
        is_mgmt = r["action_type"] in SINGLETON_MANAGEMENT_TYPES
        _trades_log.warning(
            "claim_released id=%s type=%s claimed_at=%s max_age_sec=%s outcome=%s",
            r["id"], r["action_type"], r["claimed_at"], max_age_sec,
            "failed_no_retry" if is_mgmt else "recycled_sent",
        )
    return len(released)


def _in_placeholders(values) -> str:
    """Return a ``?,?,...`` placeholder string sized for ``values``."""
    return ",".join("?" for _ in values)


def expire_stale_management(
    conn: sqlite3.Connection, max_age_sec: int = MANAGEMENT_MAX_AGE_SEC
) -> int:
    """Fail singleton-management actions that have been executable too long.

    Complements release_stale_claims (which guards the claimed-then-resurrect
    path) by closing the slow-poll path: an action that sits in 'sent' while a
    new position opens, then gets claimed and executed against that new
    singleton. ``execute_after`` is a sound lower bound for "became
    executable" — promote_due_actions only flips a row to 'sent' once
    execute_after has elapsed, and the zero-grace fast path also stamps it at
    insert. Rows still in 'pending' are untouched (they cannot be claimed yet,
    so they carry no retarget risk; this also leaves backfill-review rows for
    the operator). Returns the number of actions failed.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=max_age_sec)).isoformat()
    now_iso = datetime.now(timezone.utc).isoformat()
    stale = conn.execute(
        "SELECT id, action_type, execute_after FROM actions "
        "WHERE status='sent' AND execute_after IS NOT NULL AND execute_after < ? "
        f"AND action_type IN ({_in_placeholders(SINGLETON_MANAGEMENT_TYPES)})",
        (cutoff, *sorted(SINGLETON_MANAGEMENT_TYPES)),
    ).fetchall()
    if not stale:
        return 0
    conn.execute(
        "UPDATE actions SET status='failed', ea_response='management_action_expired', "
        "executed_at=? "
        "WHERE status='sent' AND execute_after IS NOT NULL AND execute_after < ? "
        f"AND action_type IN ({_in_placeholders(SINGLETON_MANAGEMENT_TYPES)})",
        (now_iso, cutoff, *sorted(SINGLETON_MANAGEMENT_TYPES)),
    )
    for r in stale:
        _trades_log.warning(
            "management_action_expired id=%s type=%s execute_after=%s max_age_sec=%s",
            r["id"], r["action_type"], r["execute_after"], max_age_sec,
        )
    return len(stale)


