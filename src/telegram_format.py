def render_manual_prefix(payload: dict) -> str:
    """Prefix manual (GUI-placed) trades so DMs are unambiguous."""
    return "🛠 MANUAL " if isinstance(payload, dict) and payload.get("manual") else ""


def _reply_context_block(reply_parent_text: str | None) -> str:
    """Return a short '↳ in reply to' block for DM notifications, or
    an empty string when the action's source message wasn't a reply or
    the parent isn't available. Helps the operator see the same
    antecedent the AI used (e.g. "cancel that order" was a reply to
    a specific limit-order signal)."""
    if not reply_parent_text:
        return ""
    snippet = reply_parent_text.strip()
    if len(snippet) > 160:
        snippet = snippet[:160] + "..."
    return f"\n↳ in reply to: \"{snippet}\""


def render_action_notification(
    action_id: int, action_type: str, payload: dict,
    source_text: str, auto_execute_delay_sec: int,
    reply_parent_text: str | None = None,
) -> str:
    when = "now" if auto_execute_delay_sec <= 0 else f"in {auto_execute_delay_sec}s"
    reply = _reply_context_block(reply_parent_text)
    if action_type == "OPEN":
        tps = " / ".join(str(t) for t in payload.get("tps", []))
        image_note = ""
        if payload.get("image_corrected"):
            reason = payload.get("image_fallback_reason", "")
            reason_snippet = (reason[:100] + "…") if len(reason) > 100 else reason
            image_note = (
                f"⚠️ Values corrected from chart image\n"
                f"   (text had: {reason_snippet})\n\n"
            )
        return (
            f"{render_manual_prefix(payload)}🟢 NEW SIGNAL #{action_id}  (auto-execute {when})\n\n"
            f"{image_note}"
            f"OPEN {payload['side']} {payload['symbol']}\n"
            f"Entry: {payload['entry_low']}–{payload['entry_high']}\n"
            f"SL:    {payload['sl']}\n"
            f"TPs:   {tps}\n\n"
            f"Source: \"{source_text[:120]}\"{reply}"
        )
    if action_type == "MODIFY":
        return (
            f"🔧 MODIFY #{action_id}  (auto {when})\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"new_sl: {payload.get('new_sl')}  new_tp: {payload.get('new_tp')}\n\n"
            f"Source: \"{source_text[:120]}\"{reply}"
        )
    if action_type == "CLOSE":
        return (
            f"🔴 CLOSE #{action_id}  (auto {when})\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\"{reply}"
        )
    if action_type == "CLOSE_ALL":
        return (
            f"🔴 CLOSE_ALL #{action_id}  (auto {when})\n"
            f"Symbol: {payload['symbol']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\"{reply}"
        )
    if action_type == "ALERT":
        level = payload.get("level", "info").upper()
        return f"⚠️ ALERT [{level}] #{action_id}\n{payload.get('text','')}{reply}"
    return f"#{action_id} {action_type}: {payload}{reply}"


# ---- Phase 5: terminal-state + MT5-event notifications ------------------
#
# Per project policy (fully automated, no approval gate): the bot only
# notifies the operator about ACTIONS THAT HAPPENED and MT5 EVENTS.
# These two renderers produce the bodies for those DMs.

_STATUS_GLYPH = {
    "executed": "✅",
    "failed":   "❌",
    "rejected": "🚫",
}


def _payload_summary(
    action_type: str, payload: dict, *,
    actual_entry: float | None = None,
    actual_volume: float | None = None,
) -> str:
    """One-line summary of an action's payload for terminal notifications.

    `actual_entry` (optional, OPEN only): the broker-reported fill price
    for the position created by this action. When supplied, the OPEN
    summary shows BOTH the channel signal's entry zone (intent) and the
    actual fill price (reality), so the operator can see at a glance how
    far the chase landed from the analyst's plan.

    `actual_volume` (optional, OPEN only): broker-reported filled lot
    size. EA computes it at fill time via LotsPer100Balance / risk cap,
    so it's only known after execution. Surfaced in the OPEN summary as
    "lots 0.61". Combined format:
        "BUY XAUUSD lots 0.61 signal-entry 4555-4553 actual 4554.20 sl ..."
    """
    if action_type == "OPEN":
        tps = ",".join(str(t) for t in payload.get("tps", []))
        actual_part = (
            f" actual {actual_entry:.2f}" if actual_entry is not None else ""
        )
        lots_part = (
            f" lots {actual_volume:.2f}" if actual_volume is not None else ""
        )
        return (
            f"{payload.get('side','?')} {payload.get('symbol','XAUUSD')}"
            f"{lots_part} "
            f"signal-entry {payload.get('entry_low')}-{payload.get('entry_high')}"
            f"{actual_part} "
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
    *,
    actual_entry: float | None = None,
    actual_volume: float | None = None,
    source_channel_name: str = "",
    reply_parent_text: str | None = None,
) -> str:
    """One-line DM for an action that just reached a terminal state.

    Status glyph + action type + concise payload summary + EA reason if any.
    Examples:
      "✅ #42 MOVE_SL_BE executed"
      "🚫 #43 CLOSE_PARTIAL rejected — fraction=0.5 (no_open_position)"
      "✅ #137 OPEN executed — BUY XAUUSD lots 0.61 signal-entry 4575-4577 actual 4576.42 sl 4566 tps [4588,4600,4610]"
      "✅ #137 OPEN executed [from: SMC Daily] — BUY XAUUSD ..."

    Args:
        actual_entry: optional broker fill price (OPEN only).
        actual_volume: optional broker-filled lot size (OPEN only).
        source_channel_name: Step 12 — when the destination receives
            messages from multiple channels (aggregate routing), prefix
            the DM with the originating channel so the operator can tell
            them apart. Pass "" (default) for single-channel destinations.
    """
    glyph = _STATUS_GLYPH.get(status, "•")
    body = _payload_summary(
        action_type, payload,
        actual_entry=actual_entry,
        actual_volume=actual_volume,
    )
    head = f"{glyph} #{action_id} {action_type} {status}"
    if source_channel_name:
        head += f" [from: {source_channel_name}]"
    parts = [head]
    if body:
        parts.append(f"— {body}")
    if ea_response:
        parts.append(f"({ea_response})")
    line = " ".join(parts)
    # When the source message was a Telegram reply, append a short
    # "↳ in reply to" tail so the operator sees the same antecedent the
    # AI used (e.g. CANCEL_PENDING fired because the operator's
    # "cancel that order" was a reply to a specific limit-order signal).
    reply_tail = _reply_context_block(reply_parent_text)
    if reply_tail:
        line += reply_tail
    return line


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
