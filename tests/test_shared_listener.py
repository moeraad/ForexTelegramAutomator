"""Tests for src.shared_listener (Step 5 of multi-channel plan).

Covers the v2-aware entry-point logic without touching Telethon — the
actual Telethon work is delegated to ``listener.main()`` (untouched in
Step 5) and is exercised by the existing integration tests.

What we test here:
  - v2 absent → delegates to legacy listener
  - v2 with 0 accounts → SystemExit with clear pointer
  - v2 with >1 accounts → SystemExit pointing at Step 13
  - v2 with 1 account / 0 channels → SystemExit
  - v2 with 1 account / >1 channels → SystemExit pointing at Step 6
  - v2 with 1 account / 1 enabled channel → calls legacy delegate
  - Disabled channels are filtered out before counting
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src import config_v2
from src.config_v2 import (
    Account,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)
from src.shared_listener import (
    _resolve_v2_or_none,
    _validate_current_scope,
    main as shared_main,
)


def _write_cfg(appdata: Path, cfg: ConfigV2) -> Path:
    path = appdata / "CopyTrades" / "stacks_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, path)
    return path


def _account(account_id: str = "acc_primary") -> Account:
    return Account(
        id=account_id, name="Primary", phone="+961",
        session_path="", service_name=f"CT-Listener-{account_id}",
    )


def _channel(channel_id: str, account_id: str, *, enabled: bool = True) -> Channel:
    return Channel(
        id=channel_id, name=channel_id, account_id=account_id,
        chat_id=-1, profile_id="p", enabled=enabled,
    )


# ---- _resolve_v2_or_none --------------------------------------------------


def test_resolve_returns_none_when_v2_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    # No file written → not v2.
    assert _resolve_v2_or_none() is None


def test_resolve_returns_none_when_only_v1_present(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(json.dumps({"stacks": []}), encoding="utf-8")
    assert _resolve_v2_or_none() is None


def test_resolve_returns_config_when_v2_present(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(accounts=(_account(),)))
    cfg = _resolve_v2_or_none()
    assert cfg is not None
    assert len(cfg.accounts) == 1


# ---- _validate_current_scope ----------------------------------------------


def test_validate_no_accounts_exits():
    with pytest.raises(SystemExit, match="no accounts"):
        _validate_current_scope(ConfigV2())


def test_validate_multi_account_without_account_id_exits():
    """Step 13: N>1 accounts requires --account-id on the command line."""
    cfg = ConfigV2(accounts=(_account("a1"), _account("a2")))
    with pytest.raises(SystemExit, match="MUST pass\\s+--account-id"):
        _validate_current_scope(cfg)


def test_validate_multi_account_unknown_id_exits():
    """An --account-id that doesn't match any configured account."""
    cfg = ConfigV2(accounts=(_account("a1"), _account("a2")))
    with pytest.raises(SystemExit, match="not found"):
        _validate_current_scope(cfg, account_id="acc_typo")


def test_validate_no_channels_exits():
    cfg = ConfigV2(accounts=(_account(),))
    with pytest.raises(SystemExit, match="no enabled channels"):
        _validate_current_scope(cfg)


def test_validate_disabled_channels_filtered_out():
    cfg = ConfigV2(
        accounts=(_account(),),
        channels=(_channel("ch_a", "acc_primary", enabled=False),),
    )
    with pytest.raises(SystemExit, match="no enabled channels"):
        _validate_current_scope(cfg)


def test_validate_single_enabled_channel_succeeds():
    cfg = ConfigV2(
        accounts=(_account(),),
        channels=(_channel("ch_a", "acc_primary"),),
    )
    account, channels = _validate_current_scope(cfg)
    assert account.id == "acc_primary"
    assert len(channels) == 1
    assert channels[0].id == "ch_a"


def test_validate_multi_channel_returns_all_for_step6():
    """Validation itself permits multiple channels; main() enforces the
    Step 5 single-channel limit when delegating to the legacy path."""
    cfg = ConfigV2(
        accounts=(_account(),),
        channels=(
            _channel("ch_a", "acc_primary"),
            _channel("ch_b", "acc_primary"),
        ),
    )
    account, channels = _validate_current_scope(cfg)
    assert len(channels) == 2


# ---- main() dispatch ------------------------------------------------------


def test_main_delegates_to_legacy_when_v2_absent(monkeypatch, tmp_path: Path):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    called = {"n": 0}

    async def fake_legacy():
        called["n"] += 1

    monkeypatch.setattr("src.listener.main", fake_legacy)
    asyncio.run(shared_main())
    assert called["n"] == 1


def test_main_delegates_to_legacy_when_single_channel(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account(),),
        channels=(_channel("ch_a", "acc_primary"),),
    ))
    called = {"n": 0}

    async def fake_legacy():
        called["n"] += 1

    monkeypatch.setattr("src.listener.main", fake_legacy)
    asyncio.run(shared_main())
    assert called["n"] == 1


def test_main_exits_on_multi_account_without_account_id(
    monkeypatch, tmp_path: Path,
):
    """Step 13: a multi-account config without --account-id is ambiguous."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account("a1"), _account("a2")),
    ))
    # No --account-id in argv → SystemExit
    monkeypatch.setattr("sys.argv", ["shared_listener"])
    with pytest.raises(SystemExit, match="MUST pass\\s+--account-id"):
        asyncio.run(shared_main())


def test_main_routes_multi_channel_to_new_runner(monkeypatch, tmp_path: Path):
    """Step 6: multi-channel no longer raises; it invokes _run_multi_channel."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account(),),
        channels=(
            _channel("ch_a", "acc_primary"),
            _channel("ch_b", "acc_primary"),
        ),
    ))
    captured: dict = {}

    async def fake_runner(account, channels, cfg):
        captured["account"] = account.id
        captured["channels"] = [c.id for c in channels]

    async def boom_if_called():
        raise AssertionError("legacy path should NOT fire when multi-channel runner present")

    monkeypatch.setattr("src.shared_listener._run_multi_channel", fake_runner)
    monkeypatch.setattr("src.listener.main", boom_if_called)
    asyncio.run(shared_main())
    assert captured["account"] == "acc_primary"
    assert captured["channels"] == ["ch_a", "ch_b"]


def test_main_exits_on_account_with_no_enabled_channels(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account(),),
        channels=(_channel("ch_a", "acc_primary", enabled=False),),
    ))
    with pytest.raises(SystemExit, match="no enabled channels"):
        asyncio.run(shared_main())
