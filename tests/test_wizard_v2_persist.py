"""Wizard's v2 entity composer + finalizer.

The wizard now writes v2 entities directly to stacks_config.json
instead of round-tripping through the v1 → v2 migrator. These tests
cover the two pure helpers backing that path.
"""
from __future__ import annotations

from pathlib import Path

from src.config_v2 import ConfigV2
from src.gui.windows._wizard_v2_persist import (
    compose_wizard_entities,
    finalize_wizard_channel,
)


def test_compose_creates_all_four_entities_from_empty():
    cfg = compose_wizard_entities(
        None,
        stack_name="Forex Engineer",
        symbol="XAUUSD",
        db_path=Path("/x.db"),
        api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-FE-Api", "CT-FE-Bot", "CT-Listener-acc_fe"),
    )
    assert len(cfg.accounts) == 1
    assert len(cfg.profiles) == 1
    assert len(cfg.destinations) == 1
    assert len(cfg.bots) == 1
    # Channel/Route/BotBinding NOT yet — added in finalize after Telethon.
    assert len(cfg.channels) == 0
    assert len(cfg.routes) == 0
    assert len(cfg.bot_bindings) == 0


def test_compose_derives_stable_ids_from_stack_name():
    cfg = compose_wizard_entities(
        None,
        stack_name="My Stack",
        symbol="XAUUSD",
        db_path=Path("/x.db"),
        api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-MyStack-Api", "CT-MyStack-Bot",
                       "CT-Listener-acc_my_stack"),
    )
    assert cfg.accounts[0].id == "acc_my_stack"
    assert cfg.profiles[0].id == "prof_my_stack"
    assert cfg.destinations[0].id == "dest_my_stack"
    assert cfg.bots[0].id == "bot_my_stack"


def test_compose_uses_provided_service_names():
    cfg = compose_wizard_entities(
        None, stack_name="X", symbol="XAUUSD",
        db_path=Path("/x.db"), api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-X-Api", "CT-X-Bot", "CT-Listener-acc_x"),
    )
    assert cfg.destinations[0].service_name == "CT-X-Api"
    assert cfg.bots[0].service_name == "CT-X-Bot"
    assert cfg.accounts[0].service_name == "CT-Listener-acc_x"


def test_compose_appends_to_existing_cfg():
    """A second wizard run on an existing v2 config preserves prior entries."""
    base = compose_wizard_entities(
        None, stack_name="Alpha", symbol="XAUUSD",
        db_path=Path("/a.db"), api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/a.json"),
        service_names=("CT-A-Api", "CT-A-Bot", "CT-Listener-acc_alpha"),
    )
    extended = compose_wizard_entities(
        base, stack_name="Beta", symbol="EURUSD",
        db_path=Path("/b.db"), api_host="127.0.0.1", api_port=8766,
        profile_path=Path("/b.json"),
        service_names=("CT-B-Api", "CT-B-Bot", "CT-Listener-acc_beta"),
    )
    assert len(extended.accounts) == 2
    assert {a.id for a in extended.accounts} == {"acc_alpha", "acc_beta"}
    assert len(extended.destinations) == 2


def test_compose_idempotent_on_retry():
    """Re-running compose with the same name doesn't duplicate entities."""
    cfg1 = compose_wizard_entities(
        None, stack_name="X", symbol="XAUUSD",
        db_path=Path("/x.db"), api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-X-Api", "CT-X-Bot", "CT-Listener-acc_x"),
    )
    cfg2 = compose_wizard_entities(
        cfg1, stack_name="X", symbol="XAUUSD",
        db_path=Path("/x.db"), api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-X-Api", "CT-X-Bot", "CT-Listener-acc_x"),
    )
    assert len(cfg2.accounts) == 1
    assert len(cfg2.profiles) == 1
    assert len(cfg2.destinations) == 1
    assert len(cfg2.bots) == 1


def test_compose_defaults_symbol_to_xauusd_when_blank():
    cfg = compose_wizard_entities(
        None, stack_name="X", symbol="",
        db_path=Path("/x.db"), api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-X-Api", "CT-X-Bot", "CT-Listener-acc_x"),
    )
    assert cfg.profiles[0].symbol == "XAUUSD"


# ---- finalize_wizard_channel ------------------------------------------


def _base_cfg() -> ConfigV2:
    return compose_wizard_entities(
        None, stack_name="X", symbol="XAUUSD",
        db_path=Path("/x.db"), api_host="127.0.0.1", api_port=8765,
        profile_path=Path("/p.json"),
        service_names=("CT-X-Api", "CT-X-Bot", "CT-Listener-acc_x"),
    )


def test_finalize_adds_channel_route_binding():
    cfg = finalize_wizard_channel(
        _base_cfg(), stack_name="X", chat_id=-1001234,
        account_phone="+14155551234",
    )
    assert len(cfg.channels) == 1
    assert cfg.channels[0].chat_id == -1001234
    assert cfg.channels[0].account_id == "acc_x"
    assert cfg.channels[0].profile_id == "prof_x"
    assert len(cfg.routes) == 1
    assert cfg.routes[0].channel_id == "ch_x"
    assert cfg.routes[0].destination_id == "dest_x"
    assert len(cfg.bot_bindings) == 1
    assert cfg.bot_bindings[0].bot_id == "bot_x"
    assert cfg.bot_bindings[0].scope == "destination"


def test_finalize_backfills_account_phone():
    """compose ran before Telethon login captured phone; finalize fills it in."""
    cfg = finalize_wizard_channel(
        _base_cfg(), stack_name="X", chat_id=-1,
        account_phone="+14155551234",
    )
    assert cfg.accounts[0].phone == "+14155551234"


def test_finalize_does_not_overwrite_existing_phone():
    """If Account already has a phone (e.g. operator hand-edited config),
    don't clobber it on Finish."""
    cfg = _base_cfg()
    from dataclasses import replace as _replace
    cfg = ConfigV2(
        accounts=tuple(
            _replace(a, phone="+19999999999")
            for a in cfg.accounts
        ),
        profiles=cfg.profiles, channels=cfg.channels,
        destinations=cfg.destinations, bots=cfg.bots,
        routes=cfg.routes, bot_bindings=cfg.bot_bindings,
    )
    cfg = finalize_wizard_channel(
        cfg, stack_name="X", chat_id=-1,
        account_phone="+14155551234",
    )
    assert cfg.accounts[0].phone == "+19999999999"


def test_finalize_skips_channel_when_chat_id_zero():
    """If somehow the operator finishes without picking a channel
    (zero chat_id), don't create a broken Channel/Route — let
    them fix it via V2 Config later."""
    cfg = finalize_wizard_channel(
        _base_cfg(), stack_name="X", chat_id=0,
        account_phone="+1",
    )
    assert len(cfg.channels) == 0
    assert len(cfg.routes) == 0
    # BotBinding still added — it's destination-scoped, doesn't need a Channel.
    assert len(cfg.bot_bindings) == 1


def test_finalize_idempotent_on_rerun():
    cfg = finalize_wizard_channel(
        _base_cfg(), stack_name="X", chat_id=-1, account_phone="+1",
    )
    cfg2 = finalize_wizard_channel(
        cfg, stack_name="X", chat_id=-1, account_phone="+1",
    )
    assert len(cfg2.channels) == 1
    assert len(cfg2.routes) == 1
    assert len(cfg2.bot_bindings) == 1
