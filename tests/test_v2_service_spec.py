"""Phase-3 v2 cleanup: tests for the entity-driven service-spec builder
and the new ``bootstrap_v2_install`` helper.
"""
from __future__ import annotations

import json
from pathlib import Path

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
from src.gui.services.v2_service_spec import (
    dedup_spec,
    derive_v2_service_spec,
)


def _full_cfg() -> ConfigV2:
    """One destination, one channel, one bot binding, one account."""
    return ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="+1",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1, profile_id="p"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(Bot(id="bot_main", name="Main", token_setting_key="t",
                  service_name="CT-Main-Bot"),),
        routes=(Route(id="r_a", channel_id="ch_a",
                      destination_id="dest_x"),),
        bot_bindings=(BotBinding(id="bind_main", bot_id="bot_main",
                                 scope="destination",
                                 destination_id="dest_x"),),
    )


def test_spec_has_one_api_per_destination():
    spec = derive_v2_service_spec(_full_cfg())
    apis = [e for e in spec if e["module"] == "src.api"]
    assert len(apis) == 1
    assert apis[0]["service"] == "CT-X-Api"
    assert apis[0]["db_path"] == "/x.db"


def test_spec_has_one_bot_per_bound_bot():
    spec = derive_v2_service_spec(_full_cfg())
    bots = [e for e in spec if e["module"] == "src.bot"]
    assert len(bots) == 1
    assert bots[0]["service"] == "CT-Main-Bot"


def test_spec_has_one_listener_per_account_with_channels():
    spec = derive_v2_service_spec(_full_cfg())
    listeners = [e for e in spec if e["module"] == "src.shared_listener"]
    assert len(listeners) == 1
    assert listeners[0]["service"] == "CT-Listener-acc_a"
    assert listeners[0]["account_id"] == "acc_a"


def test_spec_orders_apis_then_bots_then_listeners():
    """APIs install first so the bot's binding lookups work at startup."""
    spec = derive_v2_service_spec(_full_cfg())
    modules = [e["module"] for e in spec]
    assert modules == ["src.api", "src.bot", "src.shared_listener"]


def test_spec_dedups_shared_bot_across_destinations():
    """One bot bound to two destinations should appear ONCE."""
    cfg = ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_a",
                    chat_id=-1, profile_id="p"),
            Channel(id="ch_b", name="B", account_id="acc_a",
                    chat_id=-2, profile_id="p"),
        ),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-X-Api"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Y-Api"),
        ),
        bots=(Bot(id="bot_global", name="Global",
                  token_setting_key="t", service_name="CT-Global-Bot"),),
        routes=(
            Route(id="r_a", channel_id="ch_a", destination_id="dest_x"),
            Route(id="r_b", channel_id="ch_b", destination_id="dest_y"),
        ),
        bot_bindings=(BotBinding(id="bind", bot_id="bot_global",
                                 scope="global"),),
    )
    spec = derive_v2_service_spec(cfg)
    bots = [e for e in spec if e["module"] == "src.bot"]
    assert len(bots) == 1  # ← deduped despite being bound to both dests
    assert bots[0]["service"] == "CT-Global-Bot"


def test_spec_dedups_shared_account_across_destinations():
    """One account's listener feeds N channels routing to N dests —
    one listener service, not N."""
    cfg = ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_a",
                    chat_id=-1, profile_id="p"),
            Channel(id="ch_b", name="B", account_id="acc_a",
                    chat_id=-2, profile_id="p"),
        ),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-X-Api"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Y-Api"),
        ),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B-Bot"),),
        routes=(
            Route(id="r_a", channel_id="ch_a", destination_id="dest_x"),
            Route(id="r_b", channel_id="ch_b", destination_id="dest_y"),
        ),
        bot_bindings=(BotBinding(id="bind", bot_id="b",
                                 scope="global"),),
    )
    spec = derive_v2_service_spec(cfg)
    listeners = [e for e in spec if e["module"] == "src.shared_listener"]
    assert len(listeners) == 1
    assert listeners[0]["account_id"] == "acc_a"


def test_spec_skips_destination_with_no_enabled_routes():
    cfg = ConfigV2(
        destinations=(Destination(
            id="dest_orphan", name="Orphan", db_path="/o.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Orphan-Api",
        ),),
    )
    assert derive_v2_service_spec(cfg) == []


def test_spec_skips_orphan_bot_with_no_binding():
    """A Bot row with no binding (scratch entry) doesn't get a service."""
    cfg = _full_cfg()
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels, destinations=cfg.destinations,
        bots=cfg.bots + (Bot(
            id="bot_orphan", name="Orphan", token_setting_key="t",
            service_name="CT-Orphan-Bot",
        ),),
        routes=cfg.routes, bot_bindings=cfg.bot_bindings,
    )
    spec = derive_v2_service_spec(cfg)
    bot_services = {e["service"] for e in spec if e["module"] == "src.bot"}
    assert "CT-Orphan-Bot" not in bot_services
    assert "CT-Main-Bot" in bot_services


def test_spec_skips_orphan_account_with_no_channels():
    cfg = _full_cfg()
    cfg = ConfigV2(
        accounts=cfg.accounts + (Account(
            id="acc_orphan", name="Orphan", phone="",
            session_path="", service_name="CT-Listener-acc_orphan",
        ),),
        profiles=cfg.profiles, channels=cfg.channels,
        destinations=cfg.destinations, bots=cfg.bots,
        routes=cfg.routes, bot_bindings=cfg.bot_bindings,
    )
    spec = derive_v2_service_spec(cfg)
    listener_svcs = {e["service"] for e in spec if e["module"] == "src.shared_listener"}
    assert "CT-Listener-acc_orphan" not in listener_svcs


def test_spec_skips_disabled_channel():
    cfg = _full_cfg()
    from dataclasses import replace as _replace
    disabled_ch = _replace(cfg.channels[0], enabled=False)
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=(disabled_ch,),
        destinations=cfg.destinations, bots=cfg.bots,
        routes=cfg.routes, bot_bindings=cfg.bot_bindings,
    )
    spec = derive_v2_service_spec(cfg)
    listener_svcs = {e["service"] for e in spec if e["module"] == "src.shared_listener"}
    # No enabled channel for acc_a → no listener.
    assert "CT-Listener-acc_a" not in listener_svcs


def test_dedup_spec_first_wins():
    entries = [
        {"service": "S1", "module": "m1", "db_path": "/a", "account_id": ""},
        {"service": "S1", "module": "m2", "db_path": "/b", "account_id": ""},
        {"service": "S2", "module": "m3", "db_path": "/c", "account_id": ""},
    ]
    out = dedup_spec(entries)
    assert len(out) == 2
    assert out[0]["module"] == "m1"  # first wins
    assert out[1]["service"] == "S2"


def test_dedup_spec_skips_blank_service_names():
    entries = [
        {"service": "", "module": "m1", "db_path": "/a", "account_id": ""},
        {"service": "S1", "module": "m2", "db_path": "/b", "account_id": ""},
    ]
    assert len(dedup_spec(entries)) == 1


# ---- helper integration: bootstrap_v2_install argv handling --------------


def test_install_helper_returns_2_on_missing_argv():
    from src.gui.helpers.bootstrap_v2_install import main
    assert main([]) == 2


def test_install_helper_returns_3_on_missing_spec_file(tmp_path: Path):
    from src.gui.helpers.bootstrap_v2_install import main
    assert main([str(tmp_path / "no-such-file.json")]) == 3


def test_install_helper_returns_3_on_invalid_json(tmp_path: Path):
    bad = tmp_path / "bad.json"
    bad.write_text("not json", encoding="utf-8")
    from src.gui.helpers.bootstrap_v2_install import main
    assert main([str(bad)]) == 3


def test_install_helper_returns_3_on_non_list_spec(tmp_path: Path):
    bad = tmp_path / "obj.json"
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    from src.gui.helpers.bootstrap_v2_install import main
    assert main([str(bad)]) == 3


def test_install_helper_returns_0_on_empty_spec(tmp_path: Path):
    empty = tmp_path / "empty.json"
    empty.write_text("[]", encoding="utf-8")
    from src.gui.helpers.bootstrap_v2_install import main
    assert main([str(empty)]) == 0
