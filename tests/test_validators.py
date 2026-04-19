import pytest
from pydantic import ValidationError
from src.validators import (
    OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction,
    AIResponse, parse_ai_response,
)
from src.validators import validate_action, ValidationResult
from src.db import connect, init_schema


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


def test_parse_ai_response_strips_markdown_fences():
    raw = '```json\n{"actions":[],"reasoning":"ok"}\n```'
    resp = parse_ai_response(raw)
    assert resp.actions == []
    assert resp.reasoning == "ok"


def test_parse_ai_response_tolerates_preamble():
    raw = 'Here is the result:\n{"actions":[],"reasoning":"ok"}\ndone.'
    resp = parse_ai_response(raw)
    assert resp.reasoning == "ok"


def test_parse_ai_response_error_includes_raw_preview():
    with pytest.raises(ValueError, match="raw_preview"):
        parse_ai_response("")


def test_parse_ai_response_unknown_action_type():
    raw = '{"actions":[{"type":"YOLO"}],"reasoning":"x"}'
    with pytest.raises(ValueError):
        parse_ai_response(raw)


@pytest.fixture
def db_with_position(tmp_path):
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 99001, 'XAUUSD', 'BUY', 0.10, 4865, 4855, 4880, 'open')"
    )
    return conn


def test_validate_open_passes(db_with_position):
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4870, entry_high=4872,
                   tps=[4900], sl=4860)
    r = validate_action(a, db_with_position)
    assert r.ok


def test_validate_close_unknown_ticket_fails(db_with_position):
    a = CloseAction(mt5_ticket=999999)
    r = validate_action(a, db_with_position)
    assert not r.ok
    assert "unknown" in r.error.lower()


def test_validate_close_known_ticket_passes(db_with_position):
    a = CloseAction(mt5_ticket=99001)
    r = validate_action(a, db_with_position)
    assert r.ok


def test_validate_modify_closed_position_fails(db_with_position):
    db_with_position.execute(
        "UPDATE positions SET status='closed' WHERE mt5_ticket=99001"
    )
    a = ModifyAction(mt5_ticket=99001, new_sl=4866)
    r = validate_action(a, db_with_position)
    assert not r.ok


def test_validate_open_duplicate_zone_fails(db_with_position):
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4864, entry_high=4866,  # overlaps existing 4865
                   tps=[4900], sl=4855)
    r = validate_action(a, db_with_position)
    assert not r.ok
    assert "duplicate" in r.error.lower()


def test_validate_close_all_supported_symbol_passes(db_with_position):
    r = validate_action(CloseAllAction(symbol="XAUUSD"), db_with_position)
    assert r.ok


def test_validate_close_all_unsupported_symbol_fails(db_with_position):
    r = validate_action(CloseAllAction(symbol="EURUSD"), db_with_position)
    assert not r.ok
    assert "unsupported" in r.error.lower()
