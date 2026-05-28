"""Tests for POST /incoming_message + listener-side dispatch helpers.

See ``docs/plans/2026-05-23-multi-channel-routing.md`` Step 4. Covers:

  - Endpoint shape: validation, 202 response, body fields persist
  - Background task runs orchestrator with the right args (channel_id,
    profile, is_backfill, sender, tg_chat_id, tg_message_id)
  - Auth: X-Listener-Token validated when LISTENER_SHARED_TOKEN set;
    falls back to EA_SHARED_TOKEN when listener token unset
  - 503 when API was built without runtime context (defensive default)
  - _backfill_destination_channel_on_startup matches db_path and updates
    source_channel_id / route_id
  - Listener-side _post_incoming_message: success, retry-on-5xx,
    don't-retry-on-4xx, exhaust-retries-returns-False
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from src import config, config_v2
from src.api import (
    _backfill_destination_channel_on_startup,
    build_app,
)
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
from src.db import backfill_source_channel_id, connect, init_schema


# ---- Endpoint shape --------------------------------------------------------


def _build_test_app(conn, *, ai=None, triage=None, profile=None, ai_log_path=None):
    return build_app(
        conn,
        ai_client=ai,
        triage_client=triage,
        profile=profile,
        ai_log_path=ai_log_path,
    )


def test_endpoint_returns_503_without_runtime_context():
    conn = connect(":memory:")
    init_schema(conn)
    app = build_app(conn)  # no ai_client / no ai_log_path
    client = TestClient(app)
    r = client.post("/incoming_message", json={
        "channel_id": "ch_x",
        "tg_chat_id": -42,
        "tg_message_id": 1,
        "text": "hi",
    })
    assert r.status_code == 503
    assert "runtime context" in r.json()["detail"]


def test_endpoint_returns_202_and_calls_orchestrator(monkeypatch, tmp_path: Path):
    conn = connect(":memory:")
    init_schema(conn)

    captured: dict = {}

    def fake_process_message(*args, **kwargs):
        # Capture exactly what the endpoint forwarded.
        captured["args"] = args
        captured["kwargs"] = kwargs
        return [1]

    monkeypatch.setattr("src.orchestrator.process_message", fake_process_message)

    ai = object()  # opaque; orchestrator is mocked
    log_path = tmp_path / "ai.jsonl"
    app = _build_test_app(conn, ai=ai, ai_log_path=log_path)
    client = TestClient(app)
    r = client.post("/incoming_message", json={
        "channel_id": "ch_x",
        "tg_chat_id": -42,
        "tg_message_id": 7,
        "text": "BUY GOLD",
        "sender": "Yusuf",
        "received_at": "2026-05-23T00:00:00+00:00",
        "is_backfill": False,
    })
    assert r.status_code == 202
    assert r.json() == {"queued": True, "tg_message_id": 7}
    # BackgroundTasks runs synchronously in TestClient AFTER the response,
    # so once the client returns, the orchestrator has been invoked.
    assert "args" in captured, "orchestrator never called"
    a = captured["args"]
    # positional: conn, ai, tg_message_id, tg_chat_id, sender, text, ai_log_path, delay
    assert a[2] == 7         # tg_message_id
    assert a[3] == -42       # tg_chat_id
    assert a[4] == "Yusuf"   # sender
    assert a[5] == "BUY GOLD"  # text
    assert captured["kwargs"]["is_backfill"] is False


def test_endpoint_forwards_is_backfill(monkeypatch, tmp_path: Path):
    conn = connect(":memory:")
    init_schema(conn)
    captured: dict = {}

    def fake(*args, **kwargs):
        captured["kwargs"] = kwargs

    monkeypatch.setattr("src.orchestrator.process_message", fake)
    app = _build_test_app(conn, ai=object(), ai_log_path=tmp_path / "ai.jsonl")
    client = TestClient(app)
    client.post("/incoming_message", json={
        "channel_id": "c", "tg_chat_id": 1, "tg_message_id": 1,
        "text": "x", "is_backfill": True,
    })
    assert captured["kwargs"]["is_backfill"] is True


def test_endpoint_422_on_missing_required_fields(tmp_path: Path):
    conn = connect(":memory:")
    init_schema(conn)
    app = _build_test_app(conn, ai=object(), ai_log_path=tmp_path / "ai.jsonl")
    client = TestClient(app)
    r = client.post("/incoming_message", json={"text": "x"})  # missing all keys
    assert r.status_code == 422


# ---- Auth ------------------------------------------------------------------


def test_listener_token_required_when_set(monkeypatch, tmp_path: Path):
    conn = connect(":memory:")
    init_schema(conn)
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "secret_listener")
    monkeypatch.setattr("src.orchestrator.process_message", lambda *a, **k: [1])

    app = _build_test_app(conn, ai=object(), ai_log_path=tmp_path / "ai.jsonl")
    client = TestClient(app)
    body = {"channel_id": "c", "tg_chat_id": 1, "tg_message_id": 1, "text": "x"}

    # Missing header → 401
    r = client.post("/incoming_message", json=body)
    assert r.status_code == 401

    # Wrong header → 401
    r = client.post("/incoming_message", json=body, headers={"X-Listener-Token": "wrong"})
    assert r.status_code == 401

    # Correct header → 202
    r = client.post("/incoming_message", json=body, headers={"X-Listener-Token": "secret_listener"})
    assert r.status_code == 202


def test_listener_token_falls_back_to_ea_token(monkeypatch, tmp_path: Path):
    conn = connect(":memory:")
    init_schema(conn)
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "fallback")
    monkeypatch.setattr("src.orchestrator.process_message", lambda *a, **k: [1])

    app = _build_test_app(conn, ai=object(), ai_log_path=tmp_path / "ai.jsonl")
    client = TestClient(app)
    body = {"channel_id": "c", "tg_chat_id": 1, "tg_message_id": 1, "text": "x"}

    r = client.post("/incoming_message", json=body)
    assert r.status_code == 401
    r = client.post("/incoming_message", json=body, headers={"X-Listener-Token": "fallback"})
    assert r.status_code == 202


def test_ea_endpoints_still_use_ea_token(monkeypatch):
    """Auth split must not break the EA's existing X-EA-Token contract."""
    conn = connect(":memory:")
    init_schema(conn)
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "ea_secret")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")

    app = _build_test_app(conn)
    client = TestClient(app)
    r = client.get("/actions?status=sent")
    assert r.status_code == 401
    r = client.get("/actions?status=sent", headers={"X-EA-Token": "ea_secret"})
    assert r.status_code == 200


# ---- Startup backfill ------------------------------------------------------


def test_backfill_on_startup_matches_destination_by_db_path(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))

    db_path = appdata / "CopyTrades" / "main" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    # Pre-existing rows with NULL source_channel_id
    conn.execute("INSERT INTO messages(tg_message_id, chat_id, text) VALUES (1, -42, 'a')")
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES ('OPEN', '{}')")

    cfg = ConfigV2(
        accounts=(Account(id="a", name="A", phone="+961", session_path="", service_name="s"),),
        profiles=(Profile(id="p", name="p", path=""),),
        channels=(Channel(id="ch_main", name="Main", account_id="a", chat_id=-42, profile_id="p"),),
        destinations=(Destination(id="dest_main", name="Main",
                                  db_path=str(db_path),
                                  api_host="127.0.0.1", api_port=8765,
                                  service_name="CT-Api-Main"),),
        bots=(),
        routes=(Route(id="route_main", channel_id="ch_main",
                      destination_id="dest_main", enabled=True),),
        bot_bindings=(),
    )
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, cfg_path)

    _backfill_destination_channel_on_startup(conn, str(db_path))
    row = conn.execute("SELECT source_channel_id FROM messages WHERE tg_message_id=1").fetchone()
    assert row["source_channel_id"] == "ch_main"
    row = conn.execute("SELECT source_channel_id, route_id FROM actions LIMIT 1").fetchone()
    assert row["source_channel_id"] == "ch_main"
    assert row["route_id"] == "route_main"


def test_backfill_on_startup_skips_when_v2_absent(tmp_path: Path):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    # No v2 config exists at the env path — must not crash.
    _backfill_destination_channel_on_startup(conn, str(db_path))


def test_backfill_on_startup_skips_when_db_not_in_config(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    # Config has a destination but pointing at a different DB.
    cfg = ConfigV2(
        destinations=(Destination(id="d", name="Other",
                                  db_path=str(tmp_path / "other.db"),
                                  api_host="", api_port=0, service_name=""),),
    )
    config_v2.save_v2(cfg, cfg_path)

    this_db = tmp_path / "this.db"
    conn = connect(str(this_db))
    init_schema(conn)
    conn.execute("INSERT INTO messages(tg_message_id, chat_id, text) VALUES (1, -1, 'a')")
    _backfill_destination_channel_on_startup(conn, str(this_db))
    row = conn.execute("SELECT source_channel_id FROM messages").fetchone()
    assert row["source_channel_id"] is None  # untouched


# ---- Listener-side POST helper --------------------------------------------


def test_post_incoming_message_success(monkeypatch):
    from src.listener import _ApiDispatchTarget, _post_incoming_message

    target = _ApiDispatchTarget(url="http://test/incoming_message",
                                channel_id="ch_x", token="tok")

    captured: dict = {}

    class _FakeResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResp()

    monkeypatch.setattr("src.listener.urllib.request.urlopen", _fake_urlopen)

    ok = _post_incoming_message(target,
        tg_chat_id=-42, tg_message_id=1, text="hi",
        sender="X", received_at="t", is_backfill=False,
    )
    assert ok is True
    assert captured["body"]["channel_id"] == "ch_x"
    assert captured["body"]["tg_chat_id"] == -42
    # Token sent in custom header (urllib normalizes capitalization)
    assert any(k.lower() == "x-listener-token" and v == "tok"
               for k, v in captured["headers"].items())


def test_post_incoming_message_no_token_omits_header(monkeypatch):
    from src.listener import _ApiDispatchTarget, _post_incoming_message

    target = _ApiDispatchTarget(url="http://test/incoming_message",
                                channel_id="ch_x", token="")
    captured: dict = {}

    class _FakeResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout):
        captured["headers"] = dict(req.header_items())
        return _FakeResp()

    monkeypatch.setattr("src.listener.urllib.request.urlopen", _fake_urlopen)

    _post_incoming_message(target, tg_chat_id=1, tg_message_id=1,
                           text="x", sender="", received_at="", is_backfill=False)
    assert not any(k.lower() == "x-listener-token" for k in captured["headers"])


def test_post_incoming_message_does_not_retry_4xx(monkeypatch):
    """A 400 means the wire contract is broken — retrying won't help."""
    from src.listener import _ApiDispatchTarget, _post_incoming_message
    import urllib.error

    target = _ApiDispatchTarget(url="http://test/incoming_message",
                                channel_id="ch_x", token="")
    call_count = {"n": 0}

    def _fake_urlopen(req, timeout):
        call_count["n"] += 1
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, None)

    monkeypatch.setattr("src.listener.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("src.listener.time.sleep", lambda s: None)

    ok = _post_incoming_message(target,
        tg_chat_id=1, tg_message_id=1, text="x", sender="",
        received_at="", is_backfill=False, max_retries=3,
    )
    assert ok is False
    assert call_count["n"] == 1  # No retry on 4xx


def test_post_incoming_message_retries_5xx_then_succeeds(monkeypatch):
    from src.listener import _ApiDispatchTarget, _post_incoming_message
    import urllib.error

    target = _ApiDispatchTarget(url="http://test/incoming_message",
                                channel_id="ch_x", token="")
    state = {"calls": 0}

    class _OkResp:
        status = 202
        def __enter__(self): return self
        def __exit__(self, *a): return False

    def _fake_urlopen(req, timeout):
        state["calls"] += 1
        if state["calls"] < 3:
            raise urllib.error.HTTPError(req.full_url, 503, "Unavailable", {}, None)
        return _OkResp()

    monkeypatch.setattr("src.listener.urllib.request.urlopen", _fake_urlopen)
    monkeypatch.setattr("src.listener.time.sleep", lambda s: None)

    ok = _post_incoming_message(target,
        tg_chat_id=1, tg_message_id=1, text="x", sender="",
        received_at="", is_backfill=False, max_retries=5,
    )
    assert ok is True
    assert state["calls"] == 3


def test_post_incoming_message_exhausts_retries(monkeypatch):
    from src.listener import _ApiDispatchTarget, _post_incoming_message

    target = _ApiDispatchTarget(url="http://test/incoming_message",
                                channel_id="ch_x", token="")

    def _always_fail(req, timeout):
        raise OSError("connection refused")

    monkeypatch.setattr("src.listener.urllib.request.urlopen", _always_fail)
    monkeypatch.setattr("src.listener.time.sleep", lambda s: None)

    ok = _post_incoming_message(target,
        tg_chat_id=1, tg_message_id=1, text="x", sender="",
        received_at="", is_backfill=False, max_retries=2,
    )
    assert ok is False


# ---- Dispatch-target resolution -------------------------------------------


def test_resolve_dispatch_target_v1_returns_none(monkeypatch, tmp_path: Path):
    """No v2 config → listener falls back to legacy in-process path."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    appdata.mkdir(parents=True, exist_ok=True)
    # No stacks_config.json — fresh install pre-migration.
    from src.listener import _resolve_api_dispatch_target
    assert _resolve_api_dispatch_target() is None


def test_resolve_dispatch_target_matches_destination(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db_path = appdata / "CopyTrades" / "main" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    db_path.touch()
    cfg = ConfigV2(
        destinations=(Destination(id="d", name="Main",
                                  db_path=str(db_path),
                                  api_host="127.0.0.1", api_port=8765,
                                  service_name=""),),
        channels=(Channel(id="ch_main", name="Main", account_id="a",
                          chat_id=-1, profile_id="p"),),
        routes=(Route(id="r", channel_id="ch_main", destination_id="d"),),
    )
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, cfg_path)

    monkeypatch.setattr(config, "DB_PATH", str(db_path))
    monkeypatch.setattr(config, "API_HOST", "127.0.0.1")
    monkeypatch.setattr(config, "API_PORT", 8765)
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "tok_listener")
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "tok_ea")

    from src.listener import _resolve_api_dispatch_target
    target = _resolve_api_dispatch_target()
    assert target is not None
    assert target.url == "http://127.0.0.1:8765/incoming_message"
    assert target.channel_id == "ch_main"
    assert target.token == "tok_listener"
