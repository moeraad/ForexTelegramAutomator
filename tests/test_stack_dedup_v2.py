"""Phase-1 v2-cleanup regression: ONE Stack per Destination, never per Route.

Tests the Stack-synth dedup + multi-bot/multi-account service aggregation
introduced when the v1 1:1:1 assumption was unwound.
"""
from __future__ import annotations

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
from src.gui.services.stack_registry import _stacks_from_v2


def _full_cfg() -> ConfigV2:
    """Two channels (different accounts) BOTH routing to one destination,
    plus a destination-scoped bot and a global bot."""
    return ConfigV2(
        accounts=(
            Account(id="acc_a", name="A", phone="+1",
                    session_path="", service_name="CT-Listener-acc_a"),
            Account(id="acc_b", name="B", phone="+2",
                    session_path="", service_name="CT-Listener-acc_b"),
        ),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_a",
                    chat_id=-1, profile_id="p"),
            Channel(id="ch_b", name="B", account_id="acc_b",
                    chat_id=-2, profile_id="p"),
        ),
        destinations=(Destination(
            id="dest_x", name="X", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(
            Bot(id="bot_main", name="Main", token_setting_key="t1",
                service_name="CT-Main-Bot"),
            Bot(id="bot_global", name="Global", token_setting_key="t2",
                service_name="CT-Global-Bot"),
        ),
        routes=(
            Route(id="r_a", channel_id="ch_a", destination_id="dest_x"),
            Route(id="r_b", channel_id="ch_b", destination_id="dest_x"),
        ),
        bot_bindings=(
            BotBinding(id="bind_main", bot_id="bot_main",
                       scope="destination", destination_id="dest_x"),
            BotBinding(id="bind_global", bot_id="bot_global",
                       scope="global"),
        ),
    )


def test_aggregate_routing_produces_one_stack():
    """Pre-Phase-1: this cfg produced TWO Stacks named "X" (one per route).
    Post-Phase-1: produces ONE."""
    stacks = _stacks_from_v2(_full_cfg())
    assert len(stacks) == 1
    assert stacks[0].name == "X"


def test_all_bots_appear_in_service_names():
    s = _stacks_from_v2(_full_cfg())[0]
    assert "CT-Main-Bot" in s.service_names
    assert "CT-Global-Bot" in s.service_names


def test_all_accounts_appear_in_service_names():
    s = _stacks_from_v2(_full_cfg())[0]
    assert "CT-Listener-acc_a" in s.service_names
    assert "CT-Listener-acc_b" in s.service_names


def test_api_appears_first_in_service_names():
    s = _stacks_from_v2(_full_cfg())[0]
    assert s.service_names[0] == "CT-X-Api"


def test_primary_bot_is_destination_scoped_when_present():
    """When both a global and a destination-scoped binding exist, the
    destination one wins as primary (the per-dest icon row points at
    the bot the operator most likely thinks of as 'this dest's bot')."""
    s = _stacks_from_v2(_full_cfg())[0]
    assert s.primary_bot_service == "CT-Main-Bot"


def test_primary_listener_is_first_accounts_service():
    s = _stacks_from_v2(_full_cfg())[0]
    assert s.primary_listener_service in (
        "CT-Listener-acc_a", "CT-Listener-acc_b",
    )


def test_dest_with_no_enabled_routes_skipped():
    cfg = ConfigV2(
        destinations=(Destination(
            id="dest_orphan", name="Orphan", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Orphan-Api",
        ),),
    )
    assert _stacks_from_v2(cfg) == []


def test_dest_with_only_disabled_routes_skipped():
    cfg = ConfigV2(
        accounts=(Account(id="a", name="A", phone="", session_path="",
                          service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="c", name="C", account_id="a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B-Bot"),),
        routes=(Route(id="r", channel_id="c", destination_id="dest_x",
                      enabled=False),),
    )
    assert _stacks_from_v2(cfg) == []


def test_service_names_are_deduplicated():
    """If two routes hit dest_x via the same channel/account, the
    listener service shouldn't appear twice."""
    cfg = _full_cfg()
    # Add a third route with the SAME channel as ch_a → no new service.
    extra = (Route(id="r_a2", channel_id="ch_a", destination_id="dest_x"),)
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels, destinations=cfg.destinations,
        bots=cfg.bots, routes=cfg.routes + extra,
        bot_bindings=cfg.bot_bindings,
    )
    s = _stacks_from_v2(cfg)[0]
    # Listener-acc_a appears EXACTLY once.
    assert s.service_names.count("CT-Listener-acc_a") == 1
