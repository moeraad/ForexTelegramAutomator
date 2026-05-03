import json
import pytest
from pydantic import ValidationError
from src.validators import (
    OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction,
    AIResponse, parse_ai_response,
    MoveSlBeAction, MoveSlAction, ClosePartialAction, CloseFullAction,
    ReopenLastAction, ReinforceAction, TightenSlAction,
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


# ---- Phase 2: management action models ----------------------------------

def test_parses_move_sl_be():
    a = MoveSlBeAction()
    assert a.type == "MOVE_SL_BE"


def test_parses_move_sl():
    a = MoveSlAction(price=4856.0)
    assert a.type == "MOVE_SL"
    assert a.price == 4856.0


def test_move_sl_rejects_zero_or_negative():
    with pytest.raises(ValidationError):
        MoveSlAction(price=0)
    with pytest.raises(ValidationError):
        MoveSlAction(price=-1)


def test_parses_close_partial_default_fraction():
    a = ClosePartialAction()
    assert a.fraction == 0.5


def test_close_partial_rejects_invalid_fraction():
    with pytest.raises(ValidationError):
        ClosePartialAction(fraction=0)
    with pytest.raises(ValidationError):
        ClosePartialAction(fraction=1)
    with pytest.raises(ValidationError):
        ClosePartialAction(fraction=1.5)


def test_parses_close_full():
    a = CloseFullAction()
    assert a.type == "CLOSE_FULL"


def test_parses_reopen_last_default_window():
    a = ReopenLastAction()
    assert a.within_hours == 24


def test_reopen_last_rejects_zero_or_too_long_window():
    with pytest.raises(ValidationError):
        ReopenLastAction(within_hours=0)
    with pytest.raises(ValidationError):
        ReopenLastAction(within_hours=200)  # > 168 (1 week cap)


def test_parses_reinforce_with_side():
    a = ReinforceAction(side="BUY")
    assert a.side == "BUY"


def test_reinforce_rejects_invalid_side():
    with pytest.raises(ValidationError):
        ReinforceAction(side="LONG")


def test_parses_tighten_sl_default():
    a = TightenSlAction()
    assert a.by_fraction == 0.5


def test_tighten_sl_rejects_invalid_fraction():
    with pytest.raises(ValidationError):
        TightenSlAction(by_fraction=0)
    with pytest.raises(ValidationError):
        TightenSlAction(by_fraction=1.0)


def test_phase2_actions_round_trip_through_parse_ai_response():
    """All 7 new types parse from raw JSON via the AI's response shape."""
    raw = json.dumps({
        "category": "signal",
        "reasoning": "compound message",
        "actions": [
            {"type": "MOVE_SL_BE"},
            {"type": "MOVE_SL", "price": 4856.0},
            {"type": "CLOSE_PARTIAL", "fraction": 0.5},
            {"type": "CLOSE_FULL"},
            {"type": "REOPEN_LAST", "within_hours": 24},
            {"type": "REINFORCE", "side": "BUY"},
            {"type": "TIGHTEN_SL", "by_fraction": 0.5},
        ],
    })
    resp = parse_ai_response(raw)
    assert [a.type for a in resp.actions] == [
        "MOVE_SL_BE", "MOVE_SL", "CLOSE_PARTIAL", "CLOSE_FULL",
        "REOPEN_LAST", "REINFORCE", "TIGHTEN_SL",
    ]


def test_validate_action_passes_through_phase2_types(tmp_path):
    """validate_action returns ok=True for all new types — state guards
    are the EA's responsibility, not the validator's."""
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    for action in [
        MoveSlBeAction(),
        MoveSlAction(price=4856.0),
        ClosePartialAction(),
        CloseFullAction(),
        ReopenLastAction(),
        ReinforceAction(side="BUY"),
        TightenSlAction(),
    ]:
        r = validate_action(action, conn)
        assert r.ok, f"{action.type} should pass through"


def test_db_accepts_phase2_action_types(tmp_path):
    """The CHECK constraint on actions.action_type allows the new tokens
    after init_schema runs the Phase-2 migration."""
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    for atype in (
        "MOVE_SL_BE", "MOVE_SL", "CLOSE_PARTIAL", "CLOSE_FULL",
        "REOPEN_LAST", "REINFORCE", "TIGHTEN_SL",
    ):
        conn.execute(
            "INSERT INTO actions(action_type, payload_json) VALUES(?, '{}')",
            (atype,)
        )
    rows = conn.execute(
        "SELECT action_type FROM actions ORDER BY id"
    ).fetchall()
    assert {r["action_type"] for r in rows} == {
        "MOVE_SL_BE", "MOVE_SL", "CLOSE_PARTIAL", "CLOSE_FULL",
        "REOPEN_LAST", "REINFORCE", "TIGHTEN_SL",
    }
