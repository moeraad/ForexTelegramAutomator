"""Qt smoke tests for the two-mode Add Destination dialog."""
from __future__ import annotations

from pathlib import Path

import pytest

from src.config_v2 import ConfigV2


@pytest.fixture
def appdata_with_dbs(monkeypatch, tmp_path: Path) -> Path:
    """Point APPDATA at a fresh tree containing two existing copytrades.db files."""
    appdata = tmp_path / "appdata"
    ct = appdata / "CopyTrades"
    for stack in ("forex_engineer", "smc_live"):
        d = ct / stack
        d.mkdir(parents=True)
        (d / "copytrades.db").write_bytes(b"")  # presence is what matters
    monkeypatch.setenv("APPDATA", str(appdata))
    return ct


@pytest.fixture
def appdata_no_dbs(monkeypatch, tmp_path: Path) -> Path:
    appdata = tmp_path / "appdata"
    appdata.mkdir()
    monkeypatch.setenv("APPDATA", str(appdata))
    return appdata


def test_discover_destination_dbs_lists_existing(appdata_with_dbs: Path):
    from src.gui.views.v2_config_view import _discover_destination_dbs
    found = {p.parent.name for p in _discover_destination_dbs()}
    assert found == {"forex_engineer", "smc_live"}


def test_discover_destination_dbs_skips_dirs_without_db(appdata_with_dbs: Path):
    """A folder under CopyTrades that has NO copytrades.db should be ignored."""
    (appdata_with_dbs / "no_db_here").mkdir()
    from src.gui.views.v2_config_view import _discover_destination_dbs
    found = {p.parent.name for p in _discover_destination_dbs()}
    assert "no_db_here" not in found


def test_discover_destination_dbs_returns_empty_when_root_missing(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "totally_empty"))
    from src.gui.views.v2_config_view import _discover_destination_dbs
    assert _discover_destination_dbs() == []


def test_dialog_defaults_to_pick_when_dbs_exist(
    qapp, qtbot, appdata_with_dbs: Path,
):
    from src.gui.views.v2_config_view import _AddDestinationDialog
    dlg = _AddDestinationDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    assert dlg._mode_pick.isChecked()
    assert dlg._db_combo.isVisible()
    # Two discovered DBs populate the combo.
    assert dlg._db_combo.count() == 2
    dlg.close()


def test_dialog_defaults_to_new_when_no_dbs(
    qapp, qtbot, appdata_no_dbs: Path,
):
    from src.gui.views.v2_config_view import _AddDestinationDialog
    dlg = _AddDestinationDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    assert dlg._mode_new.isChecked()
    assert not dlg._mode_pick.isEnabled()
    # Combo hidden in new-mode.
    assert not dlg._db_combo.isVisible()
    dlg.close()


def test_dialog_pick_mode_apply_uses_selected_db(
    qapp, qtbot, appdata_with_dbs: Path,
):
    from src.gui.views.v2_config_view import _AddDestinationDialog
    dlg = _AddDestinationDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    dlg._name.setText("FxPro Live")
    # First entry is the first discovered DB.
    expected_db = dlg._db_combo.itemData(0)
    new_cfg = dlg.apply(ConfigV2())
    d = new_cfg.destinations[0]
    assert d.name == "FxPro Live"
    assert d.db_path == expected_db
    dlg.close()


def test_dialog_new_mode_derives_path_from_display_name(
    qapp, qtbot, appdata_no_dbs: Path,
):
    from src.gui.views.v2_config_view import _AddDestinationDialog
    dlg = _AddDestinationDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    dlg._name.setText("Brand New Dest")
    new_cfg = dlg.apply(ConfigV2())
    d = new_cfg.destinations[0]
    # %APPDATA%/CopyTrades/<display_name>/copytrades.db
    expected = appdata_no_dbs / "CopyTrades" / "Brand New Dest" / "copytrades.db"
    assert Path(d.db_path) == expected
    # File NOT created — the API creates it on first start.
    assert not expected.exists()
    dlg.close()


def test_dialog_new_mode_propagates_other_fields(
    qapp, qtbot, appdata_no_dbs: Path,
):
    from src.gui.views.v2_config_view import _AddDestinationDialog
    dlg = _AddDestinationDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    dlg._name.setText("Custom Dest")
    dlg._api_host.setText("0.0.0.0")
    dlg._api_port.setValue(9000)
    dlg._mt5_label.setText("FxPro-Live")
    new_cfg = dlg.apply(ConfigV2())
    d = new_cfg.destinations[0]
    assert d.api_host == "0.0.0.0"
    assert d.api_port == 9000
    assert d.mt5_label == "FxPro-Live"
    dlg.close()
