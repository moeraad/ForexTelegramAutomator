"""Cascading remove transforms for v2 entities.

Each entity has a remove transform that drops dependents in the
correct order; Profile is the exception (it refuses to remove when
still referenced — operator must reassign first).
"""
from __future__ import annotations

import pytest

from src import config_v2
from src.config_v2 import (
    Account, Bot, BotBinding, Channel, ConfigV2, Destination, Profile, Route,
)


def _full_cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="a1", name="A", phone="", session_path="",
                          service_name="s_a"),),
        profiles=(Profile(id="p1", name="P", path="/p.json"),),
        channels=(Channel(id="c1", name="Ch", account_id="a1",
                          chat_id=-1, profile_id="p1"),),
        destinations=(Destination(id="d1", name="D", db_path="/x.db",
                                  api_host="127.0.0.1", api_port=8765,
                                  service_name="s_d"),),
        bots=(Bot(id="b1", name="B", token_setting_key="tg_bot_token",
                  service_name="s_b"),),
        routes=(Route(id="r1", channel_id="c1", destination_id="d1"),),
        bot_bindings=(BotBinding(id="bd1", bot_id="b1", scope="destination",
                                 destination_id="d1"),),
    )


def test_channel_removed_cascades_routes_and_bindings():
    cfg = _full_cfg()
    # Add a channel-scoped binding so we can verify it gets dropped.
    cfg = config_v2.with_binding_added(
        cfg, bot_id="b1", scope="channel", channel_id="c1",
    )
    new_cfg = config_v2.with_channel_removed(cfg, "c1")
    assert new_cfg.channels == ()
    assert new_cfg.routes == ()
    assert all(b.channel_id != "c1" for b in new_cfg.bot_bindings)


def test_account_removed_cascades_channels():
    cfg = _full_cfg()
    new_cfg = config_v2.with_account_removed(cfg, "a1")
    assert new_cfg.accounts == ()
    assert new_cfg.channels == ()  # cascaded
    assert new_cfg.routes == ()  # cascaded via channel


def test_destination_removed_cascades_routes_and_bindings():
    cfg = _full_cfg()
    new_cfg = config_v2.with_destination_removed(cfg, "d1")
    assert new_cfg.destinations == ()
    assert new_cfg.routes == ()
    assert all(
        b.destination_id != "d1" for b in new_cfg.bot_bindings
    )


def test_bot_removed_cascades_bindings():
    cfg = _full_cfg()
    new_cfg = config_v2.with_bot_removed(cfg, "b1")
    assert new_cfg.bots == ()
    assert new_cfg.bot_bindings == ()


def test_profile_remove_refuses_when_channel_references_it():
    cfg = _full_cfg()
    with pytest.raises(ValueError, match="still used by channel"):
        config_v2.with_profile_removed(cfg, "p1")


def test_profile_remove_works_when_unreferenced():
    cfg = _full_cfg()
    # Drop the channel first so the profile is orphan.
    cfg = config_v2.with_channel_removed(cfg, "c1")
    new_cfg = config_v2.with_profile_removed(cfg, "p1")
    assert new_cfg.profiles == ()


def test_remove_unknown_id_raises():
    cfg = _full_cfg()
    for fn in (
        config_v2.with_account_removed,
        config_v2.with_profile_removed,
        config_v2.with_channel_removed,
        config_v2.with_destination_removed,
        config_v2.with_bot_removed,
    ):
        with pytest.raises(ValueError):
            fn(cfg, "nope")
