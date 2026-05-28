"""Aggregate-routing tests (Step 12 of multi-channel plan).

Validates: ONE destination can receive messages from N different channels,
each with its OWN profile. The per-channel profile resolver in
``api_helpers._resolve_profile_for_channel`` picks the correct profile
based on the request's ``channel_id``; the DM renderer labels each
event with the originating channel name.

Covers:
  - Per-channel profile resolution (the central Step 12 fix)
  - Fallback chain when v2 config unavailable or channel unknown
  - mtime-aware refresh chains through (Day-2 invariant preserved)
  - DM rendering shows `[from: <channel-name>]` for aggregate scenarios
  - Single-channel destinations don't gain channel-label noise
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src import config, config_v2
from src.ai import AICallResult, AIClient
from src.api import build_app
from src.api_helpers import _resolve_profile_for_channel
from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)
from src.db import connect, init_schema
from src.profile_context import ProfileContext, clear_cache as clear_profile_cache
from src.validators import AIResponse, OpenAction


# ---- Helpers --------------------------------------------------------------


def _make_profile_json(path: Path, *, header: str = "DEFAULT HEADER") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "header": header,
        "vocabulary_table": "v", "compound_messages": "c",
        "commentary_filter": "f", "directional_command_flow": "d",
        "worked_examples": "e", "shorthand_decode_example": "s",
        "promo_indicators": "", "noise_patterns": "", "triage_keep_triggers": "",
        "symbol": "XAUUSD",
    }), encoding="utf-8")


def _profile_ctx(path: Path) -> ProfileContext:
    from src.ai import _render_system_prompt_from_data
    from src.ai_triage import _render_triage_prompt_from_data
    data = json.loads(path.read_text(encoding="utf-8"))
    return ProfileContext(
        name=path.stem, path=path, data=data,
        system_prompt=_render_system_prompt_from_data(data),
        triage_prompt=_render_triage_prompt_from_data(data),
        symbol=data.get("symbol", "XAUUSD"),
    )


def _write_two_channel_cfg(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Write a v2 config with 2 channels (different profiles) → 1 destination.

    Returns (appdata, profile_a_path, profile_b_path).
    """
    appdata = tmp_path / "appdata"
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    db_path = appdata / "CopyTrades" / "dest_main" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)

    profile_a = appdata / "CopyTrades" / "profiles" / "a.json"
    profile_b = appdata / "CopyTrades" / "profiles" / "b.json"
    _make_profile_json(profile_a, header="HEADER FROM PROFILE A")
    _make_profile_json(profile_b, header="HEADER FROM PROFILE B")

    cfg = ConfigV2(
        accounts=(Account(
            id="acc_primary", name="Primary", phone="+961",
            session_path="x.session",
            service_name="CT-Listener-acc_primary",
        ),),
        profiles=(
            Profile(id="prof_a", name="Analyst A", path=str(profile_a),
                    language="en", symbol="XAUUSD"),
            Profile(id="prof_b", name="Analyst B", path=str(profile_b),
                    language="en", symbol="XAUUSD"),
        ),
        channels=(
            Channel(id="ch_a", name="Analyst A Channel",
                    account_id="acc_primary", chat_id=-1001,
                    profile_id="prof_a", enabled=True),
            Channel(id="ch_b", name="Analyst B Channel",
                    account_id="acc_primary", chat_id=-1002,
                    profile_id="prof_b", enabled=True),
        ),
        destinations=(Destination(
            id="dest_main", name="Main", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-Main",
        ),),
        bots=(Bot(
            id="bot_main", name="Main Bot",
            token_setting_key="tg_bot_token",
            service_name="CT-Bot-Main",
        ),),
        # Aggregate: both channels route to the SAME destination.
        routes=(
            Route(id="route_a", channel_id="ch_a",
                  destination_id="dest_main", enabled=True),
            Route(id="route_b", channel_id="ch_b",
                  destination_id="dest_main", enabled=True),
        ),
        bot_bindings=(BotBinding(
            id="bind_main", bot_id="bot_main", scope="destination",
            destination_id="dest_main",
        ),),
    )
    config_v2.save_v2(cfg, cfg_path)
    return appdata, profile_a, profile_b


# ---- _resolve_profile_for_channel ----------------------------------------


def test_resolver_picks_channel_a_profile(monkeypatch, tmp_path: Path):
    """Aggregate happy path: channel_id resolves to its own profile."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    _, profile_a, _ = _write_two_channel_cfg(tmp_path)

    fallback = _profile_ctx(profile_a)  # arbitrary
    resolved = _resolve_profile_for_channel("ch_a", fallback)
    assert resolved is not None
    assert "HEADER FROM PROFILE A" in resolved.system_prompt


def test_resolver_picks_channel_b_profile(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    _, profile_a, _ = _write_two_channel_cfg(tmp_path)

    fallback = _profile_ctx(profile_a)
    resolved = _resolve_profile_for_channel("ch_b", fallback)
    assert resolved is not None
    assert "HEADER FROM PROFILE B" in resolved.system_prompt


def test_resolver_returns_fallback_when_channel_id_blank(
    monkeypatch, tmp_path: Path,
):
    """Pre-Step-12 listeners don't send channel_id; resolver returns fallback."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    _, profile_a, _ = _write_two_channel_cfg(tmp_path)

    fallback = _profile_ctx(profile_a)
    resolved = _resolve_profile_for_channel("", fallback)
    assert resolved is fallback


def test_resolver_returns_fallback_when_channel_unknown(
    monkeypatch, tmp_path: Path,
):
    """Channel id not in v2 config → fallback (don't crash on stale data)."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    _, profile_a, _ = _write_two_channel_cfg(tmp_path)

    fallback = _profile_ctx(profile_a)
    resolved = _resolve_profile_for_channel("ch_deleted", fallback)
    assert resolved is fallback


def test_resolver_returns_fallback_when_v2_absent(monkeypatch, tmp_path: Path):
    """Fresh install pre-migration → resolver hands back the fallback."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    clear_profile_cache()

    # Build a minimal fallback profile without writing v2 config.
    profile_path = tmp_path / "fallback.json"
    _make_profile_json(profile_path, header="FALLBACK")
    fallback = _profile_ctx(profile_path)

    resolved = _resolve_profile_for_channel("ch_a", fallback)
    assert resolved is fallback


# ---- End-to-end: two channels, two profiles, one destination -------------


def _build_destination_app(tmp_path: Path) -> tuple[TestClient, "sqlite3.Connection", dict]:
    """Build a destination's API + a captured-prompts AI client."""
    db_path = tmp_path / "appdata" / "CopyTrades" / "dest_main" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)

    captured: dict = {"prompts": []}
    ai = MagicMock(spec=AIClient)

    def _capture_call(*args, system_prompt=None, **kwargs):
        captured["prompts"].append(system_prompt)
        return AICallResult(
            response=AIResponse(actions=[OpenAction(
                symbol="XAUUSD", side="BUY",
                entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
            )], reasoning=""),
            raw_text="{}",
            usage={"input_tokens": 1, "output_tokens": 1,
                   "cache_read_tokens": 0, "cache_creation_tokens": 0},
            latency_ms=1,
        )

    ai.call.side_effect = _capture_call

    # Startup-loaded profile = profile_a (the destination's "default" —
    # what it would have been on a single-channel deployment). Step 12
    # should resolve PER-CHANNEL anyway, so when channel_b's message
    # arrives, the actual prompt used should come from profile_b.
    profile_a = tmp_path / "appdata" / "CopyTrades" / "profiles" / "a.json"
    startup_profile = _profile_ctx(profile_a)

    app = build_app(
        conn, ai_client=ai, triage_client=None,
        profile=startup_profile,
        ai_log_path=db_path.parent / "ai.jsonl",
    )
    return TestClient(app), conn, captured


def test_aggregate_two_channels_use_their_own_profiles(
    monkeypatch, tmp_path: Path,
):
    """The acceptance gate: messages from channel A use prof_a's prompt,
    messages from channel B use prof_b's prompt — even though they hit
    the same destination's API process."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    _write_two_channel_cfg(tmp_path)

    client, conn, captured = _build_destination_app(tmp_path)

    received_at = datetime.now(timezone.utc).isoformat()
    # Channel A message
    r = client.post("/incoming_message", json={
        "channel_id": "ch_a", "tg_chat_id": -1001, "tg_message_id": 1,
        "text": "buy", "received_at": received_at,
        "route_id": "route_a",
    })
    assert r.status_code == 202
    # Channel B message
    r = client.post("/incoming_message", json={
        "channel_id": "ch_b", "tg_chat_id": -1002, "tg_message_id": 2,
        "text": "buy", "received_at": received_at,
        "route_id": "route_b",
    })
    assert r.status_code == 202

    # Each AI call used the channel-appropriate prompt.
    assert len(captured["prompts"]) == 2
    a_prompt, b_prompt = captured["prompts"]
    assert "HEADER FROM PROFILE A" in a_prompt
    assert "HEADER FROM PROFILE B" in b_prompt
    # Cross-check: each prompt contains only ITS header.
    assert "HEADER FROM PROFILE B" not in a_prompt
    assert "HEADER FROM PROFILE A" not in b_prompt

    # Each action is tagged with the right source_channel_id + route_id.
    rows = conn.execute(
        "SELECT source_channel_id, route_id FROM actions "
        "WHERE action_type='OPEN' ORDER BY id"
    ).fetchall()
    assert [r["source_channel_id"] for r in rows] == ["ch_a", "ch_b"]
    assert [r["route_id"] for r in rows] == ["route_a", "route_b"]


# ---- DM rendering: channel-name annotation -------------------------------


def test_render_action_terminal_omits_channel_when_unset():
    """Single-channel destinations get the clean legacy DM (no '[from: ...]')."""
    from src.telegram_format import render_action_terminal
    text = render_action_terminal(
        action_id=42, action_type="OPEN", status="executed",
        payload={"symbol": "XAUUSD", "side": "BUY"},
        ea_response="",
    )
    assert "[from:" not in text
    assert "OPEN executed" in text


def test_render_action_terminal_includes_channel_when_set():
    """Aggregate-routing DMs show which channel triggered each event."""
    from src.telegram_format import render_action_terminal
    text = render_action_terminal(
        action_id=42, action_type="OPEN", status="executed",
        payload={"symbol": "XAUUSD", "side": "BUY"},
        ea_response="",
        source_channel_name="SMC Daily",
    )
    assert "[from: SMC Daily]" in text


def test_resolve_channel_name_returns_name_from_v2_cfg(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    _write_two_channel_cfg(tmp_path)
    from src.bot_outbox_tailer import _resolve_channel_name
    assert _resolve_channel_name("ch_a") == "Analyst A Channel"
    assert _resolve_channel_name("ch_b") == "Analyst B Channel"


def test_resolve_channel_name_blank_for_none_or_missing(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    _write_two_channel_cfg(tmp_path)
    from src.bot_outbox_tailer import _resolve_channel_name
    assert _resolve_channel_name(None) == ""
    assert _resolve_channel_name("") == ""
    # Unknown id returns blank — caller renders without channel label.
    assert _resolve_channel_name("ch_deleted") == ""


def test_resolve_channel_name_returns_blank_when_v2_absent(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    clear_profile_cache()
    from src.bot_outbox_tailer import _resolve_channel_name
    assert _resolve_channel_name("ch_a") == ""


# ---- ConfigV2.channel_name helper ----------------------------------------


def test_config_v2_channel_name_returns_name():
    cfg = ConfigV2(
        channels=(Channel(id="ch_x", name="My Channel",
                          account_id="a", chat_id=-1, profile_id="p"),),
    )
    assert cfg.channel_name("ch_x") == "My Channel"


def test_config_v2_channel_name_falls_back_to_id():
    """Stable identifier when channel not in cfg (e.g. deleted row tag)."""
    cfg = ConfigV2()
    assert cfg.channel_name("ch_orphan") == "ch_orphan"
