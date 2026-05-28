"""Per-account credential resolution tests."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

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
from src.gui.services.account_credentials import (
    AccountCreds,
    load_account_credentials,
)


def _cfg_with_dest(db_path: Path) -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="+1",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-X",
        ),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B"),),
        routes=(Route(id="r", channel_id="ch_a",
                      destination_id="dest_x"),),
        bot_bindings=(BotBinding(id="bind", bot_id="b",
                                 scope="destination",
                                 destination_id="dest_x"),),
    )


def _write_sidecar(monkeypatch, tmp_path: Path, account_id: str,
                   *, api_id: int, api_hash: str, session: str) -> None:
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    accounts_dir = appdata / "CopyTrades" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / f"{account_id}.creds.json").write_text(
        json.dumps({"api_id": api_id, "api_hash": api_hash}),
        encoding="utf-8",
    )
    (accounts_dir / f"{account_id}.session.txt").write_text(
        session, encoding="utf-8",
    )


def _patch_account_session_path(cfg: ConfigV2, tmp_path: Path) -> ConfigV2:
    """Point Account.session_path at the sidecar session file."""
    from dataclasses import replace
    sess = tmp_path / "appdata" / "CopyTrades" / "accounts" / "acc_a.session.txt"
    new_acc = replace(cfg.accounts[0], session_path=str(sess))
    return replace(cfg, accounts=(new_acc, *cfg.accounts[1:]))


def test_resolves_from_sidecar_when_available(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    cfg = _cfg_with_dest(db_path)
    _write_sidecar(monkeypatch, tmp_path, "acc_a",
                   api_id=111, api_hash="HASHHASHHASH", session="BLOB")
    cfg = _patch_account_session_path(cfg, tmp_path)
    creds = load_account_credentials(cfg, cfg.accounts[0])
    assert creds is not None
    assert creds.api_id == 111
    assert creds.api_hash == "HASHHASHHASH"
    assert creds.session_blob == "BLOB"


def test_falls_back_to_destination_db_when_no_sidecar(
    monkeypatch, tmp_path: Path,
):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    # Seed the destination DB the way the wizard does.
    from src import db_settings
    db_settings.set_str(db_path, "tg_api_id", "222")
    db_settings.set_str(db_path, "tg_api_hash", "DBHASHDBHASHDBHASH")
    db_settings.set_str(db_path, "tg_session_blob", "DBBLOB")
    # No APPDATA sidecar.
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    cfg = _cfg_with_dest(db_path)
    creds = load_account_credentials(cfg, cfg.accounts[0])
    assert creds is not None
    assert creds.api_id == 222
    assert creds.session_blob == "DBBLOB"


def test_returns_none_when_nothing_available(monkeypatch, tmp_path: Path):
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    # No sidecar, no DB creds.
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    cfg = _cfg_with_dest(db_path)
    assert load_account_credentials(cfg, cfg.accounts[0]) is None


def test_returns_none_when_no_destination_and_no_sidecar(
    monkeypatch, tmp_path: Path,
):
    """Fresh account with no channels wired → no DB to fall back to."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    cfg = ConfigV2(accounts=(Account(
        id="acc_a", name="A", phone="+1", session_path="",
        service_name="CT-Listener-acc_a",
    ),))
    assert load_account_credentials(cfg, cfg.accounts[0]) is None


def test_falls_back_to_phone_match_when_no_channels_wired(
    monkeypatch, tmp_path: Path,
):
    """The Add Channel dialog case: account has NO channels yet, but
    its phone matches a destination DB the wizard already populated."""
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    from src import db_settings
    db_settings.set_str(db_path, "tg_api_id", "444")
    db_settings.set_str(db_path, "tg_api_hash", "BYPHONEHASH4444")
    db_settings.set_str(db_path, "tg_session_blob", "BYPHONEBLOB")
    db_settings.set_str(db_path, "tg_phone", "+961 1234567")  # formatting drift

    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))

    # Account has the SAME phone but no channels routing to this dest.
    cfg = ConfigV2(
        accounts=(Account(
            id="acc_a", name="A", phone="9611234567",  # digits-only variant
            session_path="", service_name="CT-Listener-acc_a",
        ),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-X",
        ),),
    )
    creds = load_account_credentials(cfg, cfg.accounts[0])
    assert creds is not None
    assert creds.api_id == 444
    assert creds.session_blob == "BYPHONEBLOB"


def test_falls_back_to_any_session_when_no_phone_anywhere(
    monkeypatch, tmp_path: Path,
):
    """Single-account install where the wizard didn't record tg_phone:
    fall through to any destination DB with a non-empty session blob."""
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    from src import db_settings
    db_settings.set_str(db_path, "tg_api_id", "555")
    db_settings.set_str(db_path, "tg_api_hash", "ANYSESSIONHASH5555")
    db_settings.set_str(db_path, "tg_session_blob", "ANYBLOB")
    # NOTE: no tg_phone — earlier wizard versions didn't record it.

    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))

    cfg = ConfigV2(
        accounts=(Account(
            id="acc_a", name="A", phone="",  # account also no phone
            session_path="", service_name="CT-Listener-acc_a",
        ),),
        destinations=(Destination(
            id="dest_x", name="X", db_path=str(db_path),
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-X",
        ),),
    )
    creds = load_account_credentials(cfg, cfg.accounts[0])
    assert creds is not None
    assert creds.api_id == 555
    assert creds.session_blob == "ANYBLOB"


def test_phone_normalization_strips_separators(monkeypatch, tmp_path: Path):
    from src.gui.services.account_credentials import _normalize_phone
    assert _normalize_phone("+961 12-34 567") == "9611234567"
    assert _normalize_phone("") == ""
    assert _normalize_phone(None) == ""  # type: ignore[arg-type]


def test_sidecar_partial_data_still_falls_back_to_db(
    monkeypatch, tmp_path: Path,
):
    """If the sidecar exists but has only api_id (no api_hash), the
    helper falls through to the DB. Avoids using partially-populated
    sidecars from earlier broken installs."""
    db_path = tmp_path / "x.db"
    conn = connect(str(db_path))
    init_schema(conn)
    from src import db_settings
    db_settings.set_str(db_path, "tg_api_id", "333")
    db_settings.set_str(db_path, "tg_api_hash", "DBHASH33333333333")
    db_settings.set_str(db_path, "tg_session_blob", "DBBLOB")
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    accounts_dir = appdata / "CopyTrades" / "accounts"
    accounts_dir.mkdir(parents=True, exist_ok=True)
    (accounts_dir / "acc_a.creds.json").write_text(
        json.dumps({"api_id": 999}),  # api_hash missing
        encoding="utf-8",
    )
    cfg = _cfg_with_dest(db_path)
    creds = load_account_credentials(cfg, cfg.accounts[0])
    assert creds is not None
    # Falls back to DB values.
    assert creds.api_id == 333
    assert creds.api_hash == "DBHASH33333333333"
