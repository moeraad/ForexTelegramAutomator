"""Tests for src.notification_dispatcher (Step 7 of multi-channel plan).

Covers:
  - dispatch_notification: returns 0 when v2 absent, when destination
    doesn't match, when no bindings exist
  - dispatch_notification: writes one outbox row per matching binding
  - global-scope binding receives events from any destination matching
    by db_path resolution
  - extra_payload serialized into event_payload JSON
  - has_v2_binding_for_destination: True/False matrix
  - resolve_bot_id_for_destination: returns destination-scope binding
    bot_id; falls back to global; returns None when no matching binding
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import config_v2
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
from src.notification_dispatcher import (
    dispatch_notification,
    has_v2_binding_for_destination,
    resolve_bot_id_for_destination,
)


def _write_cfg(appdata: Path, cfg: ConfigV2) -> Path:
    p = appdata / "CopyTrades" / "stacks_config.json"
    p.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, p)
    return p


def _basic_v2(db_path: Path, *, bot_id: str = "bot_a",
              binding_scope: str = "destination") -> ConfigV2:
    binding_kwargs = {"id": "bind", "bot_id": bot_id, "scope": binding_scope}
    if binding_scope == "destination":
        binding_kwargs["destination_id"] = "dest_a"
    return ConfigV2(
        accounts=(Account(id="a", name="A", phone="", session_path="", service_name="s"),),
        profiles=(Profile(id="p", name="p", path=""),),
        channels=(Channel(id="ch", name="ch", account_id="a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(id="dest_a", name="Dest A",
                                  db_path=str(db_path),
                                  api_host="127.0.0.1", api_port=8765,
                                  service_name=""),),
        bots=(Bot(id=bot_id, name="Bot", token_setting_key="tg_bot_token",
                  service_name=""),),
        routes=(Route(id="r", channel_id="ch", destination_id="dest_a"),),
        bot_bindings=(BotBinding(**binding_kwargs),),
    )


# ---- dispatch_notification ------------------------------------------------


def test_dispatch_returns_zero_when_v2_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    db = tmp_path / "x.db"
    conn = connect(str(db))
    init_schema(conn)
    n = dispatch_notification(conn, event_type="action_terminal",
                              action_id=1, db_path=str(db))
    assert n == 0


def test_dispatch_returns_zero_when_destination_not_in_config(
    monkeypatch, tmp_path: Path,
):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    other_db = tmp_path / "other.db"
    _write_cfg(appdata, _basic_v2(other_db))

    my_db = tmp_path / "mine.db"
    conn = connect(str(my_db))
    init_schema(conn)
    n = dispatch_notification(conn, event_type="action_terminal",
                              action_id=1, db_path=str(my_db))
    assert n == 0


def test_dispatch_writes_one_row_per_binding(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    conn = connect(str(db))
    init_schema(conn)

    cfg = _basic_v2(db, bot_id="bot_x")
    # Add a second binding for a different bot — both should receive.
    extra_binding = BotBinding(id="bind2", bot_id="bot_y",
                               scope="destination", destination_id="dest_a")
    cfg2 = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles, channels=cfg.channels,
        destinations=cfg.destinations, bots=cfg.bots,
        routes=cfg.routes,
        bot_bindings=cfg.bot_bindings + (extra_binding,),
    )
    _write_cfg(appdata, cfg2)

    # Insert a real action row so bot_outbox FK is satisfied.
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid

    n = dispatch_notification(
        conn, event_type="action_terminal",
        action_id=aid, db_path=str(db),
        source_channel_id="ch_x", route_id="r_x",
    )
    assert n == 2
    rows = conn.execute(
        "SELECT bot_id, action_id, source_channel_id, route_id, event_type "
        "FROM bot_outbox ORDER BY bot_id"
    ).fetchall()
    assert [r["bot_id"] for r in rows] == ["bot_x", "bot_y"]
    for r in rows:
        assert r["action_id"] == aid
        assert r["source_channel_id"] == "ch_x"
        assert r["route_id"] == "r_x"
        assert r["event_type"] == "action_terminal"


def test_dispatch_global_scope_binding_matches(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    conn = connect(str(db))
    init_schema(conn)
    _write_cfg(appdata, _basic_v2(db, bot_id="bot_global", binding_scope="global"))
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('ALERT', '{}', 'executed')"
    )
    aid = cur.lastrowid

    n = dispatch_notification(conn, event_type="alert",
                              action_id=aid, db_path=str(db))
    assert n == 1
    row = conn.execute("SELECT bot_id FROM bot_outbox").fetchone()
    assert row["bot_id"] == "bot_global"


def test_dispatch_extra_payload_serialized(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    conn = connect(str(db))
    init_schema(conn)
    _write_cfg(appdata, _basic_v2(db))
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('ALERT', '{}', 'executed')"
    )
    aid = cur.lastrowid

    dispatch_notification(
        conn, event_type="alert", action_id=aid,
        db_path=str(db),
        extra_payload={"level": "warning", "text": "EA disconnected"},
    )
    row = conn.execute("SELECT event_payload FROM bot_outbox").fetchone()
    payload = json.loads(row["event_payload"])
    assert payload == {"level": "warning", "text": "EA disconnected"}


def test_dispatch_position_closed_event(monkeypatch, tmp_path: Path):
    """Day-3 cleanup: position-close events go through dispatch_notification."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    conn = connect(str(db))
    init_schema(conn)
    _write_cfg(appdata, _basic_v2(db))
    # Need an action row for the FK; position references that.
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid

    n = dispatch_notification(
        conn, event_type="position_closed",
        action_id=aid, db_path=str(db),
        extra_payload={"position_id": 42},
    )
    assert n == 1
    row = conn.execute(
        "SELECT event_type, action_id, event_payload FROM bot_outbox"
    ).fetchone()
    assert row["event_type"] == "position_closed"
    assert row["action_id"] == aid
    assert json.loads(row["event_payload"]) == {"position_id": 42}


# ---- has_v2_binding_for_destination ---------------------------------------


def test_has_binding_true_when_destination_scope(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    _write_cfg(appdata, _basic_v2(db))
    assert has_v2_binding_for_destination(db) is True


def test_has_binding_false_when_no_v2(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert has_v2_binding_for_destination(tmp_path / "x.db") is False


def test_has_binding_false_when_destination_not_in_config(
    monkeypatch, tmp_path: Path,
):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    other = tmp_path / "other.db"
    _write_cfg(appdata, _basic_v2(other))
    assert has_v2_binding_for_destination(tmp_path / "mine.db") is False


# ---- resolve_bot_id_for_destination ---------------------------------------


def test_resolve_bot_id_prefers_destination_over_global(
    monkeypatch, tmp_path: Path,
):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    cfg = _basic_v2(db, bot_id="bot_dest")
    global_binding = BotBinding(id="bg", bot_id="bot_global", scope="global")
    cfg2 = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles, channels=cfg.channels,
        destinations=cfg.destinations, bots=cfg.bots, routes=cfg.routes,
        bot_bindings=cfg.bot_bindings + (global_binding,),
    )
    _write_cfg(appdata, cfg2)
    assert resolve_bot_id_for_destination(db) == "bot_dest"


def test_resolve_bot_id_falls_back_to_global(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    db = tmp_path / "main.db"
    _write_cfg(appdata, _basic_v2(db, bot_id="bot_glo", binding_scope="global"))
    assert resolve_bot_id_for_destination(db) == "bot_glo"


def test_resolve_bot_id_none_when_no_match(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    assert resolve_bot_id_for_destination(tmp_path / "missing.db") is None
