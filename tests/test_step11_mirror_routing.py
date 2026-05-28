"""Mirror-routing tests (Step 11 of multi-channel plan).

Validates that ONE channel can route to N destinations and that each
destination's actions are correctly tagged with the originating
``source_channel_id`` and ``route_id``.

Covers the code wire chain — TestClient against real FastAPI endpoints,
real SQLite. Mocks AI to keep tests hermetic. The two-destination fan-out
inside the listener (asyncio.gather) is unit-tested via the resolver
return shape + per-leg ``_post_incoming_message`` behavior (already
covered by Step 4 tests).
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src import config
from src.ai import AICallResult, AIClient
from src.api import build_app
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


def _profile_ctx(symbol: str = "XAUUSD") -> ProfileContext:
    from src.ai import _render_system_prompt_from_data
    from src.ai_triage import _render_triage_prompt_from_data
    data = {
        "header": "T", "vocabulary_table": "v", "compound_messages": "c",
        "commentary_filter": "f", "directional_command_flow": "d",
        "worked_examples": "e", "shorthand_decode_example": "s",
        "promo_indicators": "", "noise_patterns": "", "triage_keep_triggers": "",
        "symbol": symbol,
    }
    return ProfileContext(
        name="test", path=Path("/dev/null"), data=data,
        system_prompt=_render_system_prompt_from_data(data),
        triage_prompt=_render_triage_prompt_from_data(data),
        symbol=symbol,
    )


def _ai_returning_open() -> MagicMock:
    client = MagicMock(spec=AIClient)
    client.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol="XAUUSD", side="BUY",
            entry_low=4000.0, entry_high=4002.0, sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=1,
    )
    return client


def _build_destination(tmp_path: Path, name: str):
    db_path = tmp_path / name / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    app = build_app(
        conn,
        ai_client=_ai_returning_open(),
        triage_client=None,
        profile=_profile_ctx(),
        ai_log_path=db_path.parent / "ai.jsonl",
    )
    return conn, TestClient(app), db_path


# ---- Step 11: API endpoint accepts route_id ------------------------------


def test_endpoint_accepts_route_id_in_body(monkeypatch, tmp_path: Path):
    """The IncomingMessageBody schema must accept route_id per Step 11."""
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()

    conn, client, _ = _build_destination(tmp_path, "dest_a")
    r = client.post("/incoming_message", json={
        "channel_id": "ch_a",
        "tg_chat_id": -1001,
        "tg_message_id": 1,
        "text": "BUY",
        "route_id": "route_a_to_dest_a",
        "sizing_multiplier": 1.0,
    })
    assert r.status_code == 202

    # Message inserted with source_channel_id tagged (Step 4 gap closed).
    msg = conn.execute(
        "SELECT source_channel_id FROM messages WHERE tg_message_id=1"
    ).fetchone()
    assert msg["source_channel_id"] == "ch_a"

    # Action inserted with both source_channel_id AND route_id tagged.
    action = conn.execute(
        "SELECT source_channel_id, route_id FROM actions "
        "WHERE action_type='OPEN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert action is not None
    assert action["source_channel_id"] == "ch_a"
    assert action["route_id"] == "route_a_to_dest_a"


def test_endpoint_accepts_blank_route_id_for_backcompat(
    monkeypatch, tmp_path: Path,
):
    """Pre-Step-11 listeners don't send route_id. Default ('') means
    actions get NULL route_id — preserves pre-mirror behavior."""
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()

    conn, client, _ = _build_destination(tmp_path, "dest_a")
    r = client.post("/incoming_message", json={
        "channel_id": "ch_a",
        "tg_chat_id": -1001,
        "tg_message_id": 2,
        "text": "BUY",
        # No route_id — pre-Step-11 listener shape
    })
    assert r.status_code == 202

    action = conn.execute(
        "SELECT source_channel_id, route_id FROM actions "
        "WHERE action_type='OPEN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert action["source_channel_id"] == "ch_a"
    # Empty string → None in DB (the orchestrator stamper coerces "").
    assert action["route_id"] is None


# ---- Step 11: full mirror flow against TWO destinations ------------------


def test_mirror_one_channel_to_two_destinations(monkeypatch, tmp_path: Path):
    """The acceptance check: one channel POSTing the SAME message to two
    destinations with DIFFERENT route_ids should land each as a distinct
    action in each destination's DB."""
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()

    conn_a, client_a, _ = _build_destination(tmp_path, "dest_a")
    conn_b, client_b, _ = _build_destination(tmp_path, "dest_b")

    received_at = datetime.now(timezone.utc).isoformat()
    body = {
        "channel_id": "ch_main",
        "tg_chat_id": -1001,
        "tg_message_id": 42,
        "text": "BUY GOLD",
        "sender": "X",
        "received_at": received_at,
        "is_backfill": False,
    }

    # Leg 1: same channel, route_a → destination A
    r_a = client_a.post("/incoming_message", json={
        **body, "route_id": "route_main_to_a", "sizing_multiplier": 1.0,
    })
    assert r_a.status_code == 202

    # Leg 2: same channel, same tg_message_id, route_b → destination B,
    # 0.5x sizing (conservative mirror)
    r_b = client_b.post("/incoming_message", json={
        **body, "route_id": "route_main_to_b", "sizing_multiplier": 0.5,
    })
    assert r_b.status_code == 202

    # Each destination has its own actions row with the SAME
    # source_channel_id but DIFFERENT route_id.
    a = conn_a.execute(
        "SELECT source_channel_id, route_id FROM actions "
        "WHERE action_type='OPEN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    b = conn_b.execute(
        "SELECT source_channel_id, route_id FROM actions "
        "WHERE action_type='OPEN' ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert a["source_channel_id"] == "ch_main"
    assert b["source_channel_id"] == "ch_main"
    assert a["route_id"] == "route_main_to_a"
    assert b["route_id"] == "route_main_to_b"

    # Each destination has its own message row with the same tg_message_id
    # (no leakage between destinations — they're isolated DBs).
    msg_a = conn_a.execute(
        "SELECT source_channel_id FROM messages WHERE tg_message_id=42"
    ).fetchone()
    msg_b = conn_b.execute(
        "SELECT source_channel_id FROM messages WHERE tg_message_id=42"
    ).fetchone()
    assert msg_a["source_channel_id"] == "ch_main"
    assert msg_b["source_channel_id"] == "ch_main"


def test_destination_b_unaffected_when_destination_a_fails(
    monkeypatch, tmp_path: Path,
):
    """Per Step 11 design: 'one slow destination must not block dispatch
    to its siblings'. Even when destination A's API would 503, B's POST
    succeeds independently. (TestClient is sync so we simulate the
    failure via a separately-constructed app without runtime context.)"""
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()

    # Destination A: no runtime context (returns 503 on incoming_message).
    db_a = tmp_path / "dest_a" / "copytrades.db"
    db_a.parent.mkdir(parents=True, exist_ok=True)
    conn_a = connect(str(db_a))
    init_schema(conn_a)
    app_a = build_app(conn_a)  # no ai_client kwarg → 503
    client_a = TestClient(app_a)

    # Destination B: fully wired.
    conn_b, client_b, _ = _build_destination(tmp_path, "dest_b")

    body = {
        "channel_id": "ch_main", "tg_chat_id": -1001, "tg_message_id": 99,
        "text": "BUY", "route_id": "route_main_to_a",
    }

    r_a = client_a.post("/incoming_message", json=body)
    assert r_a.status_code == 503

    r_b = client_b.post(
        "/incoming_message",
        json={**body, "route_id": "route_main_to_b"},
    )
    assert r_b.status_code == 202

    # Destination B still got its action; A has none.
    a_count = conn_a.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    b_count = conn_b.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert a_count == 0
    assert b_count == 1


# ---- Listener-side helpers: _ApiDispatchTarget carries the new fields ----


def test_dispatch_target_includes_route_id_in_post_body(monkeypatch):
    """_post_incoming_message must include route_id + sizing_multiplier
    in the JSON body so the receiving API can read them."""
    from src.listener import _ApiDispatchTarget, _post_incoming_message

    target = _ApiDispatchTarget(
        url="http://test/incoming_message", channel_id="ch_x", token="",
        route_id="route_xyz", sizing_multiplier=0.75,
    )
    captured: dict = {}

    class _FakeResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr("src.listener.urllib.request.urlopen", _fake_urlopen)

    ok = _post_incoming_message(
        target, tg_chat_id=-1, tg_message_id=1, text="x",
        sender="", received_at="", is_backfill=False,
    )
    assert ok is True
    assert captured["body"]["route_id"] == "route_xyz"
    assert captured["body"]["sizing_multiplier"] == 0.75


def test_dispatch_target_defaults_blank_route_id():
    """Existing _ApiDispatchTarget callers that don't set route_id/
    sizing_multiplier get safe defaults."""
    from src.listener import _ApiDispatchTarget

    target = _ApiDispatchTarget(url="x", channel_id="c", token="")
    assert target.route_id == ""
    assert target.sizing_multiplier == 1.0
