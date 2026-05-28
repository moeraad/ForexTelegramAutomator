"""Qt smoke tests for the Bot Bindings view (Step 17)."""
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
        accounts=(Account(id="acc_a", name="A", phone="",
                          session_path="x.session",
                          service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="prof", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1001, profile_id="prof"),),
        destinations=(Destination(
            id="dest_x", name="X", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-Api-X",
        ),),
        bots=(Bot(id="bot_main", name="Main",
                  token_setting_key="tg_bot_token",
                  service_name="CT-Bot-Main"),),
        routes=(Route(id="r_ax", channel_id="ch_a",
                      destination_id="dest_x"),),
        bot_bindings=(BotBinding(
            id="bind_main", bot_id="bot_main",
            scope="destination", destination_id="dest_x",
        ),),
    )


def test_bot_bindings_view_constructs_with_no_cfg(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-empty"))
    from src.gui.views.bot_bindings_view import BotBindingsView
    view = BotBindingsView(tmp_stack)
    qtbot.addWidget(view)
    # Empty config → tree empty, summary explains why.
    assert view._tree.topLevelItemCount() == 0


def test_bot_bindings_view_renders_bot_and_binding(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.bot_bindings_view import BotBindingsView
    view = BotBindingsView(tmp_stack)
    qtbot.addWidget(view)
    # 1 top-level (the bot) + the binding as child.
    assert view._tree.topLevelItemCount() == 1
    top = view._tree.topLevelItem(0)
    assert "bot_main" in top.text(0)
    assert top.childCount() == 1
    assert "bind_main" in top.child(0).text(0)


def test_bot_bindings_view_add_persists_to_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Bypass the dialog by calling with_binding_added directly through
    the view's mutation surface."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.bot_bindings_view import BotBindingsView
    view = BotBindingsView(tmp_stack)
    qtbot.addWidget(view)
    # Use the transform directly + save (the dialog's apply() does the
    # same thing internally; smoke-testing the dialog event loop is
    # over-coverage for a CRUD form).
    new_cfg = config_v2.with_binding_added(
        view._cfg, bot_id="bot_main", scope="channel", channel_id="ch_a",
    )
    config_v2.save_v2(new_cfg)
    view.refresh()
    reloaded = config_v2.load_v2(config_v2.config_path())
    scopes = {b.scope for b in reloaded.bot_bindings}
    assert "channel" in scopes


def test_bot_bindings_view_remove_persists_to_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.bot_bindings_view import BotBindingsView
    view = BotBindingsView(tmp_stack)
    qtbot.addWidget(view)
    new_cfg = config_v2.with_binding_removed(view._cfg, "bind_main")
    config_v2.save_v2(new_cfg)
    view.refresh()
    reloaded = config_v2.load_v2(config_v2.config_path())
    assert reloaded.bot_bindings == ()


def test_bot_bindings_view_warning_shown_on_overlap(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Two distinct bots covering the same destination → warning surfaces."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg = _baseline_cfg()
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels, destinations=cfg.destinations,
        bots=cfg.bots + (Bot(
            id="bot_alert", name="Alert",
            token_setting_key="tg_alert_token",
            service_name="CT-Bot-Alert",
        ),),
        routes=cfg.routes,
        bot_bindings=cfg.bot_bindings + (BotBinding(
            id="bind_alert", bot_id="bot_alert", scope="global",
        ),),
    )
    config_v2.save_v2(cfg)
    from src.gui.views.bot_bindings_view import BotBindingsView
    view = BotBindingsView(tmp_stack)
    qtbot.addWidget(view)
    warning_html = view._warnings.text()
    assert "overlap" in warning_html.lower()
    assert "dest_x" in warning_html
