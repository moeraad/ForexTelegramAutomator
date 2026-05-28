"""Pure transform tests for the four standalone entity adds:
``with_account_added`` / ``with_profile_added`` / ``with_destination_added``
/ ``with_bot_added``.

The GUI dialogs in v2_config_view delegate field validation to these
transforms — testing them here means the GUI smoke tests only have to
prove "the dialog wires inputs to the transform" rather than
re-asserting all the validation rules.
"""
from __future__ import annotations

import pytest

from src.config_v2 import (
    Account,
    Bot,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
    with_account_added,
    with_bot_added,
    with_destination_added,
    with_profile_added,
)


def _empty_cfg() -> ConfigV2:
    return ConfigV2()


# ---- with_account_added -------------------------------------------------


def test_account_appends_with_explicit_id():
    cfg = with_account_added(
        _empty_cfg(), account_id="acc_a", name="A",
        phone="+961", session_path="x.session",
    )
    assert len(cfg.accounts) == 1
    a = cfg.accounts[0]
    assert a.id == "acc_a"
    assert a.name == "A"
    assert a.phone == "+961"
    assert a.service_name == "CT-Listener-acc_a"  # auto-derived


def test_account_uses_explicit_service_name_when_given():
    cfg = with_account_added(
        _empty_cfg(), account_id="acc_a", name="A",
        service_name="CT-Custom",
    )
    assert cfg.accounts[0].service_name == "CT-Custom"


def test_account_rejects_blank_id():
    with pytest.raises(ValueError, match="account_id"):
        with_account_added(_empty_cfg(), account_id="", name="A")


def test_account_rejects_blank_name():
    with pytest.raises(ValueError, match="Name"):
        with_account_added(_empty_cfg(), account_id="acc_a", name="")


def test_account_rejects_id_collision():
    cfg = with_account_added(_empty_cfg(), account_id="acc_a", name="A")
    with pytest.raises(ValueError, match="Account id collision"):
        with_account_added(cfg, account_id="acc_a", name="A2")


# ---- with_profile_added -------------------------------------------------


def test_profile_appends_with_all_fields():
    cfg = with_profile_added(
        _empty_cfg(), profile_id="prof_p", name="P",
        path="/p.json", language="en", symbol="XAUUSD",
    )
    p = cfg.profiles[0]
    assert p.id == "prof_p"
    assert p.name == "P"
    assert p.path == "/p.json"
    assert p.language == "en"
    assert p.symbol == "XAUUSD"


def test_profile_rejects_blank_path():
    with pytest.raises(ValueError, match="Path"):
        with_profile_added(
            _empty_cfg(), profile_id="prof_p", name="P", path="",
        )


def test_profile_rejects_blank_id():
    with pytest.raises(ValueError, match="profile_id"):
        with_profile_added(
            _empty_cfg(), profile_id="", name="P", path="/p.json",
        )


def test_profile_rejects_blank_name():
    with pytest.raises(ValueError, match="Name"):
        with_profile_added(
            _empty_cfg(), profile_id="prof_p", name="", path="/p.json",
        )


def test_profile_rejects_id_collision():
    cfg = with_profile_added(
        _empty_cfg(), profile_id="prof_p", name="P", path="/p.json",
    )
    with pytest.raises(ValueError, match="Profile id collision"):
        with_profile_added(
            cfg, profile_id="prof_p", name="P2", path="/p2.json",
        )


# ---- with_destination_added ---------------------------------------------


def test_destination_appends_with_defaults():
    cfg = with_destination_added(
        _empty_cfg(), destination_id="dest_x", name="X",
        db_path="/x.db",
    )
    d = cfg.destinations[0]
    assert d.id == "dest_x"
    assert d.api_host == "127.0.0.1"
    assert d.api_port == 8765
    assert d.service_name == "CT-Api-dest_x"


def test_destination_uses_explicit_port_and_host():
    cfg = with_destination_added(
        _empty_cfg(), destination_id="dest_x", name="X",
        db_path="/x.db", api_host="0.0.0.0", api_port=9000,
    )
    d = cfg.destinations[0]
    assert d.api_host == "0.0.0.0"
    assert d.api_port == 9000


def test_destination_rejects_blank_db_path():
    with pytest.raises(ValueError, match="db_path"):
        with_destination_added(
            _empty_cfg(), destination_id="dest_x", name="X", db_path="",
        )


def test_destination_rejects_out_of_range_port():
    with pytest.raises(ValueError, match="api_port"):
        with_destination_added(
            _empty_cfg(), destination_id="dest_x", name="X",
            db_path="/x.db", api_port=0,
        )
    with pytest.raises(ValueError, match="api_port"):
        with_destination_added(
            _empty_cfg(), destination_id="dest_x", name="X",
            db_path="/x.db", api_port=70000,
        )


def test_destination_rejects_id_collision():
    cfg = with_destination_added(
        _empty_cfg(), destination_id="dest_x", name="X", db_path="/x.db",
    )
    with pytest.raises(ValueError, match="Destination id collision"):
        with_destination_added(
            cfg, destination_id="dest_x", name="X2", db_path="/x2.db",
        )


def test_destination_rejects_port_collision_on_same_host():
    """Two destinations on the same host+port would race for the socket."""
    cfg = with_destination_added(
        _empty_cfg(), destination_id="dest_x", name="X",
        db_path="/x.db", api_port=8765,
    )
    with pytest.raises(ValueError, match="already binds"):
        with_destination_added(
            cfg, destination_id="dest_y", name="Y",
            db_path="/y.db", api_port=8765,
        )


def test_destination_allows_same_port_on_different_host():
    """Same port + different host = no conflict (e.g., one binds 0.0.0.0,
    the other 127.0.0.1 is rejected by OS anyway but config-level we don't
    second-guess that decision)."""
    cfg = with_destination_added(
        _empty_cfg(), destination_id="dest_x", name="X",
        db_path="/x.db", api_host="127.0.0.1", api_port=8765,
    )
    cfg2 = with_destination_added(
        cfg, destination_id="dest_y", name="Y",
        db_path="/y.db", api_host="192.168.1.10", api_port=8765,
    )
    assert len(cfg2.destinations) == 2


# ---- with_bot_added -----------------------------------------------------


def test_bot_appends_with_defaults():
    cfg = with_bot_added(
        _empty_cfg(), bot_id="bot_main", name="Main",
        token_setting_key="tg_bot_token",
    )
    b = cfg.bots[0]
    assert b.id == "bot_main"
    assert b.name == "Main"
    assert b.token_setting_key == "tg_bot_token"
    assert b.service_name == "CT-Bot-bot_main"


def test_bot_uses_explicit_service_name():
    cfg = with_bot_added(
        _empty_cfg(), bot_id="bot_main", name="Main",
        token_setting_key="tg_bot_token", service_name="CT-Custom",
    )
    assert cfg.bots[0].service_name == "CT-Custom"


def test_bot_rejects_blank_token_key():
    with pytest.raises(ValueError, match="token_setting_key"):
        with_bot_added(
            _empty_cfg(), bot_id="bot_main", name="Main",
            token_setting_key="",
        )


def test_bot_rejects_blank_id():
    with pytest.raises(ValueError, match="bot_id"):
        with_bot_added(
            _empty_cfg(), bot_id="", name="Main",
            token_setting_key="t",
        )


def test_bot_rejects_blank_name():
    with pytest.raises(ValueError, match="Name"):
        with_bot_added(
            _empty_cfg(), bot_id="bot_main", name="",
            token_setting_key="t",
        )


def test_bot_rejects_id_collision():
    cfg = with_bot_added(
        _empty_cfg(), bot_id="bot_main", name="Main",
        token_setting_key="t1",
    )
    with pytest.raises(ValueError, match="Bot id collision"):
        with_bot_added(
            cfg, bot_id="bot_main", name="Main2", token_setting_key="t2",
        )


# ---- composability ------------------------------------------------------


def test_compose_full_stack_via_transforms():
    """Sanity check: chain the four transforms + with_route_added /
    with_channel-via-apply_add_channel-equivalent to build a 1-stack
    cfg purely through pure transforms."""
    from src.config_v2 import with_route_added
    cfg = _empty_cfg()
    cfg = with_account_added(cfg, account_id="acc_a", name="A")
    cfg = with_profile_added(
        cfg, profile_id="prof_p", name="P", path="/p.json", symbol="XAUUSD",
    )
    cfg = with_destination_added(
        cfg, destination_id="dest_x", name="X",
        db_path="/x.db", api_port=8765,
    )
    cfg = with_bot_added(
        cfg, bot_id="bot_main", name="Main", token_setting_key="t",
    )
    # Channel must be added with an Account + Profile so use the v2_config_view
    # apply_add_channel helper. Routes can use with_route_added directly.
    from src.gui.views.v2_config_view import apply_add_channel
    cfg = apply_add_channel(
        cfg, name="Source", chat_id=-1001,
        account_id="acc_a", profile_id="prof_p",
        destination_id="dest_x", bot_id="bot_main",
    )
    # Verify all 7 entity types are populated.
    assert len(cfg.accounts) == 1
    assert len(cfg.profiles) == 1
    assert len(cfg.channels) == 1
    assert len(cfg.destinations) == 1
    assert len(cfg.bots) == 1
    assert len(cfg.routes) == 1
    assert len(cfg.bot_bindings) == 1
