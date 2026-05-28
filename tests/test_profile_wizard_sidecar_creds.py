"""Profile-generator-wizard credential resolution (post-v2-cleanup).

The wizard's _fetch_history now resolves Telethon creds via two paths:
  1. Legacy: dest DB settings (Setup wizard's write target).
  2. v2: sidecar files written by the Add Account dialog.

Tests confirm the v2 fallback fires when the dest DB is empty.
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
from src.gui.services.profile_wizard import _resolve_telethon_credentials


def _seed_v2_with_sidecar(
    monkeypatch, tmp_path: Path,
    *, api_id: int = 12345, api_hash: str = "HASHHASHHASH",
    session: str = "SIDECARSESSIONBLOB",
) -> Path:
    """Write a v2 cfg + sidecar files; return the destination db_path."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    accounts_dir = appdata / "CopyTrades" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "acc_a.creds.json").write_text(
        json.dumps({"api_id": api_id, "api_hash": api_hash}),
        encoding="utf-8",
    )
    sess_file = accounts_dir / "acc_a.session.txt"
    sess_file.write_text(session, encoding="utf-8")

    db_path = appdata / "CopyTrades" / "dest_x" / "copytrades.db"
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    cfg = ConfigV2(
        accounts=(Account(
            id="acc_a", name="A", phone="+1",
            session_path=str(sess_file),
            service_name="CT-Listener-acc_a",
        ),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1, profile_id="p"),),
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
    config_v2.save_v2(cfg)
    return db_path


def test_resolves_from_dest_db_when_seeded_legacy_style(tmp_path: Path):
    """Pre-v2 / wizard-seeded path: creds in db_settings → use them."""
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    from src import db_settings
    db_settings.set_str(db_path, "tg_api_id", "999")
    db_settings.set_str(db_path, "tg_api_hash", "LEGACYHASHLEGACY")
    db_settings.set_str(db_path, "tg_session_blob", "LEGACYBLOB")

    api_id, api_hash, blob = _resolve_telethon_credentials(db_path)
    assert api_id == 999
    assert api_hash == "LEGACYHASHLEGACY"
    assert blob == "LEGACYBLOB"


def test_resolves_from_sidecar_when_dest_db_empty(monkeypatch, tmp_path: Path):
    """v2 Add Account path: dest DB has no creds, sidecar files do."""
    db_path = _seed_v2_with_sidecar(monkeypatch, tmp_path)
    api_id, api_hash, blob = _resolve_telethon_credentials(db_path)
    assert api_id == 12345
    assert api_hash == "HASHHASHHASH"
    assert blob == "SIDECARSESSIONBLOB"


def test_raises_with_helpful_message_when_no_creds_anywhere(
    monkeypatch, tmp_path: Path,
):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    with pytest.raises(RuntimeError, match="credentials missing"):
        _resolve_telethon_credentials(db_path)


def test_dest_db_wins_over_sidecar(monkeypatch, tmp_path: Path):
    """When BOTH paths populate creds, the dest DB wins. Matches the
    listener's startup behavior: once the dest DB is mirrored, it's the
    fast path and the sidecar is just a bootstrap aid."""
    db_path = _seed_v2_with_sidecar(monkeypatch, tmp_path)
    from src import db_settings
    db_settings.set_str(db_path, "tg_api_id", "111")
    db_settings.set_str(db_path, "tg_api_hash", "DBHASHDBHASHDBHASH")
    db_settings.set_str(db_path, "tg_session_blob", "DBBLOB")
    api_id, api_hash, blob = _resolve_telethon_credentials(db_path)
    assert api_id == 111
    assert blob == "DBBLOB"
