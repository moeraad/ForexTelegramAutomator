"""Qt smoke tests for the Routes Matrix view (Step 16).

Pure-Python transforms are tested in tests/test_step16_routes_matrix.py
without Qt. These tests cover the view's construction, grid sizing,
and round-trip on cell-check / sizing edits — pieces that need a
real QWidget + the v2 config file on disk to verify.
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
    """2 channels × 2 destinations, one route filled (ch_a → dest_x)."""
    return ConfigV2(
        accounts=(Account(
            id="acc_a", name="Primary", phone="+961",
            session_path="x.session", service_name="CT-Listener-acc_a",
        ),),
        profiles=(Profile(
            id="prof_p", name="P", path="/p.json",
            language="en", symbol="XAUUSD",
        ),),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_a",
                    chat_id=-1001, profile_id="prof_p"),
            Channel(id="ch_b", name="B", account_id="acc_a",
                    chat_id=-1002, profile_id="prof_p"),
        ),
        destinations=(
            Destination(id="dest_x", name="X", db_path="/x.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path="/y.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(Bot(id="bot_main", name="B", token_setting_key="t",
                  service_name="CT-Bot-Main"),),
        routes=(Route(id="r_ax", channel_id="ch_a",
                      destination_id="dest_x", sizing_multiplier=1.0),),
        bot_bindings=(BotBinding(
            id="bind_main", bot_id="bot_main", scope="destination",
            destination_id="dest_x",
        ),),
    )


def test_routes_matrix_constructs_with_no_cfg(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata-empty"))
    from src.gui.views.routes_matrix_view import RoutesMatrixView
    view = RoutesMatrixView(tmp_stack)
    qtbot.addWidget(view)
    assert view._table.rowCount() == 0
    assert view._table.columnCount() == 0


def test_routes_matrix_grid_dimensions_match_cfg(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.routes_matrix_view import RoutesMatrixView
    view = RoutesMatrixView(tmp_stack)
    qtbot.addWidget(view)
    # 2 channels × 2 destinations
    assert view._table.rowCount() == 2
    assert view._table.columnCount() == 2


def test_routes_matrix_check_creates_route_on_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Programmatically call the cell-handler (faking a user click) and
    confirm the resulting Route round-trips to stacks_config.json."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.routes_matrix_view import RoutesMatrixView
    view = RoutesMatrixView(tmp_stack)
    qtbot.addWidget(view)
    # ch_b → dest_y has no route yet. Simulate the operator checking it.
    view._on_cell_checked("ch_b", "dest_y", True)
    reloaded = config_v2.load_v2(config_v2.config_path())
    pairs = {(r.channel_id, r.destination_id) for r in reloaded.routes}
    assert ("ch_b", "dest_y") in pairs


def test_routes_matrix_uncheck_removes_route_on_disk(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.routes_matrix_view import RoutesMatrixView
    view = RoutesMatrixView(tmp_stack)
    qtbot.addWidget(view)
    # r_ax is the existing route — simulate uncheck.
    view._on_cell_checked("ch_a", "dest_x", False)
    reloaded = config_v2.load_v2(config_v2.config_path())
    assert all(r.id != "r_ax" for r in reloaded.routes)


def test_routes_matrix_sizing_change_persists(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.routes_matrix_view import RoutesMatrixView
    view = RoutesMatrixView(tmp_stack)
    qtbot.addWidget(view)
    # Simulate spinbox change to 0.5×.
    view._on_cell_sizing_changed("ch_a", "dest_x", 0.5)
    reloaded = config_v2.load_v2(config_v2.config_path())
    route = next(r for r in reloaded.routes if r.id == "r_ax")
    assert route.sizing_multiplier == 0.5


def test_routes_matrix_check_then_uncheck_keeps_deterministic_id(
    qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Toggle a cell off then on — the resulting route id must equal
    what the deterministic helper would produce, so stacks_config.json
    doesn't churn ids."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    config_v2.save_v2(_baseline_cfg())
    from src.gui.views.routes_matrix_view import RoutesMatrixView
    view = RoutesMatrixView(tmp_stack)
    qtbot.addWidget(view)
    # r_ax → uncheck → recheck
    view._on_cell_checked("ch_a", "dest_x", False)
    view._on_cell_checked("ch_a", "dest_x", True)
    reloaded = config_v2.load_v2(config_v2.config_path())
    new_route = next(
        (r for r in reloaded.routes
         if r.channel_id == "ch_a" and r.destination_id == "dest_x"),
        None,
    )
    assert new_route is not None
    # The deterministic id pattern from with_route_added.
    assert new_route.id == "route_ch_a__dest_x"
