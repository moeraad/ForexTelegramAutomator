"""Shared helpers for resolving the originating OPEN signal of a position.

Both `src.api` (for the EA-facing `/positions/last_closed` endpoint) and
`src.state_summary` (for the AI prompt's LAST CLOSED block) need to answer
the same question: "what's the most recent closed position that we can
actually replay?" Replayable means: side, sl, tps, and an entry zone are
known. OPEN_INSTANT-origin positions whose ATTACH_SIGNAL never fired are
NOT replayable — REOPEN_LAST/REINFORCE against them produces the EA
result `last_closed_unparseable`.

Keeping the logic here means the prompt and the EA stay in lockstep: if
the prompt sees a replayable last-closed, the EA endpoint will return
the same one; if neither finds anything replayable, the prompt knows to
emit ALERT instead of REOPEN_LAST.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any


def parse_payload(raw: str | None) -> dict | None:
    """Tolerant JSON decode for actions.payload_json columns."""
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def payload_has_signal_fields(payload: dict | None) -> bool:
    """True when the payload carries the OPEN-signal fields the EA's
    BuildOpenPayloadFromLastClosed parser needs (entry_low + entry_high
    + sl + non-empty tps).
    """
    if not payload:
        return False
    if any(payload.get(k) is None for k in ("entry_low", "entry_high", "sl")):
        return False
    return bool(payload.get("tps"))


def resolve_signal_payload(
    conn: sqlite3.Connection,
    *,
    joined_payload: str | None,
    symbol: str,
    side: str,
    opened_at: str,
    until: str | None,
) -> dict | None:
    """Return the canonical signal dict for a position.

    For plain OPEN, the joined payload already has the signal fields.
    For OPEN_INSTANT, walk forward to find an ATTACH_SIGNAL action whose
    payload matches symbol/side and was executed during the position's
    lifetime; return that payload as the signal.

    Returns the resolved dict (which may still be sparse) or None when
    nothing is joinable. Callers should check
    `payload_has_signal_fields` to decide whether the result is actually
    replayable.
    """
    direct = parse_payload(joined_payload)
    if payload_has_signal_fields(direct):
        return direct
    if until is None:
        rows = conn.execute(
            "SELECT payload_json FROM actions "
            "WHERE action_type='ATTACH_SIGNAL' "
            "  AND status='executed' "
            "  AND created_at >= ? "
            "  AND json_extract(payload_json,'$.symbol')=? "
            "  AND json_extract(payload_json,'$.side')=? "
            "ORDER BY id DESC LIMIT 1",
            (opened_at, symbol, side),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT payload_json FROM actions "
            "WHERE action_type='ATTACH_SIGNAL' "
            "  AND status='executed' "
            "  AND created_at >= ? AND created_at <= ? "
            "  AND json_extract(payload_json,'$.symbol')=? "
            "  AND json_extract(payload_json,'$.side')=? "
            "ORDER BY id DESC LIMIT 1",
            (opened_at, until, symbol, side),
        ).fetchall()
    if rows:
        attached = parse_payload(rows[0]["payload_json"])
        if payload_has_signal_fields(attached):
            return attached
    return direct


# Maximum closed positions to inspect when walking back to find a
# replayable one. Far exceeds realistic operator activity in a 168h
# window; bounded so a corrupt DB can't make this loop unbounded.
_MAX_WALK = 50


def find_last_replayable_closed(
    conn: sqlite3.Connection, *, symbol: str, within_hours: int
) -> tuple[dict[str, Any], dict] | None:
    """Most recent closed position within the window whose originating
    signal is replayable (side + sl + tps + entry zone known).

    Walks back from the most recent close. OPEN_INSTANT positions that
    never received an ATTACH_SIGNAL are skipped — they have no zone to
    replay, so REOPEN_LAST/REINFORCE against them would fail at the EA
    with `last_closed_unparseable`.

    Returns `(row_dict, signal_dict)` or None when nothing in the window
    is replayable. `row_dict` is the position columns; `signal_dict` is
    the resolved payload (guaranteed to satisfy `payload_has_signal_fields`).
    """
    rows = conn.execute(
        "SELECT p.mt5_ticket, p.symbol, p.side, p.volume, p.original_volume, "
        "       p.partial_close_count, p.entry_price, p.sl, p.tp, "
        "       p.opened_at, p.closed_at, p.close_reason, p.action_id, "
        "       a.payload_json AS signal_payload "
        "FROM positions p "
        "LEFT JOIN actions a ON a.id = p.action_id "
        "WHERE p.symbol = ? AND p.status = 'closed' "
        "  AND p.closed_at IS NOT NULL "
        "  AND p.closed_at > datetime('now', ?) "
        "ORDER BY p.closed_at DESC LIMIT ?",
        (symbol, f"-{within_hours} hours", _MAX_WALK),
    ).fetchall()
    for row in rows:
        signal = resolve_signal_payload(
            conn,
            joined_payload=row["signal_payload"],
            symbol=row["symbol"],
            side=row["side"],
            opened_at=row["opened_at"],
            until=row["closed_at"],
        )
        if payload_has_signal_fields(signal):
            return dict(row), signal
    return None
