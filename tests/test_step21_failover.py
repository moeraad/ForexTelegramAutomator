"""Failover destinations tests (Step 21 of multi-channel plan).

Covers:
  - Route.fallback_destination_id field defaults to ""
  - with_route_failover pure transform: validation + circular-fallback
    rejection
  - JSON round-trip preserves the field
  - _resolve_dispatch_for_channel attaches fallback target when configured
  - _post_one (in shared_listener) retries on fallback when primary fails;
    failover_from_destination_id propagates through the wire
  - process_message tags OPEN payload with failover_from when the action
    came in via a failover request
  - Log rows from failover requests are tagged so the audit trail is
    greppable
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src import config_v2
from src.ai import AICallResult, AIClient
from src.config_v2 import (
    Account,
    Bot,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
    with_route_added,
    with_route_failover,
)
from src.db import connect, init_schema
from src.listener import _ApiDispatchTarget
from src.profile_context import ProfileContext
from src.shared_listener import (
    _build_target_for_destination,
    _resolve_dispatch_for_channel,
)
from src.validators import AIResponse, OpenAction


# ---- schema -----------------------------------------------------------


def _cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="a", name="A", phone="",
                          session_path="", service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="a",
                          chat_id=-1001, profile_id="p"),),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B"),),
        routes=(),
    )


def test_route_fallback_defaults_to_empty():
    r = Route(id="r", channel_id="ch_a", destination_id="dest_x")
    assert r.fallback_destination_id == ""


# ---- with_route_failover ---------------------------------------------


def _cfg_with_route() -> ConfigV2:
    return with_route_added(
        _cfg(), channel_id="ch_a", destination_id="dest_x", route_id="r1",
    )


def test_with_route_failover_sets_fallback():
    cfg = with_route_failover(_cfg_with_route(), "r1", "dest_y")
    assert cfg.route("r1").fallback_destination_id == "dest_y"


def test_with_route_failover_clears_when_empty_string():
    cfg = with_route_failover(_cfg_with_route(), "r1", "dest_y")
    cfg = with_route_failover(cfg, "r1", "")
    assert cfg.route("r1").fallback_destination_id == ""


def test_with_route_failover_rejects_unknown_route():
    with pytest.raises(ValueError, match="Unknown route"):
        with_route_failover(_cfg_with_route(), "r_nope", "dest_y")


def test_with_route_failover_rejects_unknown_fallback_destination():
    with pytest.raises(ValueError, match="Unknown fallback destination"):
        with_route_failover(_cfg_with_route(), "r1", "dest_nope")


def test_with_route_failover_rejects_circular_fallback():
    """A route pointing to dest_x can't fall back to dest_x — it would
    just retry the same dead API and gain nothing."""
    with pytest.raises(ValueError, match="circular fallback"):
        with_route_failover(_cfg_with_route(), "r1", "dest_x")


# ---- JSON round-trip -------------------------------------------------


def test_save_load_round_trip_preserves_fallback(tmp_path: Path):
    cfg_path = tmp_path / "stacks_config.json"
    cfg = with_route_failover(_cfg_with_route(), "r1", "dest_y")
    config_v2.save_v2(cfg, cfg_path)
    reloaded = config_v2.load_v2(cfg_path)
    assert reloaded.route("r1").fallback_destination_id == "dest_y"


# ---- _build_target_for_destination + _resolve_dispatch_for_channel ----


def _cfg_with_two_dests_and_routes(tmp_path: Path) -> ConfigV2:
    """Two destinations + one route ch_a → dest_x with dest_y as fallback."""
    db_x = tmp_path / "dest_x.db"
    db_y = tmp_path / "dest_y.db"
    db_x.touch()
    db_y.touch()
    return ConfigV2(
        accounts=(Account(id="a", name="A", phone="",
                          session_path="", service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="a",
                          chat_id=-1001, profile_id="p"),),
        destinations=(
            Destination(id="dest_x", name="X", db_path=str(db_x),
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path=str(db_y),
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B"),),
        routes=(Route(
            id="route_ax", channel_id="ch_a", destination_id="dest_x",
            fallback_destination_id="dest_y",
        ),),
    )


def test_resolve_dispatch_attaches_fallback_target(tmp_path: Path):
    cfg = _cfg_with_two_dests_and_routes(tmp_path)
    targets = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    assert len(targets) == 1
    primary_path, primary_target = targets[0]
    # Primary points at dest_x.
    assert primary_target.destination_id == "dest_x"
    assert "8765" in primary_target.url
    # Fallback attached, pointing at dest_y.
    assert primary_target.fallback is not None
    assert primary_target.fallback.destination_id == "dest_y"
    assert "8766" in primary_target.fallback.url
    # Fallback carries the SAME route_id + channel_id so analytics
    # attribute the failover to the original route.
    assert primary_target.fallback.route_id == primary_target.route_id
    assert primary_target.fallback.channel_id == primary_target.channel_id


def test_resolve_dispatch_omits_fallback_when_unconfigured(tmp_path: Path):
    cfg = _cfg_with_two_dests_and_routes(tmp_path)
    # Remove the fallback by replacing the route.
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels, destinations=cfg.destinations,
        bots=cfg.bots,
        routes=(Route(id="route_ax", channel_id="ch_a",
                      destination_id="dest_x"),),
    )
    targets = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    primary_target = targets[0][1]
    assert primary_target.fallback is None


def test_resolve_dispatch_handles_missing_fallback_destination(tmp_path: Path):
    """If fallback_destination_id points at a deleted dest, primary still
    schedules — failover just doesn't attach."""
    cfg = _cfg_with_two_dests_and_routes(tmp_path)
    # Drop dest_y from destinations to simulate a stale fallback ref.
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels,
        destinations=(cfg.destinations[0],),  # only dest_x
        bots=cfg.bots, routes=cfg.routes,
    )
    targets = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    assert len(targets) == 1
    primary_target = targets[0][1]
    assert primary_target.destination_id == "dest_x"
    assert primary_target.fallback is None


# ---- _post_incoming_message wire: failover field on body --------------


def test_post_incoming_message_includes_failover_field():
    """Confirm the wire body carries failover_from_destination_id."""
    from src.listener import _post_incoming_message
    target = _ApiDispatchTarget(
        url="http://127.0.0.1:8765/incoming_message",
        channel_id="ch_a", token="",
        route_id="route_ax", destination_id="dest_y",
    )
    captured: dict = {}

    class _FakeResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=10):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        ok = _post_incoming_message(
            target,
            tg_chat_id=-1, tg_message_id=1, text="x",
            sender="x", received_at="2026-05-24T12:00:00+00:00",
            is_backfill=False,
            failover_from_destination_id="dest_x",
        )
    assert ok is True
    assert captured["body"]["failover_from_destination_id"] == "dest_x"


def test_post_incoming_message_omits_failover_when_empty():
    """Primary POSTs send an empty failover field — receiver knows it's
    not a failover."""
    from src.listener import _post_incoming_message
    target = _ApiDispatchTarget(
        url="http://127.0.0.1:8765/incoming_message",
        channel_id="ch_a", token="",
        route_id="route_ax", destination_id="dest_x",
    )
    captured: dict = {}

    class _FakeResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout=10):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    with patch("urllib.request.urlopen", side_effect=_fake_urlopen):
        _post_incoming_message(
            target,
            tg_chat_id=-1, tg_message_id=1, text="x",
            sender="x", received_at="2026-05-24T12:00:00+00:00",
            is_backfill=False,
        )
    assert captured["body"]["failover_from_destination_id"] == ""


# ---- process_message tags OPEN payload + log rows on failover --------


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


def _seed_cfg_with_route(appdata: Path, db_path: Path) -> None:
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
        routes=(Route(id="route_ax", channel_id="ch_a",
                      destination_id="dest_x"),),
    )
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, cfg_path)


def test_orchestrator_tags_open_payload_with_failover_from(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_route(tmp_path / "appdata", db_path)
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
        failover_from_destination_id="dest_primary",
    )
    assert ids
    row = conn.execute(
        "SELECT payload_json FROM actions WHERE id=?", (ids[0],),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["failover_from"] == "dest_primary"


def test_orchestrator_log_rows_tagged_with_failover_from(
    monkeypatch, tmp_path: Path,
):
    """ai_calls.jsonl rows from a failover request carry failover_from
    so the audit grep tells the operator which messages used failover."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_route(tmp_path / "appdata", db_path)
    log_path = tmp_path / "ai.jsonl"

    from src.orchestrator import process_message
    process_message(
        conn, MagicMock(spec=AIClient),
        tg_message_id=1, chat_id=-1001, sender="x", text="buy",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=_make_profile(tmp_path),
        source_channel_id="ch_a", route_id="route_ax",
        halted=True,
        failover_from_destination_id="dest_primary",
    )
    rows = [
        json.loads(l) for l in log_path.read_text(encoding="utf-8").splitlines()
    ]
    halt_rows = [r for r in rows if r.get("stage") == "halt"]
    assert halt_rows
    assert halt_rows[0]["failover_from"] == "dest_primary"


def test_orchestrator_omits_failover_when_not_a_failover(
    monkeypatch, tmp_path: Path,
):
    """Primary POSTs don't add failover_from to the OPEN payload."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    _seed_cfg_with_route(tmp_path / "appdata", db_path)
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
    row = conn.execute(
        "SELECT payload_json FROM actions WHERE id=?", (ids[0],),
    ).fetchone()
    payload = json.loads(row["payload_json"])
    assert "failover_from" not in payload
