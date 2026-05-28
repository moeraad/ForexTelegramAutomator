"""Per-route rule tests (Step 20 of multi-channel plan).

Covers:
  - Route schema extended with 5 rule fields, all back-compat-default
  - ``with_route_rules`` pure transform: validation, partial updates
  - JSON round-trip preserves the new fields + coerces list → tuple for
    ``allowed_action_types``
  - ``evaluate_pre_persistence_rules``: allowed_action_types filter,
    time_of_day_filter (including overnight wrap)
  - Orchestrator drops AI actions when route rules reject them; logs
    ``stage=route_rule decision=drop`` to ai_calls.jsonl
  - OPEN payload gains ``route_max_lots`` (and friends) when set
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import config_v2
from src.ai import AICallResult, AIClient
from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
    with_route_added,
    with_route_rules,
)
from src.db import connect, init_schema
from src.profile_context import ProfileContext
from src.route_rules import _in_time_window, evaluate_pre_persistence_rules
from src.validators import AIResponse, CloseFullAction, MoveSlBeAction, OpenAction


# ---- schema: default values ---------------------------------------------


def _cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="a", name="A", phone="",
                          session_path="", service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(id="dest_x", name="X", db_path="/x.db",
                                  api_host="127.0.0.1", api_port=8765,
                                  service_name="CT-Api-X"),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B"),),
        routes=(),
    )


def test_route_defaults_have_disabled_rules():
    route = Route(id="r", channel_id="ch_a", destination_id="dest_x")
    assert route.max_lots == 0.0
    assert route.min_account_balance == 0.0
    assert route.skip_if_drawdown_pct == 0.0
    assert route.allowed_action_types == ()
    assert route.time_of_day_filter == ""


# ---- with_route_rules ---------------------------------------------------


def _cfg_with_route() -> ConfigV2:
    return with_route_added(
        _cfg(), channel_id="ch_a", destination_id="dest_x", route_id="r1",
    )


def test_with_route_rules_sets_max_lots():
    cfg = with_route_rules(_cfg_with_route(), "r1", max_lots=0.5)
    assert cfg.route("r1").max_lots == 0.5


def test_with_route_rules_partial_update_leaves_others_default():
    """Pass only one rule → other rules stay at defaults."""
    cfg = with_route_rules(
        _cfg_with_route(), "r1",
        allowed_action_types=("OPEN", "MOVE_SL_BE"),
    )
    r = cfg.route("r1")
    assert r.allowed_action_types == ("OPEN", "MOVE_SL_BE")
    assert r.max_lots == 0.0


def test_with_route_rules_sets_all_fields():
    cfg = with_route_rules(
        _cfg_with_route(), "r1",
        max_lots=1.0,
        min_account_balance=1000.0,
        skip_if_drawdown_pct=10.0,
        allowed_action_types=("OPEN",),
        time_of_day_filter="08:00-20:00",
    )
    r = cfg.route("r1")
    assert r.max_lots == 1.0
    assert r.min_account_balance == 1000.0
    assert r.skip_if_drawdown_pct == 10.0
    assert r.allowed_action_types == ("OPEN",)
    assert r.time_of_day_filter == "08:00-20:00"


def test_with_route_rules_rejects_unknown_route():
    with pytest.raises(ValueError, match="Unknown route"):
        with_route_rules(_cfg_with_route(), "r_nope", max_lots=1.0)


def test_with_route_rules_rejects_negative_max_lots():
    with pytest.raises(ValueError, match="max_lots"):
        with_route_rules(_cfg_with_route(), "r1", max_lots=-1.0)


def test_with_route_rules_rejects_negative_min_account_balance():
    with pytest.raises(ValueError, match="min_account_balance"):
        with_route_rules(_cfg_with_route(), "r1", min_account_balance=-1.0)


def test_with_route_rules_rejects_out_of_range_drawdown():
    with pytest.raises(ValueError, match="skip_if_drawdown_pct"):
        with_route_rules(_cfg_with_route(), "r1", skip_if_drawdown_pct=150.0)


def test_with_route_rules_rejects_empty_action_type_entry():
    with pytest.raises(ValueError, match="allowed_action_types"):
        with_route_rules(
            _cfg_with_route(), "r1", allowed_action_types=("OPEN", ""),
        )


def test_with_route_rules_rejects_malformed_time_window():
    with pytest.raises(ValueError, match="time_of_day_filter"):
        with_route_rules(_cfg_with_route(), "r1", time_of_day_filter="garbage")


# ---- JSON round-trip ----------------------------------------------------


def test_save_load_round_trip_preserves_rules(tmp_path: Path):
    """asdict serializes tuple → list; loader coerces back to tuple."""
    cfg_path = tmp_path / "stacks_config.json"
    cfg = with_route_rules(
        _cfg_with_route(), "r1",
        max_lots=0.25, allowed_action_types=("OPEN", "MOVE_SL_BE"),
        time_of_day_filter="09:00-17:00",
    )
    config_v2.save_v2(cfg, cfg_path)
    reloaded = config_v2.load_v2(cfg_path)
    r = reloaded.route("r1")
    assert r.max_lots == 0.25
    # allowed_action_types must come back as a tuple (immutability invariant).
    assert isinstance(r.allowed_action_types, tuple)
    assert r.allowed_action_types == ("OPEN", "MOVE_SL_BE")
    assert r.time_of_day_filter == "09:00-17:00"


# ---- evaluate_pre_persistence_rules: allowed_action_types ----------------


def _route_with(**rules) -> Route:
    return Route(id="r", channel_id="ch", destination_id="dest", **rules)


def test_evaluate_allows_when_no_rules_set():
    allowed, reason = evaluate_pre_persistence_rules(
        _route_with(), action_type="OPEN",
    )
    assert allowed is True
    assert reason == ""


def test_evaluate_allows_when_action_type_in_whitelist():
    allowed, _ = evaluate_pre_persistence_rules(
        _route_with(allowed_action_types=("OPEN", "MOVE_SL_BE")),
        action_type="OPEN",
    )
    assert allowed is True


def test_evaluate_rejects_when_action_type_not_in_whitelist():
    allowed, reason = evaluate_pre_persistence_rules(
        _route_with(allowed_action_types=("OPEN",)),
        action_type="CLOSE_FULL",
    )
    assert allowed is False
    assert "route_rule_action_type_filtered" in reason
    assert "CLOSE_FULL" in reason
    assert "OPEN" in reason


# ---- evaluate_pre_persistence_rules: time_of_day_filter -----------------


def _utc(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 5, 24, hour, minute, tzinfo=timezone.utc)


def test_evaluate_allows_inside_normal_window():
    allowed, _ = evaluate_pre_persistence_rules(
        _route_with(time_of_day_filter="08:00-20:00"),
        action_type="OPEN", now_utc=_utc(12),
    )
    assert allowed is True


def test_evaluate_rejects_outside_normal_window():
    allowed, reason = evaluate_pre_persistence_rules(
        _route_with(time_of_day_filter="08:00-20:00"),
        action_type="OPEN", now_utc=_utc(22),
    )
    assert allowed is False
    assert "route_rule_time_window" in reason
    assert "22:00" in reason


def test_evaluate_rejects_exactly_at_end_boundary():
    """end is exclusive: 20:00 falls OUTSIDE 08:00-20:00 (matches 'until')."""
    allowed, _ = evaluate_pre_persistence_rules(
        _route_with(time_of_day_filter="08:00-20:00"),
        action_type="OPEN", now_utc=_utc(20),
    )
    assert allowed is False


def test_evaluate_allows_at_start_boundary():
    allowed, _ = evaluate_pre_persistence_rules(
        _route_with(time_of_day_filter="08:00-20:00"),
        action_type="OPEN", now_utc=_utc(8),
    )
    assert allowed is True


def test_in_time_window_wrap_around_overnight():
    """23:00-01:00 spans midnight: 23:30 and 00:30 both match; 12:00 doesn't."""
    assert _in_time_window(_utc(23, 30), "23:00-01:00") is True
    assert _in_time_window(_utc(0, 30), "23:00-01:00") is True
    assert _in_time_window(_utc(12), "23:00-01:00") is False


def test_in_time_window_empty_filter_allows():
    assert _in_time_window(_utc(3), "") is True


def test_in_time_window_malformed_fails_open():
    """Bad filter strings don't drop traffic — they're ignored (fail-open)."""
    assert _in_time_window(_utc(3), "garbage") is True


# ---- orchestrator: drops AI actions on rule reject ----------------------


def _make_profile(tmp_path: Path) -> ProfileContext:
    from src.ai import _render_system_prompt_from_data
    from src.ai_triage import _render_triage_prompt_from_data
    data = {
        "header": "H", "vocabulary_table": "v", "compound_messages": "c",
        "commentary_filter": "f", "directional_command_flow": "d",
        "worked_examples": "e", "shorthand_decode_example": "s",
        "promo_indicators": "", "noise_patterns": "", "triage_keep_triggers": "",
        "symbol": "XAUUSD",
    }
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return ProfileContext(
        name="p", path=p, data=data,
        system_prompt=_render_system_prompt_from_data(data),
        triage_prompt=_render_triage_prompt_from_data(data),
        symbol="XAUUSD",
    )


def _seed_cfg_with_rules(
    appdata: Path, *,
    db_path: Path,
    allowed_action_types: tuple[str, ...] = (),
    max_lots: float = 0.0,
) -> None:
    cfg = ConfigV2(
        accounts=(Account(id="a", name="A", phone="",
                          session_path="", service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="a",
                          chat_id=-1001, profile_id="p"),),
        destinations=(Destination(id="dest_x", name="X", db_path=str(db_path),
                                  api_host="127.0.0.1", api_port=8765,
                                  service_name="CT-Api-X"),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B"),),
        routes=(Route(
            id="route_ax", channel_id="ch_a", destination_id="dest_x",
            allowed_action_types=allowed_action_types,
            max_lots=max_lots,
        ),),
    )
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, cfg_path)


def test_orchestrator_drops_action_filtered_by_route_rule(
    monkeypatch, tmp_path: Path,
):
    """Route restricts to OPEN only; AI emits CLOSE_FULL → action dropped,
    nothing persisted, log row records the drop."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_rules(
        tmp_path / "appdata", db_path=db_path,
        allowed_action_types=("OPEN",),
    )
    log_path = tmp_path / "ai.jsonl"

    ai = MagicMock(spec=AIClient)
    ai.call.return_value = AICallResult(
        response=AIResponse(actions=[CloseFullAction()], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 10, "output_tokens": 5,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )

    from src.orchestrator import process_message
    ids = process_message(
        conn, ai,
        tg_message_id=1, chat_id=-1001, sender="x", text="close",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
    )
    # Action was dropped → no row inserted.
    assert ids == []
    n = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert n == 0
    # Log row records the drop with the reason code.
    log_rows = [
        json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()
    ]
    drops = [r for r in log_rows if r.get("stage") == "route_rule"]
    assert len(drops) == 1
    assert drops[0]["decision"] == "drop"
    assert "route_rule_action_type_filtered" in drops[0]["reason"]


def test_orchestrator_allows_action_when_in_whitelist(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_rules(
        tmp_path / "appdata", db_path=db_path,
        allowed_action_types=("OPEN",),
    )
    log_path = tmp_path / "ai.jsonl"

    ai = MagicMock(spec=AIClient)
    ai.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol="XAUUSD", side="BUY",
            entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 10, "output_tokens": 5,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )

    from src.orchestrator import process_message
    ids = process_message(
        conn, ai,
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
    )
    # OPEN is on the whitelist → action persists.
    assert len(ids) == 1
    row = conn.execute(
        "SELECT action_type, status FROM actions WHERE id=?", (ids[0],),
    ).fetchone()
    assert row["action_type"] == "OPEN"
    # No route_rule drops in log.
    log_rows = [
        json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()
    ]
    drops = [r for r in log_rows if r.get("stage") == "route_rule"]
    assert drops == []


# ---- max_lots propagation into OPEN payload -----------------------------


def test_open_payload_gains_route_max_lots_when_set(
    monkeypatch, tmp_path: Path,
):
    """max_lots > 0 on the route → OPEN payload gains 'route_max_lots'."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_rules(
        tmp_path / "appdata", db_path=db_path, max_lots=0.5,
    )
    log_path = tmp_path / "ai.jsonl"

    ai = MagicMock(spec=AIClient)
    ai.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol="XAUUSD", side="BUY",
            entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 10, "output_tokens": 5,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )

    from src.orchestrator import process_message
    ids = process_message(
        conn, ai,
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
    )
    assert ids
    row = conn.execute(
        "SELECT payload_json FROM actions WHERE id=?", (ids[0],),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["route_max_lots"] == 0.5
    # Original OPEN fields still present.
    assert payload["symbol"] == "XAUUSD"
    assert payload["side"] == "BUY"


def test_open_payload_omits_route_max_lots_when_unset(
    monkeypatch, tmp_path: Path,
):
    """max_lots=0 (default) → payload has NO route_max_lots key."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_rules(tmp_path / "appdata", db_path=db_path)
    log_path = tmp_path / "ai.jsonl"

    ai = MagicMock(spec=AIClient)
    ai.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol="XAUUSD", side="BUY",
            entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 10, "output_tokens": 5,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=10,
    )

    from src.orchestrator import process_message
    ids = process_message(
        conn, ai,
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
    )
    assert ids
    row = conn.execute(
        "SELECT payload_json FROM actions WHERE id=?", (ids[0],),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert "route_max_lots" not in payload
