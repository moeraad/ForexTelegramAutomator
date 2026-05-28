"""Qt smoke tests for the two-mode Add Profile dialog.

Mode A picks an existing ``channels/*.json``; Mode B creates a blank
JSON from a template and links it.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.config_v2 import ConfigV2


@pytest.fixture
def channels_dir(monkeypatch, tmp_path: Path) -> Path:
    """Point BASE_DIR at a tmp tree with a channels/ subdir."""
    base = tmp_path / "base"
    (base / "channels").mkdir(parents=True)
    monkeypatch.setattr(
        "src.gui.services.stack_registry.BASE_DIR", base,
    )
    return base / "channels"


def _seed_profile_json(dir_: Path, stem: str, *, symbol: str = "XAUUSD",
                       language: str = "en", name: str | None = None) -> Path:
    p = dir_ / f"{stem}.json"
    p.write_text(json.dumps({
        "name": name or stem, "symbol": symbol, "language": language,
    }), encoding="utf-8")
    return p


def test_discover_profile_jsons_lists_real_files_only(channels_dir: Path):
    _seed_profile_json(channels_dir, "Forex Engineer")
    _seed_profile_json(channels_dir, "SMC Daily")
    _seed_profile_json(channels_dir, "scratch_draft")  # excluded
    (channels_dir / "Forex Engineer.json.bak").write_text(
        "{}", encoding="utf-8",
    )  # excluded — not *.json
    from src.gui.views.v2_config_view import _discover_profile_jsons
    found = [p.stem for p in _discover_profile_jsons()]
    assert set(found) == {"Forex Engineer", "SMC Daily"}


def test_dialog_defaults_to_pick_when_profiles_exist(qapp, qtbot, channels_dir: Path):
    _seed_profile_json(channels_dir, "Forex Engineer", symbol="XAUUSD",
                       language="ar")
    from src.gui.views.v2_config_view import _AddProfileDialog
    dlg = _AddProfileDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    assert dlg._mode_pick.isChecked()
    # Auto-fill from the selected JSON.
    assert dlg._name.text() == "Forex Engineer"
    assert dlg._symbol.text() == "XAUUSD"
    assert dlg._language.text() == "ar"
    # Pick combo visible — new mode has no path-preview row.
    assert dlg._existing.isVisible()
    dlg.close()


def test_dialog_defaults_to_new_when_no_existing_profiles(
    qapp, qtbot, channels_dir: Path,
):
    from src.gui.views.v2_config_view import _AddProfileDialog
    dlg = _AddProfileDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    assert dlg._mode_new.isChecked()
    assert not dlg._mode_pick.isEnabled()
    assert not dlg._existing.isVisible()
    dlg.close()


def test_dialog_pick_mode_apply_uses_selected_path(
    qapp, qtbot, channels_dir: Path,
):
    path = _seed_profile_json(channels_dir, "SMC", symbol="EURUSD",
                              language="en")
    from src.gui.views.v2_config_view import _AddProfileDialog
    dlg = _AddProfileDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    # Pick mode + the only entry is already selected.
    new_cfg = dlg.apply(ConfigV2())
    p = new_cfg.profiles[0]
    assert p.name == "SMC"
    assert p.path == str(path)
    assert p.symbol == "EURUSD"
    assert p.language == "en"
    dlg.close()


def test_dialog_new_mode_creates_blank_json_and_links_it(
    qapp, qtbot, channels_dir: Path,
):
    from src.gui.views.v2_config_view import _AddProfileDialog
    dlg = _AddProfileDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    # No existing profiles → defaults to new mode.
    dlg._name.setText("Brand New Channel")
    dlg._language.setText("en")
    dlg._symbol.setText("XAUUSD")

    new_cfg = dlg.apply(ConfigV2())
    p = new_cfg.profiles[0]
    assert p.name == "Brand New Channel"
    target = channels_dir / "brand_new_channel.json"
    assert Path(p.path) == target
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    # Template fields present + populated with operator inputs.
    assert payload["name"] == "Brand New Channel"
    assert payload["symbol"] == "XAUUSD"
    assert payload["language"] == "en"
    assert "vocabulary_table" in payload  # template skeleton survived
    dlg.close()


def test_dialog_new_mode_reuses_existing_file_if_present(
    qapp, qtbot, channels_dir: Path,
):
    """If the slugified name collides with an existing file, apply()
    uses the existing file rather than overwriting (we already prompted
    the operator on the Ok path)."""
    pre_existing = channels_dir / "already_there.json"
    pre_existing.write_text(
        json.dumps({"name": "Pre", "symbol": "BTC", "language": "fr"}),
        encoding="utf-8",
    )
    from src.gui.views.v2_config_view import _AddProfileDialog
    dlg = _AddProfileDialog()
    qtbot.addWidget(dlg)
    dlg.show()
    qapp.processEvents()
    dlg._mode_new.setChecked(True)
    dlg._name.setText("Already There")
    new_cfg = dlg.apply(ConfigV2())
    # Path is the existing file — not blown away.
    assert Path(new_cfg.profiles[0].path) == pre_existing
    payload = json.loads(pre_existing.read_text(encoding="utf-8"))
    assert payload["symbol"] == "BTC"  # original content preserved
    dlg.close()


def test_write_blank_profile_json_uses_template(tmp_path: Path):
    from src.gui.views.v2_config_view import _write_blank_profile_json
    out = tmp_path / "x.json"
    _write_blank_profile_json(out, name="X", symbol="XAUUSD", language="ar")
    payload = json.loads(out.read_text(encoding="utf-8"))
    # Every template key present.
    for key in ("name", "symbol", "language", "vocabulary_table",
                "compound_messages", "worked_examples"):
        assert key in payload
    assert payload["name"] == "X"
    assert payload["language"] == "ar"
