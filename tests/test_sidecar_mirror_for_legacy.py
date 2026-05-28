"""Sidecar → dest DB mirror before legacy listener delegation.

The shared_listener delegates to the legacy listener.main() for single-
channel-per-account stacks. That legacy code reads tg_session_blob /
tg_api_id / tg_api_hash from config.DB_PATH (the dest DB). For accounts
created via the v2 Add Account dialog, those values live in sidecar
files — the mirror surfaces them into the dest DB so legacy code works
unchanged.
"""
from __future__ import annotations

import json
from pathlib import Path

from src import config_v2, db_settings
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
from src.shared_listener import _mirror_sidecar_to_dest_db_if_needed


def _seed_sidecar_for_account(
    monkeypatch, tmp_path: Path, account_id: str,
    *, api_id: int, api_hash: str, session: str,
) -> Path:
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    accounts_dir = appdata / "CopyTrades" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / f"{account_id}.creds.json").write_text(
        json.dumps({"api_id": api_id, "api_hash": api_hash}),
        encoding="utf-8",
    )
    sess_file = accounts_dir / f"{account_id}.session.txt"
    sess_file.write_text(session, encoding="utf-8")
    return sess_file


def _full_cfg(db_path: Path, sess_file: Path) -> ConfigV2:
    return ConfigV2(
        accounts=(Account(
            id="acc_a", name="A", phone="+1",
            session_path=str(sess_file),
            service_name="CT-Listener-acc_a",
        ),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1001, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B-Bot"),),
        routes=(Route(id="r", channel_id="ch_a",
                      destination_id="dest_x"),),
        bot_bindings=(BotBinding(id="bind", bot_id="b",
                                 scope="destination",
                                 destination_id="dest_x"),),
    )


def test_mirror_copies_sidecar_to_empty_dest_db(monkeypatch, tmp_path: Path):
    sess_file = _seed_sidecar_for_account(
        monkeypatch, tmp_path, "acc_a",
        api_id=11111, api_hash="SIDECARHASH11", session="SIDECARBLOB",
    )
    db_path = tmp_path / "appdata" / "CopyTrades" / "dest_x" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    cfg = _full_cfg(db_path, sess_file)

    # Before mirror: dest DB empty.
    assert db_settings.get_str(db_path, "tg_session_blob", "") == ""

    _mirror_sidecar_to_dest_db_if_needed(cfg.accounts[0], cfg.channels, cfg)

    # After mirror: legacy listener will find the keys it needs.
    assert db_settings.get_str(db_path, "tg_session_blob", "") == "SIDECARBLOB"
    assert db_settings.get_str(db_path, "tg_api_id", "") == "11111"
    assert db_settings.get_str(db_path, "tg_api_hash", "") == "SIDECARHASH11"


def test_mirror_is_noop_when_dest_already_seeded(monkeypatch, tmp_path: Path):
    """Once the dest DB has tg_session_blob, the mirror skips — no point
    overwriting the fast path."""
    sess_file = _seed_sidecar_for_account(
        monkeypatch, tmp_path, "acc_a",
        api_id=11111, api_hash="SIDECARHASH11", session="SIDECARBLOB",
    )
    db_path = tmp_path / "appdata" / "CopyTrades" / "dest_x" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    # Seed the DB with DIFFERENT creds.
    db_settings.set_str(db_path, "tg_api_id", "99999")
    db_settings.set_str(db_path, "tg_api_hash", "DBHASHDBHASH")
    db_settings.set_str(db_path, "tg_session_blob", "DBBLOB")

    cfg = _full_cfg(db_path, sess_file)
    _mirror_sidecar_to_dest_db_if_needed(cfg.accounts[0], cfg.channels, cfg)

    # DB creds preserved — sidecar didn't overwrite.
    assert db_settings.get_str(db_path, "tg_api_id", "") == "99999"
    assert db_settings.get_str(db_path, "tg_session_blob", "") == "DBBLOB"


def test_mirror_silent_no_op_when_no_sidecar(monkeypatch, tmp_path: Path):
    """No sidecar files = nothing to mirror; dest DB stays empty; the
    legacy listener's polling loop will surface the issue."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    db_path = tmp_path / "dest_x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    cfg = _full_cfg(db_path, tmp_path / "no.session.txt")
    # Doesn't raise.
    _mirror_sidecar_to_dest_db_if_needed(cfg.accounts[0], cfg.channels, cfg)
    assert db_settings.get_str(db_path, "tg_session_blob", "") == ""


def test_mirror_silent_no_op_when_dest_missing(monkeypatch, tmp_path: Path):
    """Destination's db_path doesn't exist on disk → mirror silently skips."""
    sess_file = _seed_sidecar_for_account(
        monkeypatch, tmp_path, "acc_a",
        api_id=1, api_hash="h", session="b",
    )
    missing = tmp_path / "no_such" / "db.db"
    cfg = _full_cfg(missing, sess_file)
    # Doesn't raise.
    _mirror_sidecar_to_dest_db_if_needed(cfg.accounts[0], cfg.channels, cfg)
