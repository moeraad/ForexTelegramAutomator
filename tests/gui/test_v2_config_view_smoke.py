"""Qt smoke tests for v2 config view (Step 9 of multi-channel plan).

Lives under tests/gui/ because it uses the ``tmp_stack`` fixture from
tests/gui/conftest.py. Pure-Python apply_add_channel logic is tested
in tests/test_v2_config_view.py without Qt.
"""
from __future__ import annotations

from pathlib import Path

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


def _baseline_cfg() -> ConfigV2:
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
        routes=(Route(
            id="r_fe", channel_id="ch_fe", destination_id="dest_main",
        ),),
        bot_bindings=(BotBinding(
            id="bind_fe", bot_id="bot_main", scope="destination",
            destination_id="dest_main",
        ),),
    )


def test_v2_config_view_constructs_with_no_cfg(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """The view should render gracefully with no v2 config on disk."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-no-cfg"))
    from src.gui.views.v2_config_view import V2ConfigView
    view = V2ConfigView(tmp_stack)
    qtbot.addWidget(view)
    assert view._tab_accounts.row_count() == 0
    assert view._tab_channels.row_count() == 0


def test_v2_config_view_populates_from_existing_cfg(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """View tables reflect the v2 entities loaded from disk."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.v2_config_view import V2ConfigView
    view = V2ConfigView(tmp_stack)
    qtbot.addWidget(view)
    assert view._tab_accounts.row_count() == 1
    assert view._tab_profiles.row_count() == 1
    assert view._tab_channels.row_count() == 1
    assert view._tab_destinations.row_count() == 1
    assert view._tab_bots.row_count() == 1
    assert view._tab_routes.row_count() == 1
    assert view._tab_bindings.row_count() == 1


def test_toggle_channel_enabled_round_trips_to_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Calling toggle on a channel id should flip enabled and persist."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.v2_config_view import V2ConfigView
    view = V2ConfigView(tmp_stack)
    qtbot.addWidget(view)
    # Channel starts enabled.
    assert view._cfg.channels[0].enabled is True
    view._toggle_channel_enabled("ch_fe")
    reloaded = config_v2.load_v2(config_v2.config_path())
    assert reloaded.channels[0].enabled is False


def test_toggle_channel_halt_round_trips_to_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Step 15: channel halt toggle flips Channel.halted and persists."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.v2_config_view import V2ConfigView
    view = V2ConfigView(tmp_stack)
    qtbot.addWidget(view)
    assert view._cfg.channels[0].halted is False
    view._toggle_channel_halt("ch_fe")
    reloaded = config_v2.load_v2(config_v2.config_path())
    assert reloaded.channels[0].halted is True
    # Second toggle flips it back.
    view._toggle_channel_halt("ch_fe")
    reloaded = config_v2.load_v2(config_v2.config_path())
    assert reloaded.channels[0].halted is False


def test_toggle_route_halt_round_trips_to_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Step 15: route halt toggle flips Route.halted and persists."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.v2_config_view import V2ConfigView
    view = V2ConfigView(tmp_stack)
    qtbot.addWidget(view)
    assert view._cfg.routes[0].halted is False
    view._toggle_route_halt("r_fe")
    reloaded = config_v2.load_v2(config_v2.config_path())
    assert reloaded.routes[0].halted is True
