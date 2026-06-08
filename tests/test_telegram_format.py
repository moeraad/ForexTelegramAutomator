from src.telegram_format import (
    render_action_notification,
    render_action_terminal,
)


def test_render_open_notification():
    payload = {
        "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4864, "entry_high": 4866,
        "tps": [4880, 4900, 4920], "sl": 4855,
    }
    text = render_action_notification(
        action_id=87, action_type="OPEN", payload=payload,
        source_text="GOLD BUY @ 4866-4864", auto_execute_delay_sec=30,
    )
    assert "#87" in text
    assert "BUY" in text
    assert "XAUUSD" in text
    assert "4864" in text and "4866" in text
    assert "30s" in text


def test_render_close_all_notification():
    payload = {"symbol": "XAUUSD", "reason": "trader_emergency_exit"}
    text = render_action_notification(88, "CLOSE_ALL", payload, "Close all", 30)
    assert "CLOSE_ALL" in text
    assert "XAUUSD" in text


def test_render_alert_no_buttons_implied():
    payload = {"level": "warning", "text": "NFP coming"}
    text = render_action_notification(89, "ALERT", payload, "be careful", 0)
    assert "ALERT" in text
    assert "NFP" in text


def test_render_action_terminal_includes_reply_context():
    """When the action's source message was a Telegram reply, the DM
    appends a '↳ in reply to' tail so the operator sees the same
    antecedent the AI used to interpret pronouns."""
    text = render_action_terminal(
        action_id=42, action_type="CANCEL_PENDING", status="executed",
        payload={"symbol": "XAUUSD"}, ea_response="",
        reply_parent_text="Xauusd sell limit 4575.84 / SL 4582 / TP 4547",
    )
    assert "#42" in text
    assert "CANCEL_PENDING" in text
    assert "in reply to" in text
    assert "sell limit 4575.84" in text


def test_render_action_terminal_no_reply_when_not_a_reply():
    """No tail when reply_parent_text is None — normal DMs are unchanged."""
    text = render_action_terminal(
        action_id=43, action_type="MOVE_SL_BE", status="executed",
        payload={}, ea_response="",
    )
    assert "in reply to" not in text


def test_render_action_terminal_truncates_long_reply():
    long_parent = "x" * 500
    text = render_action_terminal(
        action_id=44, action_type="MOVE_SL_BE", status="executed",
        payload={}, ea_response="",
        reply_parent_text=long_parent,
    )
    assert "in reply to" in text
    assert "..." in text  # truncation marker
    # Reply tail itself is capped, not the whole message.
    assert text.count("x") <= 200


# ---- image-corrected warning note (Task 5) ----------------------------------

def _open_payload(image_corrected: bool = False, reason: str = "") -> dict:
    p = {
        "symbol": "XAUUSD", "side": "SELL",
        "entry_low": 4471.27, "entry_high": 4471.27,
        "sl": 4481.57, "tps": [4419.63],
    }
    if image_corrected:
        p["image_corrected"] = True
        p["image_fallback_reason"] = reason or "[partial] inconsistent SELL setup"
    return p


def test_open_dm_has_warning_note_when_image_corrected():
    text = render_action_notification(
        action_id=42,
        action_type="OPEN",
        payload=_open_payload(image_corrected=True),
        source_text="Xauusd sell limit 4471.27 SL 4451.87 Tp 4419.63",
        auto_execute_delay_sec=5,
    )
    assert "⚠️" in text
    assert "corrected from chart image" in text.lower() or "chart image" in text.lower()
    assert "4481.57" in text  # corrected SL present


def test_open_dm_no_warning_note_when_not_image_corrected():
    text = render_action_notification(
        action_id=42,
        action_type="OPEN",
        payload=_open_payload(image_corrected=False),
        source_text="Xauusd sell limit 4471.27 SL 4481.57 Tp 4419.63",
        auto_execute_delay_sec=5,
    )
    assert "corrected from chart image" not in text.lower()


def test_image_corrected_note_shows_original_text():
    reason = "[partial] inconsistent SELL setup: SL 4451 is below entry 4471"
    p = _open_payload(image_corrected=True, reason=reason)
    text = render_action_notification(
        action_id=1, action_type="OPEN", payload=p,
        source_text="test", auto_execute_delay_sec=0,
    )
    assert "inconsistent" in text.lower() or "wrong side" in text.lower() or "below entry" in text.lower()


def test_manual_trade_gets_prefix():
    from src.telegram_format import render_manual_prefix
    assert render_manual_prefix({"manual": True}) == "🛠 MANUAL "
    assert render_manual_prefix({}) == ""
    assert render_manual_prefix({"manual": False}) == ""
