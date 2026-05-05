import json
import pytest
from pydantic import ValidationError
from src.validators import (
    OpenAction, ModifyAction, CloseAction, CloseAllAction, AlertAction,
    AIResponse, parse_ai_response,
    MoveSlBeAction, MoveSlAction, ClosePartialAction, CloseFullAction,
    ReopenLastAction, ReinforceAction, TightenSlAction, ModifyTpsAction,
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


def test_validate_open_passes_when_no_position(tmp_path):
    """OPEN passes only when no position is currently open on the symbol —
    single-position invariant, defense-in-depth alongside the EA's
    CountOurOpenPositions guard."""
    from src.db import connect, init_schema
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4870, entry_high=4872,
                   tps=[4900], sl=4860)
    r = validate_action(a, conn)
    assert r.ok


def test_validate_open_blocked_by_existing_position(db_with_position):
    """Any open position on the symbol blocks a new OPEN, regardless of
    side or zone overlap. Previously only same-side zone overlap was
    checked, which gave a false sense of safety."""
    a = OpenAction(symbol="XAUUSD", side="BUY",
                   entry_low=4870, entry_high=4872,  # NO overlap with 4865
                   tps=[4900], sl=4860)
    r = validate_action(a, db_with_position)
    assert not r.ok
    assert "duplicate" in r.error.lower()


def test_validate_open_blocked_opposite_side(db_with_position):
    """A SELL signal is rejected when a BUY is already open (and vice versa).
    The single-position invariant is symbol-only, not side-aware."""
    a = OpenAction(symbol="XAUUSD", side="SELL",
                   entry_low=4870, entry_high=4872,
                   tps=[4840], sl=4880)
    r = validate_action(a, db_with_position)
    assert not r.ok


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


# ---- Phase 3: MODIFY_TPS + compound-aware OPEN validation ---------


def test_modify_tps_action_valid():
    a = ModifyTpsAction(tps=[4570, 4580, 4600], reason="new signal")
    assert a.type == "MODIFY_TPS"
    assert a.tps == [4570, 4580, 4600]


def test_modify_tps_action_rejects_empty_tps():
    with pytest.raises(ValidationError):
        ModifyTpsAction(tps=[])


def test_modify_tps_action_rejects_too_many_tps():
    with pytest.raises(ValidationError):
        ModifyTpsAction(tps=[4570, 4580, 4590, 4600])


def test_modify_tps_action_rejects_non_positive_price():
    with pytest.raises(ValidationError):
        ModifyTpsAction(tps=[4570, 0])
    with pytest.raises(ValidationError):
        ModifyTpsAction(tps=[-1])


def test_parse_ai_response_dispatches_modify_tps():
    raw = '''{
      "actions":[
        {"type":"MOVE_SL","price":4545},
        {"type":"MODIFY_TPS","tps":[4570,4580],"reason":"new signal arrived"}
      ],
      "category":"signal"
    }'''
    resp = parse_ai_response(raw)
    assert len(resp.actions) == 2
    assert isinstance(resp.actions[1], ModifyTpsAction)
    assert resp.actions[1].tps == [4570, 4580]


def test_validate_modify_tps_passes_through(tmp_path):
    """Phase-3 management action: state-existence checks deferred to EA."""
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    a = ModifyTpsAction(tps=[4570, 4580])
    r = validate_action(a, conn)
    assert r.ok


def test_validate_open_with_close_full_preceding_passes(db_with_position):
    """Compound emit [CLOSE_FULL, OPEN] must NOT reject the OPEN even when
    a position is currently open. The EA executes them in insertion order
    so by the time OPEN claims, the close has run. This is the
    close+reopen rule from the SYSTEM_PROMPT decision tree (RULE A side
    flip; RULE B partials+profit reset)."""
    open_action = OpenAction(
        symbol="XAUUSD", side="SELL",
        entry_low=4870, entry_high=4872, tps=[4840], sl=4880,
    )
    r = validate_action(
        open_action, db_with_position,
        preceding_actions=[CloseFullAction()],
    )
    assert r.ok


def test_validate_open_with_close_action_preceding_passes(db_with_position):
    """Legacy CloseAction also unblocks a following OPEN in a compound."""
    open_action = OpenAction(
        symbol="XAUUSD", side="SELL",
        entry_low=4870, entry_high=4872, tps=[4840], sl=4880,
    )
    r = validate_action(
        open_action, db_with_position,
        preceding_actions=[CloseAction(mt5_ticket=99001)],
    )
    assert r.ok


def test_validate_open_without_close_preceding_still_blocked(db_with_position):
    """The compound shortcut requires a CLOSE in `preceding_actions`. A
    list of unrelated management actions does NOT bypass the guard."""
    open_action = OpenAction(
        symbol="XAUUSD", side="BUY",
        entry_low=4870, entry_high=4872, tps=[4900], sl=4860,
    )
    r = validate_action(
        open_action, db_with_position,
        preceding_actions=[MoveSlBeAction(), ModifyTpsAction(tps=[4870])],
    )
    assert not r.ok
    assert "duplicate" in r.error.lower()


def test_db_accepts_modify_tps_action_type(tmp_path):
    """Phase-3 migration widens actions.action_type CHECK to allow the
    new MODIFY_TPS token after init_schema runs."""
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES(?, '{}')",
        ("MODIFY_TPS",)
    )
    row = conn.execute(
        "SELECT action_type FROM actions WHERE action_type='MODIFY_TPS'"
    ).fetchone()
    assert row is not None
