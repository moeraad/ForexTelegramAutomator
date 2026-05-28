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
