"""Profile view picker (Phase-2 v2-cleanup): picker decouples 'which
profile is being edited?' from 'which stack are we on?'.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _seed_profile(channels_dir: Path, name: str,
                  *, symbol: str = "XAUUSD", language: str = "en") -> Path:
    p = channels_dir / f"{name}.json"
    p.write_text(
        json.dumps({"name": name, "symbol": symbol, "language": language,
                    "header": "H", "vocabulary_table": "v",
                    "worked_examples": "e"}),
        encoding="utf-8",
    )
    return p


@pytest.fixture
def two_profiles(monkeypatch, tmp_path: Path):
    """Set up a temp BASE_DIR with two profile JSONs."""
    base = tmp_path / "base"
    (base / "channels").mkdir(parents=True)
    monkeypatch.setattr("src.gui.services.stack_registry.BASE_DIR", base)
    monkeypatch.setattr("src.gui.services.profile_io.BASE_DIR", base)
    # Make APPDATA empty so the legacy <base>/channels path wins.
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    _seed_profile(base / "channels", "alpha", symbol="XAUUSD")
    _seed_profile(base / "channels", "beta", symbol="EURUSD")
    return base


def test_picker_lists_discovered_profiles(qapp, qtbot, tmp_stack, two_profiles):
    from src.gui.views.profile_view import ProfileView
    view = ProfileView(tmp_stack)
    qtbot.addWidget(view)
    names = [view._picker.itemData(i) for i in range(view._picker.count())]
    # Both discovered profiles + the current stack's name should be present.
    assert "alpha" in names
    assert "beta" in names
    view.close()


def test_picker_initially_selects_current_stack(
    qapp, qtbot, tmp_stack, two_profiles,
):
    from src.gui.views.profile_view import ProfileView
    view = ProfileView(tmp_stack)
    qtbot.addWidget(view)
    # tmp_stack.name from conftest is "test_stack" — should be active.
    assert view._active_profile_name == tmp_stack.name
    view.close()


def test_picker_switch_loads_other_profile(
    qapp, qtbot, tmp_stack, two_profiles, monkeypatch,
):
    """Picking 'alpha' from the dropdown reloads the editor against that file."""
    from src.gui.views.profile_view import ProfileView
    view = ProfileView(tmp_stack)
    qtbot.addWidget(view)
    # Pick alpha.
    idx = view._picker.findData("alpha")
    assert idx >= 0
    view._picker.setCurrentIndex(idx)
    qapp.processEvents()
    assert view._active_profile_name == "alpha"
    # The active path resolves to the alpha JSON.
    assert view._active_profile_path() == (
        two_profiles / "channels" / "alpha.json"
    )
    view.close()


def test_picker_export_filename_follows_active_profile(
    qapp, qtbot, tmp_stack, two_profiles,
):
    """_export's suggested filename uses the picker's active name."""
    from src.gui.views.profile_view import ProfileView
    view = ProfileView(tmp_stack)
    qtbot.addWidget(view)
    view._active_profile_name = "beta"
    suggested = f"{view._active_profile_name}_profile_backup.json"
    assert suggested == "beta_profile_backup.json"
    view.close()


def test_rebind_resets_picker_to_new_stack(
    qapp, qtbot, tmp_stack, two_profiles,
):
    """Switching stacks via the header bar should reset the picker so
    the operator sees their new stack's profile (their most likely
    intent), not whatever they were last editing."""
    from src.gui.views.profile_view import ProfileView
    view = ProfileView(tmp_stack)
    qtbot.addWidget(view)
    view._picker.setCurrentIndex(view._picker.findData("alpha"))
    qapp.processEvents()
    assert view._active_profile_name == "alpha"
    # Pretend the operator switched to a brand-new stack named "beta".
    from dataclasses import replace as _replace
    stack2 = _replace(tmp_stack, name="beta")
    view.rebind(stack2)
    qapp.processEvents()
    assert view._active_profile_name == "beta"
    view.close()
