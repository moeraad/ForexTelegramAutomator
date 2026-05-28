"""Multi-account listener tests (Step 13 of multi-channel plan).

Validates that the shared listener can serve a SPECIFIC account from a
v2 config containing multiple accounts. Each NSSM listener service is
registered with a ``--account-id`` flag so its process binds to one
Telegram user account. Crashes / session expiries in account A don't
affect account B (they're separate OS processes).

Covers:
  - `_parse_account_id_arg`: argv parsing
  - `_validate_current_scope(cfg, account_id=...)`: routing each
    process to the right account
  - `main()` with --account-id runs the chosen account
  - Single-account back-compat: no --account-id needed when N=1
  - `bootstrap_services_install` passes --account-id correctly
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src import config_v2
from src.config_v2 import (
    Account,
    Channel,
    ConfigV2,
)
from src.shared_listener import (
    _parse_account_id_arg,
    _validate_current_scope,
    main as shared_main,
)


def _account(account_id: str = "acc_primary", name: str = "P") -> Account:
    return Account(
        id=account_id, name=name, phone="", session_path="",
        service_name=f"CT-Listener-{account_id}",
    )


def _channel(
    channel_id: str, account_id: str, *,
    chat_id: int = -1, profile_id: str = "p", enabled: bool = True,
) -> Channel:
    return Channel(
        id=channel_id, name=channel_id, account_id=account_id,
        chat_id=chat_id, profile_id=profile_id, enabled=enabled,
    )


def _write_cfg(appdata: Path, cfg: ConfigV2) -> Path:
    path = appdata / "CopyTrades" / "stacks_config.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(cfg, path)
    return path


# ---- _parse_account_id_arg ------------------------------------------------


def test_parse_account_id_returns_value_when_present():
    assert _parse_account_id_arg(
        ["shared_listener", "--account-id", "acc_a"],
    ) == "acc_a"


def test_parse_account_id_returns_none_when_absent():
    assert _parse_account_id_arg(["shared_listener"]) is None


def test_parse_account_id_returns_none_when_value_missing():
    """--account-id at the end with no value → None (not crash)."""
    assert _parse_account_id_arg(["shared_listener", "--account-id"]) is None


def test_parse_account_id_strips_whitespace():
    assert _parse_account_id_arg(
        ["shared_listener", "--account-id", "  acc_a  "],
    ) == "acc_a"


def test_parse_account_id_returns_none_when_value_blank():
    """Empty string after --account-id behaves like missing arg."""
    assert _parse_account_id_arg(
        ["shared_listener", "--account-id", ""],
    ) is None


# ---- _validate_current_scope with account_id -----------------------------


def test_validate_picks_specified_account_from_multi():
    cfg = ConfigV2(
        accounts=(_account("a1"), _account("a2")),
        channels=(
            _channel("ch_a1_1", "a1"),
            _channel("ch_a2_1", "a2"),
        ),
    )
    account, channels = _validate_current_scope(cfg, account_id="a1")
    assert account.id == "a1"
    # Only a1's channels are returned.
    assert [c.id for c in channels] == ["ch_a1_1"]


def test_validate_single_account_works_without_account_id():
    """Step 5 back-compat: no --account-id needed when only one account."""
    cfg = ConfigV2(
        accounts=(_account(),),
        channels=(_channel("ch_a", "acc_primary"),),
    )
    account, channels = _validate_current_scope(cfg)
    assert account.id == "acc_primary"


def test_validate_picks_account_with_no_channels_exits():
    """Even with --account-id, an account with zero enabled channels exits."""
    cfg = ConfigV2(
        accounts=(_account("a1"), _account("a2")),
        channels=(_channel("ch_only", "a1"),),
    )
    # a2 has no channels at all
    with pytest.raises(SystemExit, match="no enabled channels"):
        _validate_current_scope(cfg, account_id="a2")


def test_validate_filters_disabled_channels():
    cfg = ConfigV2(
        accounts=(_account("a1"), _account("a2")),
        channels=(
            _channel("ch_a1_1", "a1"),
            _channel("ch_a1_2", "a1", enabled=False),
            _channel("ch_a2", "a2"),
        ),
    )
    _, channels = _validate_current_scope(cfg, account_id="a1")
    assert [c.id for c in channels] == ["ch_a1_1"]


# ---- main() routes per --account-id --------------------------------------


def test_main_with_account_id_runs_only_that_account(
    monkeypatch, tmp_path: Path,
):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account("a1"), _account("a2")),
        channels=(_channel("ch_a1", "a1"),),
    ))
    monkeypatch.setattr("sys.argv", ["shared_listener", "--account-id", "a1"])

    called = {"n": 0}

    async def fake_legacy():
        called["n"] += 1

    monkeypatch.setattr("src.listener.main", fake_legacy)
    asyncio.run(shared_main())
    assert called["n"] == 1


def test_main_with_unknown_account_id_exits(monkeypatch, tmp_path: Path):
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account("a1"),),
        channels=(_channel("ch_a1", "a1"),),
    ))
    monkeypatch.setattr("sys.argv", ["shared_listener", "--account-id", "acc_typo"])
    with pytest.raises(SystemExit, match="not found"):
        asyncio.run(shared_main())


def test_main_single_account_works_without_account_id_arg(
    monkeypatch, tmp_path: Path,
):
    """Step 5 back-compat: single-account installs don't need --account-id."""
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    _write_cfg(appdata, ConfigV2(
        accounts=(_account(),),
        channels=(_channel("ch_a", "acc_primary"),),
    ))
    monkeypatch.setattr("sys.argv", ["shared_listener"])

    called = {"n": 0}

    async def fake_legacy():
        called["n"] += 1

    monkeypatch.setattr("src.listener.main", fake_legacy)
    asyncio.run(shared_main())
    assert called["n"] == 1


# ---- bootstrap install passes --account-id -------------------------------


def test_install_helper_extracts_account_id_from_listener_svc():
    from src.gui.helpers.bootstrap_services_install import (
        _account_id_from_listener_svc,
    )
    assert _account_id_from_listener_svc("CT-Listener-acc_primary") == "acc_primary"
    assert _account_id_from_listener_svc("CT-Listener-acc_9611234567") == "acc_9611234567"


def test_install_helper_returns_none_for_non_matching_name():
    from src.gui.helpers.bootstrap_services_install import (
        _account_id_from_listener_svc,
    )
    # Legacy per-stack pattern (Step 8 should have migrated these away).
    assert _account_id_from_listener_svc("CT-FOREXENGINEER-Listener") is None
    # Bot or API service (not a listener).
    assert _account_id_from_listener_svc("CT-FOREXENGINEER-Api") is None
    # Pathological cases.
    assert _account_id_from_listener_svc("CT-Listener-") is None
    assert _account_id_from_listener_svc("") is None


def test_service_args_includes_account_id_for_shared_listener():
    from src.gui.helpers.bootstrap_services_install import _service_args
    args = _service_args(
        "frozen", "src.shared_listener", db_path="X",
        account_id="acc_a1",
    )
    assert args == [
        "--service", "shared-listener",
        "--db-path", "X",
        "--account-id", "acc_a1",
    ]


def test_service_args_omits_account_id_for_api_or_bot():
    """Only the shared_listener slot gets --account-id; api/bot don't."""
    from src.gui.helpers.bootstrap_services_install import _service_args
    api_args = _service_args(
        "frozen", "src.api", db_path="X", account_id="acc_a1",
    )
    bot_args = _service_args(
        "frozen", "src.bot", db_path="X", account_id="acc_a1",
    )
    assert "--account-id" not in api_args
    assert "--account-id" not in bot_args


def test_service_args_omits_account_id_when_none():
    """Single-account install: no account_id passed = no --account-id arg."""
    from src.gui.helpers.bootstrap_services_install import _service_args
    args = _service_args(
        "frozen", "src.shared_listener", db_path="X", account_id=None,
    )
    assert "--account-id" not in args
