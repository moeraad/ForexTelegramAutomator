"""Per-channel halt tests (Step 15 of multi-channel plan).

Validates the halt-at-API-boundary design:
  - ``api_helpers._resolve_halt_for_message`` reads v2 config (mtime-cached)
  - ``orchestrator.process_message(halted=True)`` records the message but
    emits ZERO actions
  - ``POST /incoming_message`` honors halt end-to-end (audit row present,
    actions empty)
  - Halt is per-channel AND per-route (either short-circuits)
  - Halt persists across config reloads (file-backed)
  - GUI helpers ``with_channel_halted`` / ``with_route_halted`` produce
    new ``ConfigV2`` instances without touching the originals

The store is the v2 config file itself — ``Channel.halted`` and
``Route.halted`` carved out as forward-compat fields by Steps 1/9. No
new DB columns, no new components.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src import config, config_v2
from src.ai import AICallResult, AIClient
from src.api import build_app
from src.api_helpers import _resolve_halt_for_message
from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
    with_channel_halted,
    with_route_halted,
)
from src.db import connect, init_schema
from src.profile_context import ProfileContext, clear_cache as clear_profile_cache
from src.validators import AIResponse, OpenAction


# ---- with_channel_halted / with_route_halted: pure transforms ------------


def _minimal_cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(
            id="acc_primary", name="P", phone="",
            session_path="", service_name="CT-Listener-acc_primary",
        ),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_primary",
                    chat_id=-1, profile_id="prof"),
            Channel(id="ch_b", name="B", account_id="acc_primary",
                    chat_id=-2, profile_id="prof"),
        ),
        routes=(
            Route(id="route_a", channel_id="ch_a", destination_id="dest"),
            Route(id="route_b", channel_id="ch_b", destination_id="dest"),
        ),
    )


def test_with_channel_halted_returns_new_cfg_with_flag_set():
    cfg = _minimal_cfg()
    new_cfg = with_channel_halted(cfg, "ch_a", True)
    assert new_cfg.channel("ch_a").halted is True
    assert new_cfg.channel("ch_b").halted is False
    # Original is untouched (immutability invariant).
    assert cfg.channel("ch_a").halted is False


def test_with_channel_halted_flips_back_when_false():
    cfg = with_channel_halted(_minimal_cfg(), "ch_a", True)
    cfg2 = with_channel_halted(cfg, "ch_a", False)
    assert cfg2.channel("ch_a").halted is False


def test_with_channel_halted_raises_on_unknown_id():
    import pytest
    with pytest.raises(ValueError, match="Unknown channel"):
        with_channel_halted(_minimal_cfg(), "ch_nope", True)


def test_with_route_halted_returns_new_cfg_with_flag_set():
    cfg = _minimal_cfg()
    new_cfg = with_route_halted(cfg, "route_a", True)
    assert new_cfg.route("route_a").halted is True
    assert new_cfg.route("route_b").halted is False


def test_with_route_halted_raises_on_unknown_id():
    import pytest
    with pytest.raises(ValueError, match="Unknown route"):
        with_route_halted(_minimal_cfg(), "route_nope", True)


# ---- _resolve_halt_for_message: backend resolution -----------------------


def _write_cfg(appdata: Path, cfg: ConfigV2) -> Path:
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, cfg_path)
    return cfg_path


def test_resolver_returns_false_when_blank_ids():
    assert _resolve_halt_for_message("", "") is False


def test_resolver_returns_false_when_v2_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    assert _resolve_halt_for_message("ch_a", "route_a") is False


def test_resolver_returns_true_when_channel_halted(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = with_channel_halted(_minimal_cfg(), "ch_a", True)
    _write_cfg(tmp_path / "appdata", cfg)
    assert _resolve_halt_for_message("ch_a", "route_a") is True
    # Channel B is not halted → halt resolver returns False for it.
    assert _resolve_halt_for_message("ch_b", "route_b") is False


def test_resolver_returns_true_when_route_halted_but_channel_not(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = with_route_halted(_minimal_cfg(), "route_a", True)
    _write_cfg(tmp_path / "appdata", cfg)
    # route_a is halted; channel ch_a is NOT — the resolver still returns
    # True because either flag short-circuits the orchestrator.
    assert _resolve_halt_for_message("ch_a", "route_a") is True
    # Sibling route on the same channel is not halted.
    assert _resolve_halt_for_message("ch_a", "route_b") is False


def test_resolver_returns_false_when_unknown_ids(
    monkeypatch, tmp_path: Path,
):
    """Stale channel/route id (since-deleted) is fail-open: don't crash,
    don't silently halt."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    _write_cfg(tmp_path / "appdata", _minimal_cfg())
    assert _resolve_halt_for_message("ch_deleted", "route_deleted") is False


def test_resolver_picks_up_config_change_via_mtime_reload(
    monkeypatch, tmp_path: Path,
):
    """Halt persists across config-file rewrites without an API restart.

    The resolver reads via ``is_v2``/``load_v2`` which respects the
    underlying file's mtime — so flipping halt on disk takes effect on
    the next message (no service restart needed).
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg_path = _write_cfg(tmp_path / "appdata", _minimal_cfg())
    assert _resolve_halt_for_message("ch_a", "route_a") is False
    # Operator flips halt via the GUI (which calls with_channel_halted
    # + save_v2 — exact pathway exercised here).
    halted_cfg = with_channel_halted(_minimal_cfg(), "ch_a", True)
    # Bump mtime to be safe on file systems with low timestamp resolution.
    import time
    time.sleep(0.01)
    config_v2.save_v2(halted_cfg, cfg_path)
    assert _resolve_halt_for_message("ch_a", "route_a") is True


# ---- process_message(halted=True): records but emits no actions ----------


def _make_profile_json(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "header": "H", "vocabulary_table": "v", "compound_messages": "c",
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


def test_process_message_halted_inserts_message_but_no_actions(
    tmp_path: Path,
):
    """The audit invariant: halted messages are STILL recorded so the
    operator can replay them after unhalting if needed."""
    db_path = tmp_path / "copytrades.db"
    conn = connect(str(db_path))
    init_schema(conn)
    log_path = tmp_path / "ai.jsonl"

    profile_path = tmp_path / "profile.json"
    _make_profile_json(profile_path)
    profile = _profile_ctx(profile_path)

    ai = MagicMock(spec=AIClient)  # would crash if called
    from src.orchestrator import process_message

    action_ids = process_message(
        conn, ai,
        tg_message_id=1234, chat_id=-1001, sender="x", text="buy XAUUSD",
        ai_log_path=log_path,
        auto_execute_delay_sec=0,
        profile=profile,
        source_channel_id="ch_a", route_id="route_a",
        halted=True,
    )
    assert action_ids == []
    # AI was never called.
    ai.call.assert_not_called()
    # Message was inserted (audit invariant).
    rows = conn.execute(
        "SELECT tg_message_id, source_channel_id FROM messages"
    ).fetchall()
    assert len(rows) == 1
    assert rows[0]["tg_message_id"] == 1234
    assert rows[0]["source_channel_id"] == "ch_a"
    # No actions inserted.
    n = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert n == 0
    # ai_calls.jsonl shows the halt drop so the operator can confirm.
    log_text = log_path.read_text(encoding="utf-8")
    assert '"stage": "halt"' in log_text
    assert '"decision": "drop"' in log_text


# ---- End-to-end: POST /incoming_message honors halt ----------------------


def _full_cfg(tmp_path: Path) -> tuple[Path, Path]:
    """Two channels, one destination, one of them halted."""
    appdata = tmp_path / "appdata"
    db_path = appdata / "CopyTrades" / "dest_main" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path = appdata / "CopyTrades" / "profiles" / "main.json"
    _make_profile_json(profile_path)

    cfg = ConfigV2(
        accounts=(Account(
            id="acc_primary", name="P", phone="",
            session_path="", service_name="CT-Listener-acc_primary",
        ),),
        profiles=(Profile(id="prof", name="Main", path=str(profile_path),
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_primary",
                    chat_id=-1001, profile_id="prof", halted=True),
            Channel(id="ch_b", name="B", account_id="acc_primary",
                    chat_id=-1002, profile_id="prof"),
        ),
        destinations=(Destination(
            id="dest_main", name="Main", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-Main",
        ),),
        bots=(Bot(id="bot_main", name="B", token_setting_key="tg_bot_token",
                  service_name="CT-Bot-Main"),),
        routes=(
            Route(id="route_a", channel_id="ch_a", destination_id="dest_main"),
            Route(id="route_b", channel_id="ch_b", destination_id="dest_main"),
        ),
        bot_bindings=(BotBinding(
            id="bind_main", bot_id="bot_main", scope="destination",
            destination_id="dest_main",
        ),),
    )
    _write_cfg(appdata, cfg)
    return db_path, profile_path


def test_post_incoming_message_skips_orchestrator_for_halted_channel(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    db_path, profile_path = _full_cfg(tmp_path)

    conn = connect(str(db_path))
    init_schema(conn)

    call_count = {"n": 0}
    ai = MagicMock(spec=AIClient)

    def _ai_call(*args, **kwargs):
        call_count["n"] += 1
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
    ai.call.side_effect = _ai_call

    app = build_app(
        conn, ai_client=ai, triage_client=None,
        profile=_profile_ctx(profile_path),
        ai_log_path=db_path.parent / "ai.jsonl",
    )
    client = TestClient(app)

    received_at = datetime.now(timezone.utc).isoformat()
    # Halted channel: message should be recorded but no AI call, no actions.
    r = client.post("/incoming_message", json={
        "channel_id": "ch_a", "tg_chat_id": -1001, "tg_message_id": 1,
        "text": "buy", "received_at": received_at, "route_id": "route_a",
    })
    assert r.status_code == 202
    # Non-halted channel: full orchestrator path runs.
    r = client.post("/incoming_message", json={
        "channel_id": "ch_b", "tg_chat_id": -1002, "tg_message_id": 2,
        "text": "buy", "received_at": received_at, "route_id": "route_b",
    })
    assert r.status_code == 202

    # Both messages recorded for audit.
    msg_rows = conn.execute(
        "SELECT tg_message_id, source_channel_id FROM messages "
        "ORDER BY tg_message_id"
    ).fetchall()
    assert [r["source_channel_id"] for r in msg_rows] == ["ch_a", "ch_b"]
    # AI was called exactly once (for the unhalted channel).
    assert call_count["n"] == 1
    # Only the unhalted channel produced actions.
    act_rows = conn.execute(
        "SELECT source_channel_id FROM actions"
    ).fetchall()
    assert [r["source_channel_id"] for r in act_rows] == ["ch_b"]


def test_post_incoming_message_skips_orchestrator_for_halted_route(
    monkeypatch, tmp_path: Path,
):
    """Per-route halt: same channel, two routes — only the halted route
    short-circuits. (Routes mirror to two destinations in a real setup;
    this test uses one destination + per-route halt to isolate the
    resolver's per-route branch.)"""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()

    appdata = tmp_path / "appdata"
    db_path = appdata / "CopyTrades" / "dest_main" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    profile_path = appdata / "CopyTrades" / "profiles" / "main.json"
    _make_profile_json(profile_path)

    cfg = ConfigV2(
        accounts=(Account(
            id="acc_primary", name="P", phone="",
            session_path="", service_name="CT-Listener-acc_primary",
        ),),
        profiles=(Profile(id="prof", name="P", path=str(profile_path),
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(
            id="ch_a", name="A", account_id="acc_primary",
            chat_id=-1001, profile_id="prof",
        ),),
        destinations=(Destination(
            id="dest_main", name="M", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-Main",
        ),),
        bots=(Bot(id="bot_main", name="B", token_setting_key="tg_bot_token",
                  service_name="CT-Bot-Main"),),
        routes=(
            Route(id="route_halted", channel_id="ch_a",
                  destination_id="dest_main", halted=True),
            Route(id="route_live", channel_id="ch_a",
                  destination_id="dest_main"),
        ),
        bot_bindings=(BotBinding(
            id="bind_main", bot_id="bot_main", scope="destination",
            destination_id="dest_main",
        ),),
    )
    _write_cfg(appdata, cfg)

    conn = connect(str(db_path))
    init_schema(conn)

    ai = MagicMock(spec=AIClient)
    ai.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol="XAUUSD", side="BUY",
            entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=1,
    )

    app = build_app(
        conn, ai_client=ai, triage_client=None,
        profile=_profile_ctx(profile_path),
        ai_log_path=db_path.parent / "ai.jsonl",
    )
    client = TestClient(app)

    received_at = datetime.now(timezone.utc).isoformat()
    # Halted route: orchestrator should skip.
    client.post("/incoming_message", json={
        "channel_id": "ch_a", "tg_chat_id": -1001, "tg_message_id": 1,
        "text": "buy", "received_at": received_at, "route_id": "route_halted",
    })
    # Live route: orchestrator should process.
    client.post("/incoming_message", json={
        "channel_id": "ch_a", "tg_chat_id": -1001, "tg_message_id": 2,
        "text": "buy", "received_at": received_at, "route_id": "route_live",
    })

    # Only the live-route message produced an action.
    rows = conn.execute(
        "SELECT route_id FROM actions ORDER BY id"
    ).fetchall()
    assert [r["route_id"] for r in rows] == ["route_live"]
