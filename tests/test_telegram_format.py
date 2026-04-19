from src.telegram_format import render_action_notification


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
