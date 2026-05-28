"""Build the AI-prompt state context: open positions, pending OPENs,
last closed position, current market price.

Why each block:
  - OPEN POSITIONS: enriched with original_volume / partial_close_count /
    sl_moved_at so the prompt can apply idempotency rules ("don't re-emit
    CLOSE_PARTIAL on a reminder if it already fired").
  - PENDING OPEN SIGNALS: stops the AI from re-emitting an OPEN it already
    queued when the channel re-quotes the same setup.
  - LAST CLOSED POSITION: source of params for REOPEN_LAST and REINFORCE
    (Phase 2). Includes the originating signal's payload so the prompt can
    reconstruct the full trade (entry zone, SL, TPs, side).
  - MARKET (XAUUSD): current bid/ask so the model can decode two-digit SL
    shorthand (e.g. "ستوبك 56" -> 4856 only when gold is around 4850).
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from src.position_signal import find_last_replayable_closed


# Match SL to entry within ~5 cents; XAUUSD tick is 0.01.
_BE_TOLERANCE = 0.05


def _fmt(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "-"


def _at_be(sl: float | None, entry: float | None) -> bool:
    if sl is None or entry is None:
        return False
    return abs(sl - entry) <= _BE_TOLERANCE


def _age_seconds(iso_str: str | None) -> int | None:
    """Seconds since iso_str (UTC). Returns None if unparseable."""
    if not iso_str:
        return None
    try:
        # SQLite stores '2026-05-01T22:00:00+00:00' (our writes) or
        # '2026-05-01 22:00:00' (CURRENT_TIMESTAMP default). Handle both.
        s = iso_str.replace(" ", "T")
        if not (s.endswith("Z") or "+" in s[10:] or "-" in s[10:]):
            s = s + "+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        ts = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    delta = datetime.now(timezone.utc) - ts
    return int(delta.total_seconds())


def _get_market_mid(
    conn: sqlite3.Connection, *, symbol: str = "XAUUSD", stale_seconds: int = 60
) -> float | None:
    """Return MARKET mid price for `symbol` if a fresh quote exists, else None.

    Used by `_render_executed_positions` to compute per-position PnL +
    `in_profit` flag. The flag drives the AI's "OPEN signal arrives with
    position open" decision tree (close+reopen vs. modify-in-place rule).
    Mirrors `_render_market_price`'s freshness threshold so the two blocks
    stay consistent.
    """
    sym = symbol.upper()
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?,?,?)",
            (f"market_{sym}_bid", f"market_{sym}_ask", f"market_{sym}_at"),
        ).fetchall()
    }
    bid = rows.get(f"market_{sym}_bid")
    ask = rows.get(f"market_{sym}_ask")
    at = rows.get(f"market_{sym}_at")
    if bid is None or ask is None or at is None:
        return None
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return None
    age = _age_seconds(at)
    if age is None or age > stale_seconds:
        return None
    return (bid_f + ask_f) / 2.0


def _pnl_line(side: str | None, entry: float | None, mid: float | None) -> str:
    """Format a single 'pnl=…' line. Stable suffix strings — the AI prompt
    decision tree keys off the literal '(in_profit)' / '(at_be)' /
    '(in_loss)' / '(market_stale)'."""
    if mid is None or entry is None or side not in ("BUY", "SELL"):
        return "      pnl=unknown (market_stale)"
    delta = (mid - entry) if side == "BUY" else (entry - mid)
    if delta > 0.005:
        flag = "in_profit"
    elif delta < -0.005:
        flag = "in_loss"
    else:
        flag = "at_be"
    sign = "+" if delta > 0 else ("-" if delta < 0 else "")
    return f"      pnl={sign}{abs(delta):.2f}/oz ({flag})  mid={mid:.2f}"


def _render_executed_positions(
    conn: sqlite3.Connection, *, mid: float | None = None
) -> list[str]:
    rows = conn.execute(
        "SELECT action_id, mt5_ticket, symbol, side, volume, original_volume, "
        "       partial_close_count, entry_price, sl, tp, sl_moved_at, opened_at, "
        "       COALESCE(is_naked, 0) AS is_naked, naked_opened_at "
        "FROM positions WHERE status='open' "
        "ORDER BY action_id, mt5_ticket"
    ).fetchall()

    lines = ["OPEN POSITIONS (from this channel):"]
    if not rows:
        lines.append("  (none)")
        return lines

    by_signal: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_signal[r["action_id"]].append(r)

    for action_id, group in by_signal.items():
        lines.append(f"  Signal #{action_id}:")
        for r in group:
            sl_flags: list[str] = []
            if _at_be(r["sl"], r["entry_price"]):
                sl_flags.append("at_BE")
            if r["sl_moved_at"]:
                sl_flags.append("moved")
            sl_suffix = f" ({', '.join(sl_flags)})" if sl_flags else ""
            orig = r["original_volume"]
            orig_str = f" of {_fmt(orig)} orig" if orig is not None else ""
            partials = r["partial_close_count"] or 0
            age = _age_seconds(r["opened_at"])
            age_str = f"  age={age // 60}min" if age is not None else ""
            # NAKED flag: position opened from a bare directional command
            # (OPEN_INSTANT). Awaiting a structured signal to attach SL/TPs.
            # AI must emit ATTACH_SIGNAL (same side) or CLOSE_FULL +
            # OPEN_INSTANT (opposite side), NEVER a new OPEN.
            naked_tag = "  [NAKED — awaiting ATTACH_SIGNAL]" if r["is_naked"] else ""
            lines.append(
                f"    ticket={r['mt5_ticket']}  {r['side']} {r['symbol']}  "
                f"vol={_fmt(r['volume'])}{orig_str}  entry={_fmt(r['entry_price'])}"
                f"{naked_tag}"
            )
            lines.append(
                f"      sl={_fmt(r['sl'])}{sl_suffix}  tp={_fmt(r['tp'])}  "
                f"partials_taken={partials}{age_str}"
            )
            lines.append(_pnl_line(r["side"], r["entry_price"], mid))
    return lines


def _render_pending_open_signals(conn: sqlite3.Connection) -> list[str]:
    """List OPEN actions still in the pipeline (not yet filled in MT5).

    Why: AI needs to know about pending limit orders it already produced
    so it doesn't re-emit them when the channel quotes/repeats the
    signal — AND so CANCEL_PENDING isn't suppressed as "no pending order"
    when there really is a broker-side pending limit awaiting fill.

    Statuses included:
      - pending/sent/claimed: still in the dispatch pipeline.
      - watching: the EA has placed a BuyLimit/SellLimit at the broker
        and is waiting for the fill (or for CANCEL_PENDING to kill it).
        OMITTING this status caused the 2026-05-26 incident where the
        channel said "cancel the order" but the AI emitted ZERO actions
        with context="cancel requested but no pending XAUUSD order
        exists" — even though a real broker-side limit was live.
    """
    rows = conn.execute(
        "SELECT id, payload_json, status FROM actions "
        "WHERE action_type='OPEN' "
        "AND status IN ('pending','sent','claimed','watching') "
        "ORDER BY id"
    ).fetchall()

    lines = ["PENDING OPEN SIGNALS (awaiting fill or execution):"]
    if not rows:
        lines.append("  (none)")
        return lines
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            p = {}
        tps = p.get("tps") or []
        tps_str = ",".join(f"{t:g}" for t in tps) if tps else "-"
        lines.append(
            f"  Signal #{r['id']} [{r['status']}]: "
            f"{p.get('side','?')} {p.get('symbol','?')} "
            f"entry={_fmt(p.get('entry_low'))}-{_fmt(p.get('entry_high'))} "
            f"sl={_fmt(p.get('sl'))} tps={tps_str}"
        )
    return lines


def _render_last_closed_position(
    conn: sqlite3.Connection, *, symbol: str = "XAUUSD", within_hours: int = 24
) -> list[str]:
    """Most recent REPLAYABLE closed position + originating signal —
    drives REOPEN_LAST / REINFORCE in the prompt.

    Uses `find_last_replayable_closed` (the same helper backing
    `/positions/last_closed`) so the prompt and the EA agree on what
    counts as "the last closed trade." OPEN_INSTANT-origin positions
    with no ATTACH_SIGNAL are skipped — surfacing them as the
    last-closed would trick the AI into emitting REOPEN_LAST against an
    unreplayable trade, which the EA then fails with
    `last_closed_unparseable`.
    """
    lines = [f"LAST CLOSED POSITION ({symbol}, within {within_hours}h):"]
    found = find_last_replayable_closed(
        conn, symbol=symbol, within_hours=within_hours,
    )
    if found is None:
        lines.append(
            f"  (none — no replayable {symbol} position closed in the last "
            f"{within_hours}h)"
        )
        return lines
    row, sig = found
    age = _age_seconds(row["closed_at"])
    age_str = f" ({age // 60} min ago)" if age is not None else ""
    lines.append(
        f"  ticket={row['mt5_ticket']}  {row['side']} {row['symbol']}  "
        f"closed_at={row['closed_at']}{age_str}"
    )
    lines.append(
        f"    entry={_fmt(row['entry_price'])}  "
        f"sl_at_close={_fmt(row['sl'])}  tp_at_close={_fmt(row['tp'])}"
    )
    lines.append(
        f"    original_volume={_fmt(row['original_volume'])}  "
        f"volume_at_close={_fmt(row['volume'])}  "
        f"partial_close_count={row['partial_close_count'] or 0}"
    )
    lines.append(f"    close_reason={row['close_reason'] or '-'}")
    tps = sig.get("tps") or []
    tps_str = ",".join(f"{t:g}" for t in tps) if tps else "-"
    lines.append(
        f"    signal: {sig.get('side','?')} {sig.get('symbol','?')} "
        f"entry={_fmt(sig.get('entry_low'))}-{_fmt(sig.get('entry_high'))} "
        f"sl={_fmt(sig.get('sl'))} tps=[{tps_str}]"
    )
    return lines


def _render_market_price(
    conn: sqlite3.Connection, *, symbol: str = "XAUUSD", stale_seconds: int = 60
) -> list[str]:
    """Current bid/ask if the EA recently heartbeat them; else a clear marker."""
    sym = symbol.upper()
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?,?,?)",
            (f"market_{sym}_bid", f"market_{sym}_ask", f"market_{sym}_at"),
        ).fetchall()
    }
    bid = rows.get(f"market_{sym}_bid")
    ask = rows.get(f"market_{sym}_ask")
    at = rows.get(f"market_{sym}_at")
    if bid is None or ask is None or at is None:
        return [f"MARKET ({sym}): (no recent quote — EA not heartbeating prices)"]
    try:
        bid_f = float(bid)
        ask_f = float(ask)
    except (TypeError, ValueError):
        return [f"MARKET ({sym}): (corrupt quote — bid={bid!r} ask={ask!r})"]
    age = _age_seconds(at)
    if age is None:
        age_str = "age unknown"
    elif age > stale_seconds:
        age_str = f"STALE: age {age}s > {stale_seconds}s — do not trust for shorthand SL decoding"
    else:
        age_str = f"age {age}s"
    return [
        f"MARKET ({sym}): bid={bid_f:.2f} ask={ask_f:.2f} "
        f"mid={(bid_f + ask_f) / 2.0:.2f} ({age_str})"
    ]


def _render_reply_context(
    conn: sqlite3.Connection, *, tg_message_id: int, chat_id: int,
) -> list[str]:
    """When the current message replies to a prior message, prepend the
    parent's text so the AI can resolve pronouns like "cancel that
    order" / "use BE on this" against a concrete antecedent.

    Returns an empty list when the current message has no
    reply_to_tg_message_id, when the parent message isn't in the local
    messages table (cross-chat reply, pre-migration row), or when the
    lookup fails. The caller appends a blank-line separator only when
    the returned list is non-empty.
    """
    if not tg_message_id or not chat_id:
        return []
    cur = conn.execute(
        "SELECT reply_to_tg_message_id FROM messages "
        "WHERE chat_id=? AND tg_message_id=?",
        (chat_id, tg_message_id),
    ).fetchone()
    if cur is None:
        return []
    parent_id = cur["reply_to_tg_message_id"]
    if not parent_id:
        return []
    parent = conn.execute(
        "SELECT text, sender, received_at FROM messages "
        "WHERE chat_id=? AND tg_message_id=?",
        (chat_id, parent_id),
    ).fetchone()
    if parent is None:
        return [
            f"REPLY CONTEXT: this message replies to tg_msg_id={parent_id}, "
            "but the parent message is not in the local archive (may be "
            "older than backfill, or from before reply-capture landed)."
        ]
    parent_text = (parent["text"] or "").strip()
    # Trim very long parents so the prompt budget stays sane. Channels
    # rarely reply to anything that wouldn't fit comfortably here.
    if len(parent_text) > 600:
        parent_text = parent_text[:600] + "... [truncated]"
    age = _age_seconds(parent["received_at"])
    age_str = f" ({age // 60} min ago)" if age is not None else ""
    return [
        f"REPLY CONTEXT (this message is a reply to tg_msg_id={parent_id}"
        f"{age_str}):",
        f'  parent: "{parent_text}"',
    ]


def render_open_positions(
    conn: sqlite3.Connection,
    *, symbol: str = "XAUUSD",
    current_tg_message_id: int | None = None,
    current_chat_id: int | None = None,
) -> str:
    """AI prompt context: open positions + pending OPENs + last-closed + market price.

    ``symbol`` defaults to XAUUSD for backward compatibility; Step 3 of the
    multi-channel plan threads ``profile.symbol`` here so destinations serving
    different symbols (deferred capability) produce a correctly-scoped block.

    ``current_tg_message_id`` + ``current_chat_id`` (added 2026-05-26):
    when both are provided, prepends a REPLY CONTEXT block with the parent
    message's text whenever the current message is a Telegram reply. The
    AI uses this to resolve pronouns. Both default None for callers that
    don't have message context (e.g. the GUI's prompt inspector).

    Name kept for caller compatibility (orchestrator.py).
    """
    mid = _get_market_mid(conn, symbol=symbol)
    parts: list[str] = []
    if current_tg_message_id is not None and current_chat_id is not None:
        reply_block = _render_reply_context(
            conn,
            tg_message_id=current_tg_message_id,
            chat_id=current_chat_id,
        )
        if reply_block:
            parts.extend(reply_block)
            parts.append("")
    parts.extend(_render_executed_positions(conn, mid=mid))
    parts.append("")
    parts.extend(_render_pending_open_signals(conn))
    parts.append("")
    parts.extend(_render_last_closed_position(conn, symbol=symbol))
    parts.append("")
    parts.extend(_render_market_price(conn, symbol=symbol))
    return "\n".join(parts)
