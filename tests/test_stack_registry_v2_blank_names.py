"""Regression test: _stacks_from_v2 must derive a non-blank service_name
triple even when v2 entities have empty service_name fields.

The bug: a v2 Account/Destination/Bot with ``service_name=""`` produced
a Stack with ``service_names=("", "", "")``. Clicking "Uninstall services"
on that stack passed three blanks to the elevated helper, which filtered
them out and errored with "no service names; expected at least one."

The fix: each derivation falls back to ``_derive_service_names(dest.name)``
when the v2 entity's service_name is blank.
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
from src.gui.services.stack_registry import _derive_service_names, _stacks_from_v2


def _cfg_with_blanks(*, account_svc="", dest_svc="", bot_svc="") -> ConfigV2:
    return ConfigV2(
        accounts=(Account(
            id="acc_a", name="A", phone="", session_path="",
            service_name=account_svc,
        ),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="C", account_id="acc_a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name=dest_svc,
        ),),
        bots=(Bot(
            id="bot_main", name="B", token_setting_key="t",
            service_name=bot_svc,
        ),),
        routes=(Route(id="r", channel_id="ch_a",
                      destination_id="dest_x"),),
        bot_bindings=(BotBinding(id="bind", bot_id="bot_main",
                                 scope="destination",
                                 destination_id="dest_x"),),
    )


def test_all_blank_service_names_fall_back_to_derived():
    """Phase-1 cleanup: service_names is now a flat tuple of every
    service touching the destination (API + N bots + N accounts), all
    derived non-blank. The primary_* headlines pin one per role."""
    cfg = _cfg_with_blanks()
    stacks = _stacks_from_v2(cfg)
    assert len(stacks) == 1
    s = stacks[0]
    # No blanks anywhere.
    assert all(s.service_names)
    # Headlines derived from each entity's own name.
    assert s.primary_api_service == _derive_service_names("X")[0]
    assert s.primary_bot_service == _derive_service_names("B")[1]
    assert s.primary_listener_service == _derive_service_names("A")[2]


def test_only_account_blank_uses_derived_listener():
    cfg = _cfg_with_blanks(
        account_svc="", dest_svc="CT-MyApi", bot_svc="CT-MyBot",
    )
    s = _stacks_from_v2(cfg)[0]
    assert s.primary_api_service == "CT-MyApi"
    assert s.primary_bot_service == "CT-MyBot"
    # Listener falls back to derive-from-account-name.
    assert s.primary_listener_service == _derive_service_names("A")[2]


def test_only_destination_blank_uses_derived_api():
    cfg = _cfg_with_blanks(
        account_svc="CT-Listener-acc_a", dest_svc="", bot_svc="CT-MyBot",
    )
    s = _stacks_from_v2(cfg)[0]
    # API falls back to derive-from-dest-name.
    assert s.primary_api_service == _derive_service_names("X")[0]
    assert s.primary_bot_service == "CT-MyBot"
    assert s.primary_listener_service == "CT-Listener-acc_a"


def test_only_bot_blank_uses_derived_bot():
    cfg = _cfg_with_blanks(
        account_svc="CT-Listener-acc_a", dest_svc="CT-MyApi", bot_svc="",
    )
    s = _stacks_from_v2(cfg)[0]
    assert s.primary_api_service == "CT-MyApi"
    # Bot falls back to derive-from-bot-name.
    assert s.primary_bot_service == _derive_service_names("B")[1]
    assert s.primary_listener_service == "CT-Listener-acc_a"


def test_all_explicit_passes_through():
    cfg = _cfg_with_blanks(
        account_svc="CT-Listener-acc_a",
        dest_svc="CT-MyApi", bot_svc="CT-MyBot",
    )
    s = _stacks_from_v2(cfg)[0]
    assert s.primary_api_service == "CT-MyApi"
    assert s.primary_bot_service == "CT-MyBot"
    assert s.primary_listener_service == "CT-Listener-acc_a"
    # Full service_names lists everything, dedup'd.
    assert set(s.service_names) == {
        "CT-MyApi", "CT-MyBot", "CT-Listener-acc_a",
    }
