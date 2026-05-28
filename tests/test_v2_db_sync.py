"""V2 entity → destination DB settings auto-sync.

Operators who skip the wizard and use V2 Config exclusively shouldn't
have to also paste tg_phone / tg_api_id / tg_api_hash / tg_watched_chat_id
into the Tuning tab. ``sync_v2_to_destination_dbs`` mirrors those
values from the v2 entities into the per-destination SQLite settings
table on every config save. Operator-supplied DB settings are never
overwritten.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import db, db_settings
from src.config_v2 import (
    Account, Bot, BotBinding, Channel, ConfigV2, Destination, Profile, Route,
)
from src.v2_db_sync import sync_v2_to_destination_dbs


def _init_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = db.connect(str(path))
    db.init_schema(conn)
    conn.close()


def _cfg(tmp_path: Path) -> ConfigV2:
    db_path = tmp_path / "dest_a" / "copytrades.db"
    _init_db(db_path)
    return ConfigV2(
        accounts=(Account(
            id="a1", name="A", phone="+14155551234",
            session_path="", service_name="CT-Listener-a1",
            api_id=12345678, api_hash="deadbeefdeadbeefdeadbeefdeadbeef",
        ),),
        profiles=(Profile(id="p1", name="P", path="/p.json"),),
        channels=(Channel(
            id="c1", name="Ch", account_id="a1",
            chat_id=-1001234567, profile_id="p1", enabled=True,
        ),),
        destinations=(Destination(
            id="d1", name="D", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-A-Api",
        ),),
        bots=(Bot(id="b1", name="B", token_setting_key="tg_bot_token",
                  service_name="CT-A-Bot"),),
        routes=(Route(id="r1", channel_id="c1", destination_id="d1",
                      enabled=True),),
        bot_bindings=(),
    )


def test_sync_populates_missing_settings(tmp_path: Path):
    cfg = _cfg(tmp_path)
    written = sync_v2_to_destination_dbs(cfg)
    db_path = Path(cfg.destinations[0].db_path)
    assert db_settings.get_str(db_path, "tg_phone", "") == "+14155551234"
    assert db_settings.get_str(db_path, "tg_api_id", "") == "12345678"
    assert db_settings.get_str(db_path, "tg_api_hash", "") == "deadbeefdeadbeefdeadbeefdeadbeef"
    assert db_settings.get_str(db_path, "tg_watched_chat_id", "") == "-1001234567"
    assert db_settings.get_str(db_path, "ai_provider", "") == "openai"
    assert "tg_phone" in written["d1"]
    assert "ai_provider" in written["d1"]


def test_sync_default_only_keys_preserve_operator_values(tmp_path: Path):
    """Soft-default keys (ai_provider) respect operator override."""
    cfg = _cfg(tmp_path)
    db_path = Path(cfg.destinations[0].db_path)
    db_settings.set_str(db_path, "ai_provider", "anthropic")
    sync_v2_to_destination_dbs(cfg)
    assert db_settings.get_str(db_path, "ai_provider", "") == "anthropic"


def test_sync_entity_authoritative_keys_overwrite_stale_db_values(tmp_path: Path):
    """Entity-authoritative keys (port, phone, chat_id) overwrite DB
    when v2 entity has a different value. Otherwise editing a v2
    Account/Channel/Destination would have no effect on running services
    until the operator separately re-saved Tuning — confusing."""
    cfg = _cfg(tmp_path)
    db_path = Path(cfg.destinations[0].db_path)
    db_settings.set_str(db_path, "tg_phone", "+19999999999")  # stale
    db_settings.set_str(db_path, "api_port", "8765")  # stale (entity says 8765 actually; force a different stale)
    db_settings.set_str(db_path, "tg_watched_chat_id", "-9")  # stale
    sync_v2_to_destination_dbs(cfg)
    # Entity values won.
    assert db_settings.get_str(db_path, "tg_phone", "") == "+14155551234"
    assert db_settings.get_str(db_path, "tg_watched_chat_id", "") == "-1001234567"


def test_sync_skips_destination_with_no_route(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # Drop the route — destination becomes orphan.
    from dataclasses import replace as _replace
    cfg = _replace(cfg, routes=())
    written = sync_v2_to_destination_dbs(cfg)
    assert written == {}


def test_sync_skips_destination_with_no_db_file(tmp_path: Path):
    cfg = _cfg(tmp_path)
    # Point destination at a path that doesn't exist.
    from dataclasses import replace as _replace
    new_dest = _replace(cfg.destinations[0],
                        db_path=str(tmp_path / "nope" / "x.db"))
    cfg = _replace(cfg, destinations=(new_dest,))
    written = sync_v2_to_destination_dbs(cfg)
    assert written == {}


def test_sync_skips_empty_values(tmp_path: Path):
    """Account with blank api_id/api_hash doesn't write empty rows."""
    cfg = _cfg(tmp_path)
    from dataclasses import replace as _replace
    blank_acct = _replace(cfg.accounts[0], api_id=0, api_hash="", phone="")
    cfg = _replace(cfg, accounts=(blank_acct,))
    db_path = Path(cfg.destinations[0].db_path)
    sync_v2_to_destination_dbs(cfg)
    # chat_id still set (it's on Channel, not Account).
    assert db_settings.get_str(db_path, "tg_watched_chat_id", "") == "-1001234567"
    # api fields stayed empty.
    assert db_settings.get_str(db_path, "tg_phone", "") == ""
    assert db_settings.get_str(db_path, "tg_api_id", "") == ""
