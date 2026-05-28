"""Tests for the v2 config view (Step 9 of multi-channel plan).

The Qt view itself is exercised by a smoke test (constructs without
crashing). All business logic lives in ``apply_add_channel`` — a pure
function — which is tested directly without Qt.
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
from src.gui.views.v2_config_view import apply_add_channel


def _baseline_cfg() -> ConfigV2:
    """A minimal but complete v2 config for tests to extend."""
    return ConfigV2(
        accounts=(Account(
            id="acc_a", name="Primary", phone="+961",
            session_path="x.session", service_name="CT-Listener-acc_a",
        ),),
        profiles=(Profile(
            id="prof_fe", name="Forex Engineer", path="/p.json",
            language="ar", symbol="XAUUSD",
        ),),
        channels=(Channel(
            id="ch_fe", name="FE", account_id="acc_a",
            chat_id=-1001, profile_id="prof_fe",
        ),),
        destinations=(Destination(
            id="dest_main", name="Main", db_path="/main.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-Main",
        ),),
        bots=(Bot(
            id="bot_main", name="Main Bot",
            token_setting_key="tg_bot_token",
            service_name="CT-Bot-Main",
        ),),
        routes=(Route(id="r_fe", channel_id="ch_fe", destination_id="dest_main"),),
        bot_bindings=(BotBinding(
            id="bind_fe", bot_id="bot_main", scope="destination",
            destination_id="dest_main",
        ),),
    )


# ---- apply_add_channel: happy path ----------------------------------------


def test_add_channel_appends_channel_route_binding():
    cfg = _baseline_cfg()
    new_cfg = apply_add_channel(
        cfg, name="SMC Daily", chat_id=-1002,
        account_id="acc_a", profile_id="prof_fe",
        destination_id="dest_main", bot_id="bot_main",
    )
    assert len(new_cfg.channels) == 2
    assert len(new_cfg.routes) == 2
    assert len(new_cfg.bot_bindings) == 2
    new_channel = new_cfg.channels[-1]
    assert new_channel.name == "SMC Daily"
    assert new_channel.chat_id == -1002
    assert new_channel.account_id == "acc_a"
    assert new_channel.profile_id == "prof_fe"
    assert new_channel.enabled is True
    new_route = new_cfg.routes[-1]
    assert new_route.channel_id == new_channel.id
    assert new_route.destination_id == "dest_main"
    new_binding = new_cfg.bot_bindings[-1]
    assert new_binding.bot_id == "bot_main"
    assert new_binding.scope == "destination"
    assert new_binding.destination_id == "dest_main"


def test_add_channel_generates_unique_ids_when_name_clashes():
    cfg = _baseline_cfg()
    # First add with name "SMC Daily" creates ch_smc_daily / route_smc_daily etc.
    cfg = apply_add_channel(
        cfg, name="SMC Daily", chat_id=-1002,
        account_id="acc_a", profile_id="prof_fe",
        destination_id="dest_main", bot_id="bot_main",
    )
    # Second add with the SAME name should get _2 suffix.
    cfg = apply_add_channel(
        cfg, name="SMC Daily", chat_id=-1003,
        account_id="acc_a", profile_id="prof_fe",
        destination_id="dest_main", bot_id="bot_main",
    )
    ids = [c.id for c in cfg.channels]
    assert ids == ["ch_fe", "ch_smc_daily", "ch_smc_daily_2"]


def test_add_channel_slugifies_name():
    cfg = _baseline_cfg()
    cfg = apply_add_channel(
        cfg, name="Hello World 123!", chat_id=-1002,
        account_id="acc_a", profile_id="prof_fe",
        destination_id="dest_main", bot_id="bot_main",
    )
    assert cfg.channels[-1].id == "ch_hello_world_123"


# ---- apply_add_channel: validation -----------------------------------------


def test_rejects_blank_name():
    with pytest.raises(ValueError, match="Name"):
        apply_add_channel(
            _baseline_cfg(), name="", chat_id=-1002,
            account_id="acc_a", profile_id="prof_fe",
            destination_id="dest_main", bot_id="bot_main",
        )


def test_rejects_zero_chat_id():
    with pytest.raises(ValueError, match="chat_id"):
        apply_add_channel(
            _baseline_cfg(), name="X", chat_id=0,
            account_id="acc_a", profile_id="prof_fe",
            destination_id="dest_main", bot_id="bot_main",
        )


def test_rejects_duplicate_chat_id():
    cfg = _baseline_cfg()
    with pytest.raises(ValueError, match="already used"):
        apply_add_channel(
            cfg, name="Dup", chat_id=-1001,  # collides with ch_fe
            account_id="acc_a", profile_id="prof_fe",
            destination_id="dest_main", bot_id="bot_main",
        )


def test_rejects_unknown_account():
    with pytest.raises(ValueError, match="account"):
        apply_add_channel(
            _baseline_cfg(), name="X", chat_id=-1002,
            account_id="acc_missing", profile_id="prof_fe",
            destination_id="dest_main", bot_id="bot_main",
        )


def test_rejects_unknown_profile():
    with pytest.raises(ValueError, match="profile"):
        apply_add_channel(
            _baseline_cfg(), name="X", chat_id=-1002,
            account_id="acc_a", profile_id="prof_missing",
            destination_id="dest_main", bot_id="bot_main",
        )


def test_rejects_unknown_destination():
    with pytest.raises(ValueError, match="destination"):
        apply_add_channel(
            _baseline_cfg(), name="X", chat_id=-1002,
            account_id="acc_a", profile_id="prof_fe",
            destination_id="dest_missing", bot_id="bot_main",
        )


def test_rejects_unknown_bot():
    with pytest.raises(ValueError, match="bot"):
        apply_add_channel(
            _baseline_cfg(), name="X", chat_id=-1002,
            account_id="acc_a", profile_id="prof_fe",
            destination_id="dest_main", bot_id="bot_missing",
        )


# ---- Round-trip with config_v2.save_v2 ------------------------------------


def test_added_channel_round_trips_to_disk(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = _baseline_cfg()
    config_v2.save_v2(cfg)

    cfg2 = apply_add_channel(
        cfg, name="SMC", chat_id=-1002,
        account_id="acc_a", profile_id="prof_fe",
        destination_id="dest_main", bot_id="bot_main",
    )
    config_v2.save_v2(cfg2)

    reloaded = config_v2.load_v2(config_v2.config_path())
    assert reloaded is not None
    assert len(reloaded.channels) == 2
    assert len(reloaded.routes) == 2
    assert len(reloaded.bot_bindings) == 2


# Note: Qt-view smoke tests live in tests/gui/test_v2_config_view_smoke.py
# (the tmp_stack fixture is only available under tests/gui/).
