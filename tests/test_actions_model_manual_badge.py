import json
from src.gui.models.actions_model import manual_badge_text


def test_manual_badge_for_manual_payload():
    payload = json.dumps({"type": "OPEN", "manual": True, "source": "manual_gui"})
    assert manual_badge_text(payload) == "MANUAL"


def test_no_badge_for_telegram_payload():
    payload = json.dumps({"type": "OPEN"})
    assert manual_badge_text(payload) == ""


def test_no_badge_for_bad_json():
    assert manual_badge_text("not json") == ""
