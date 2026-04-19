def render_action_notification(
    action_id: int, action_type: str, payload: dict,
    source_text: str, auto_execute_delay_sec: int,
) -> str:
    if action_type == "OPEN":
        tps = " / ".join(str(t) for t in payload.get("tps", []))
        return (
            f"🟢 NEW SIGNAL #{action_id}  (auto-execute in {auto_execute_delay_sec}s)\n\n"
            f"OPEN {payload['side']} {payload['symbol']}\n"
            f"Entry: {payload['entry_low']}–{payload['entry_high']}\n"
            f"SL:    {payload['sl']}\n"
            f"TPs:   {tps}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "MODIFY":
        return (
            f"🔧 MODIFY #{action_id}  (auto in {auto_execute_delay_sec}s)\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"new_sl: {payload.get('new_sl')}  new_tp: {payload.get('new_tp')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "CLOSE":
        return (
            f"🔴 CLOSE #{action_id}  (auto in {auto_execute_delay_sec}s)\n"
            f"Ticket: {payload['mt5_ticket']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "CLOSE_ALL":
        return (
            f"🔴 CLOSE_ALL #{action_id}  (auto in {auto_execute_delay_sec}s)\n"
            f"Symbol: {payload['symbol']}\n"
            f"Reason: {payload.get('reason','')}\n\n"
            f"Source: \"{source_text[:120]}\""
        )
    if action_type == "ALERT":
        level = payload.get("level", "info").upper()
        return f"⚠️ ALERT [{level}] #{action_id}\n{payload.get('text','')}"
    return f"#{action_id} {action_type}: {payload}"
