import pytest
from pydantic import ValidationError
from src.validators import (
    OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction,
    AIResponse, parse_ai_response,
)


def test_open_action_valid():
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866,
                   tps=[4880, 4900], sl=4855)
    assert a.type == "OPEN"
    assert a.tps == [4880, 4900]


def test_open_action_rejects_non_gold():
    with pytest.raises(ValidationError):
        OpenAction(symbol="EURUSD", side="BUY",
                   entry_low=1, entry_high=2, tps=[3], sl=0.5)


def test_open_action_rejects_empty_tps():
    with pytest.raises(ValidationError):
        OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866, tps=[], sl=4855)


def test_open_action_rejects_invalid_side():
    with pytest.raises(ValidationError):
        OpenAction(symbol="XAUUSD", side="HOLD",
                   entry_low=4864, entry_high=4866, tps=[4880], sl=4855)


def test_close_action_requires_ticket():
    a = CloseAction(mt5_ticket=12345, reason="ai")
    assert a.type == "CLOSE"
    with pytest.raises(ValidationError):
        CloseAction(reason="ai")  # missing ticket


def test_parse_ai_response_dispatches_action_type():
    raw = '''{
      "actions": [
        {"type":"OPEN","symbol":"XAUUSD","side":"BUY",
         "entry_low":4864,"entry_high":4866,"tps":[4880],"sl":4855}
      ],
      "reasoning":"clean signal"
    }'''
    resp = parse_ai_response(raw)
    assert len(resp.actions) == 1
    assert isinstance(resp.actions[0], OpenAction)


def test_parse_ai_response_rejects_bad_json():
    with pytest.raises(ValueError):
        parse_ai_response("not json")


def test_parse_ai_response_unknown_action_type():
    raw = '{"actions":[{"type":"YOLO"}],"reasoning":"x"}'
    with pytest.raises(ValueError):
        parse_ai_response(raw)
