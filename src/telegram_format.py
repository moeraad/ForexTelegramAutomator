def render_action_notification(
    action_id: int, action_type: str, payload: dict,
    source_text: str, auto_execute_delay_sec: int,
) -> str:
    when = "now" if auto_execute_delay_sec <= 0 else f"in {auto_execute_delay_sec}s"
    if action_type == "OPEN":
        tps = " / ".join(str(t) for t in payload.get("tps", []))
        return (
            f"🟢 NEW SIGNAL #{action_id}  (auto-execute {when})\n\n"
            f"OPEN {payload['side']} {payload['symbol']}\n"
            f"Entry: {payload['entry_low']}–{payload['entry_high']}\n"
            f"SL:    {payload['sl']}\n"
            f"TPs:   {tps}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "MODIFY":
        return (
            f"🔧 MODIFY #{action_id}  (auto {when})\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"new_sl: {payload.get('new_sl')}  new_tp: {payload.get('new_tp')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "CLOSE":
        return (
            f"🔴 CLOSE #{action_id}  (auto {when})\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "CLOSE_ALL":
        return (
            f"🔴 CLOSE_ALL #{action_id}  (auto {when})\n"
            f"Symbol: {payload['symbol']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "ALERT":
        level = payload.get("level", "info").upper()
        return f"⚠️ ALERT [{level}] #{action_id}\n{payload.get('text','')}"
    return f"#{action_id} {action_type}: {payload}"


# ---- Phase 5: terminal-state + MT5-event notifications ------------------
#
# Per project policy (fully automated, no approval gate): the bot only
# notifies the operator about ACTIONS THAT HAPPENED and MT5 EVENTS.
# These two renderers produce the bodies for those DMs.

_STATUS_GLYPH = {
    "executed": "✅",
    "failed":   "❌",
    "rejected": "🚫",
    "watching": "👀",
}


def _payload_summary(action_type: str, payload: dict) -> str:
    """One-line summary of an action's payload for terminal notifications."""
    if action_type == "OPEN":
        tps = ",".join(str(t) for t in payload.get("tps", []))
        return (
            f"{payload.get('side','?')} {payload.get('symbol','XAUUSD')} "
            f"entry {payload.get('entry_low')}-{payload.get('entry_high')} "
            f"sl {payload.get('sl')} tps [{tps}]"
        )
    if action_type == "MOVE_SL":
        return f"price={payload.get('price')}"
    if action_type == "CLOSE_PARTIAL":
        return f"fraction={payload.get('fraction', 0.5)}"
    if action_type == "REINFORCE":
        return f"side={payload.get('side','?')}"
    if action_type == "REOPEN_LAST":
        return f"within_hours={payload.get('within_hours', 24)}"
    if action_type == "TIGHTEN_SL":
        return f"by_fraction={payload.get('by_fraction', 0.5)}"
    if action_type == "MODIFY":
        return (
            f"ticket={payload.get('mt5_ticket')} "
            f"new_sl={payload.get('new_sl')} new_tp={payload.get('new_tp')}"
        )
    if action_type == "CLOSE":
        return f"ticket={payload.get('mt5_ticket')} reason={payload.get('reason','')}"
    if action_type == "CLOSE_ALL":
        return f"symbol={payload.get('symbol','?')} reason={payload.get('reason','')}"
    if action_type == "ALERT":
        return f"[{payload.get('level','info').upper()}] {payload.get('text','')}"
    # MOVE_SL_BE, CLOSE_FULL, anything new/empty:
    return ""


def render_action_terminal(
    action_id: int, action_type: str, status: str,
    payload: dict, ea_response: str,
) -> str:
    """One-line DM for an action that just reached a terminal state.

    Status glyph + action type + concise payload summary + EA reason if any.
    Examples:
      "✅ #42 MOVE_SL_BE executed"
      "🚫 #43 CLOSE_PARTIAL rejected — fraction=0.5 (no_open_position)"
      "👀 #44 OPEN watching — BUY XAUUSD entry 4700-4702 sl 4690 tps [4710,4720]"
    """
    glyph = _STATUS_GLYPH.get(status, "•")
    body = _payload_summary(action_type, payload)
    parts = [f"{glyph} #{action_id} {action_type} {status}"]
    if body:
        parts.append(f"— {body}")
    if ea_response:
        parts.append(f"({ea_response})")
    return " ".join(parts)


def render_position_closed(
    *, ticket: int, side: str, symbol: str,
    volume: float | None, original_volume: float | None,
    entry: float | None, sl: float | None, tp: float | None,
    closed_at: str, reason: str,
) -> str:
    """Multi-line DM for a position that MT5 just closed."""
    def _fmt(v):
        return f"{v:.2f}" if v is not None else "-"
    orig_str = (
        f" of {_fmt(original_volume)} orig"
        if original_volume is not None and original_volume != volume
        else ""
    )
    return (
        f"🏁 #{ticket} {side} {symbol} closed @ {reason or 'unknown'}\n"
        f"  vol {_fmt(volume)}{orig_str}\n"
        f"  entry {_fmt(entry)} sl {_fmt(sl)} tp {_fmt(tp)}\n"
        f"  closed_at {closed_at}"
    )
