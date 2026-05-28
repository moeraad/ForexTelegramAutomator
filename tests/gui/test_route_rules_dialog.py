"""Qt smoke tests for the per-route Rules editor dialog (Step 20 + 21 GUI).

Validates the dialog round-trips every editable field through
``with_route_rules`` + ``with_route_failover``.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt

from src import config_v2
from src.config_v2 import (
    Account,
    Bot,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)


def _cfg() -> ConfigV2:
    return ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="",
                          session_path="x.session",
                          service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch_a", name="A", account_id="acc_a",
                          chat_id=-1, profile_id="p"),),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B"),),
        routes=(Route(id="r1", channel_id="ch_a",
                      destination_id="dest_x"),),
    )


def test_dialog_round_trips_all_fields(qapp):
    """Set every field in the dialog → apply → confirm new cfg reflects it."""
    from src.gui.views.routes_matrix_view import _EditRouteRulesDialog
    cfg = _cfg()
    dlg = _EditRouteRulesDialog(cfg, "r1")
    qapp.processEvents()

    dlg._max_lots.setValue(0.50)
    dlg._min_balance.setValue(1000.0)
    dlg._max_drawdown.setValue(15.0)
    # Check just OPEN + CLOSE_FULL.
    for i in range(dlg._action_list.count()):
        item = dlg._action_list.item(i)
        item.setCheckState(
            Qt.CheckState.Checked if item.text() in ("OPEN", "CLOSE_FULL")
            else Qt.CheckState.Unchecked
        )
    dlg._time_window.setText("08:00-20:00")
    # Pick fallback dest_y (dest_x is omitted to prevent circular).
    fb_idx = dlg._fallback.findData("dest_y")
    assert fb_idx >= 0, "dest_y should be available as a fallback option"
    dlg._fallback.setCurrentIndex(fb_idx)

    new_cfg = dlg.apply(cfg)
    r = new_cfg.route("r1")
    assert r.max_lots == 0.50
    assert r.min_account_balance == 1000.0
    assert r.skip_if_drawdown_pct == 15.0
    assert set(r.allowed_action_types) == {"OPEN", "CLOSE_FULL"}
    assert r.time_of_day_filter == "08:00-20:00"
    assert r.fallback_destination_id == "dest_y"


def test_dialog_circular_fallback_dest_omitted(qapp):
    """The route targets dest_x, so dest_x cannot appear as its own fallback."""
    from src.gui.views.routes_matrix_view import _EditRouteRulesDialog
    cfg = _cfg()
    dlg = _EditRouteRulesDialog(cfg, "r1")
    qapp.processEvents()
    # Iterate all combo items; dest_x must NOT be among them.
    available = [
        dlg._fallback.itemData(i) for i in range(dlg._fallback.count())
    ]
    assert "dest_x" not in available
    assert "" in available  # "(none)" sentinel always present
    assert "dest_y" in available


def test_dialog_all_action_types_checked_collapses_to_empty(qapp):
    """Checking every action type = same semantics as 'no filter';
    dialog saves an empty tuple to keep the config unambiguous."""
    from src.gui.views.routes_matrix_view import _EditRouteRulesDialog
    cfg = _cfg()
    dlg = _EditRouteRulesDialog(cfg, "r1")
    qapp.processEvents()
    for i in range(dlg._action_list.count()):
        dlg._action_list.item(i).setCheckState(Qt.CheckState.Checked)
    new_cfg = dlg.apply(cfg)
    assert new_cfg.route("r1").allowed_action_types == ()


def test_dialog_preserves_existing_values_on_open(qapp):
    """Open dialog on a route with pre-existing rules → fields reflect them."""
    from src.gui.views.routes_matrix_view import _EditRouteRulesDialog
    cfg = config_v2.with_route_rules(
        _cfg(), "r1",
        max_lots=0.25, min_account_balance=500.0,
        skip_if_drawdown_pct=10.0,
        allowed_action_types=("OPEN",),
        time_of_day_filter="09:00-17:00",
    )
    cfg = config_v2.with_route_failover(cfg, "r1", "dest_y")
    dlg = _EditRouteRulesDialog(cfg, "r1")
    qapp.processEvents()
    assert dlg._max_lots.value() == 0.25
    assert dlg._min_balance.value() == 500.0
    assert dlg._max_drawdown.value() == 10.0
    assert dlg._time_window.text() == "09:00-17:00"
    assert dlg._fallback.currentData() == "dest_y"
    # OPEN is checked, others unchecked.
    open_item = next(
        dlg._action_list.item(i) for i in range(dlg._action_list.count())
        if dlg._action_list.item(i).text() == "OPEN"
    )
    assert open_item.checkState() == Qt.CheckState.Checked


def test_dialog_unknown_route_raises_at_construction(qapp):
    import pytest
    from src.gui.views.routes_matrix_view import _EditRouteRulesDialog
    with pytest.raises(ValueError, match="Unknown route"):
        _EditRouteRulesDialog(_cfg(), "r_nope")


def test_rules_tooltip_summarises_set_fields():
    from src.gui.views.routes_matrix_view import _rules_tooltip
    cfg = config_v2.with_route_rules(
        _cfg(), "r1", max_lots=0.5, time_of_day_filter="08:00-20:00",
    )
    tip = _rules_tooltip(cfg.route("r1"))
    assert "max_lots=0.5" in tip
    assert "window=08:00-20:00" in tip
    # Unset fields aren't mentioned.
    assert "min_balance" not in tip
    assert "fallback" not in tip


def test_rules_tooltip_handles_none_route():
    from src.gui.views.routes_matrix_view import _rules_tooltip
    assert _rules_tooltip(None) == ""


def test_rules_tooltip_says_none_set_for_default_route():
    from src.gui.views.routes_matrix_view import _rules_tooltip
    tip = _rules_tooltip(_cfg().route("r1"))
    assert "none set" in tip.lower()
