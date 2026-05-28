"""TriggersView + UnmatchedView follow the ProfileView picker.

Pre-fix: both sub-tabs hard-coded ``self._stack.name`` for the profile
JSON load/save. For aggregate routing (one destination, multiple
profiles), edits silently landed in the stack's default profile not
the operator's actively picked one.

Post-fix: both accept a ``get_profile_name`` callback (parent
ProfileView's ``lambda: self._active_profile_name``).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest


def _seed_profiles(monkeypatch, tmp_path: Path) -> Path:
    """Two channel profiles under <base>/channels/."""
    base = tmp_path / "base"
    (base / "channels").mkdir(parents=True)
    monkeypatch.setattr("src.gui.services.stack_registry.BASE_DIR", base)
    monkeypatch.setattr("src.gui.services.profile_io.BASE_DIR", base)
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    for name in ("alpha", "beta"):
        (base / "channels" / f"{name}.json").write_text(
            json.dumps({
                "name": name, "symbol": "XAUUSD",
                "header": "H", "vocabulary_table": "v",
                "worked_examples": "e",
                # normalize_triggers requires phrase + action_type/action_types.
                "triggers": [{
                    "phrase": f"phrase-{name}",
                    "samples": [f"sample-{name}"],
                    "action_type": "OPEN",
                }],
            }),
            encoding="utf-8",
        )
    return base


# ---- TriggersView ------------------------------------------------------


def test_triggers_view_loads_from_get_profile_name(
    qapp, qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """Picker callback returning 'beta' makes TriggersView load beta.json's
    triggers, NOT the stack's default."""
    _seed_profiles(monkeypatch, tmp_path)
    from src.gui.views.triggers_view import TriggersView
    view = TriggersView(tmp_stack, get_profile_name=lambda: "beta")
    qtbot.addWidget(view)
    qapp.processEvents()
    # The view loaded beta's triggers — assert by phrase content.
    phrases = [t.get("phrase") for t in view._triggers]
    assert "phrase-beta" in phrases
    assert "phrase-alpha" not in phrases
    view.close()


def test_triggers_view_falls_back_to_stack_name_when_no_callback(
    qapp, qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """v1 back-compat: without get_profile_name, uses stack.name."""
    _seed_profiles(monkeypatch, tmp_path)
    # tmp_stack.name is "test_stack" — no such file → empty triggers list.
    from src.gui.views.triggers_view import TriggersView
    view = TriggersView(tmp_stack)  # no callback
    qtbot.addWidget(view)
    qapp.processEvents()
    assert view._triggers == []  # nothing loaded — fallback path
    view.close()


def test_triggers_view_reload_picks_up_picker_change(
    qapp, qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """When the picker changes, ProfileView calls TriggersView.reload();
    the next load_profile call uses the new picker value."""
    _seed_profiles(monkeypatch, tmp_path)
    from src.gui.views.triggers_view import TriggersView
    current = {"picked": "alpha"}
    view = TriggersView(tmp_stack, get_profile_name=lambda: current["picked"])
    qtbot.addWidget(view)
    qapp.processEvents()
    assert any(t.get("phrase") == "phrase-alpha" for t in view._triggers)
    # Operator switches picker.
    current["picked"] = "beta"
    view.reload()
    qapp.processEvents()
    assert any(t.get("phrase") == "phrase-beta" for t in view._triggers)
    assert not any(t.get("phrase") == "phrase-alpha" for t in view._triggers)
    view.close()


# ---- UnmatchedView ------------------------------------------------------


def test_unmatched_view_promote_writes_to_active_profile(
    qapp, qtbot, tmp_stack, monkeypatch, tmp_path,
):
    """`_append_to_profile` writes the promoted trigger to the picker's
    active profile JSON, not the stack-default's."""
    base = _seed_profiles(monkeypatch, tmp_path)
    from src.gui.views.unmatched_view import UnmatchedView
    view = UnmatchedView(tmp_stack, get_profile_name=lambda: "beta")
    qtbot.addWidget(view)
    qapp.processEvents()

    # Stop the auto-refresh timer to avoid background DB hits in tests.
    view._timer.stop()
    new_trigger = {"phrase": "promoted_phrase", "samples": ["s"],
                   "action_type": "OPEN"}
    view._append_to_profile(new_trigger)
    qapp.processEvents()

    beta_file = base / "channels" / "beta.json"
    beta_data = json.loads(beta_file.read_text(encoding="utf-8"))
    phrases = [t.get("phrase") for t in beta_data.get("triggers", [])]
    assert "promoted_phrase" in phrases

    # alpha.json should be untouched.
    alpha_file = base / "channels" / "alpha.json"
    alpha_data = json.loads(alpha_file.read_text(encoding="utf-8"))
    alpha_phrases = [t.get("phrase") for t in alpha_data.get("triggers", [])]
    assert "promoted_phrase" not in alpha_phrases
    view.close()


# ---- Journal model Channel column --------------------------------------


def test_journal_traderow_default_source_channel_id_blank():
    """Legacy positions (pre-Step-11) have no channel tag."""
    from src.gui.services.journal_data import TradeRow
    r = TradeRow(
        ticket=1, action_id=None, side="BUY", volume=0.1,
        original_volume=0.1, entry_price=4000.0, exit_price=4010.0,
        realized_pnl=10.0, close_reason="tp", opened_at="", closed_at="",
    )
    assert r.source_channel_id == ""


def test_journal_model_renders_channel_dash_for_legacy_row(qapp):
    """Legacy rows show '—' in the Channel column."""
    from PySide6.QtCore import Qt
    from src.gui.models.journal_model import JournalModel, COL_CHANNEL
    from src.gui.services.journal_data import TradeRow
    model = JournalModel()
    model.set_rows([TradeRow(
        ticket=1, action_id=None, side="BUY", volume=0.1,
        original_volume=0.1, entry_price=4000.0, exit_price=4010.0,
        realized_pnl=10.0, close_reason="tp",
        opened_at="2026-05-24T10:00:00+00:00",
        closed_at="2026-05-24T11:00:00+00:00",
    )])
    idx = model.index(0, COL_CHANNEL)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "—"


def test_journal_model_renders_channel_id_when_v2_missing(qapp, monkeypatch, tmp_path):
    """Tagged row + no v2 config → raw id is shown."""
    from PySide6.QtCore import Qt
    from src.gui.models.actions_model import clear_channel_name_cache
    from src.gui.models.journal_model import JournalModel, COL_CHANNEL
    from src.gui.services.journal_data import TradeRow
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    clear_channel_name_cache()
    model = JournalModel()
    model.set_rows([TradeRow(
        ticket=1, action_id=42, side="BUY", volume=0.1,
        original_volume=0.1, entry_price=4000.0, exit_price=4010.0,
        realized_pnl=10.0, close_reason="tp",
        opened_at="2026-05-24T10:00:00+00:00",
        closed_at="2026-05-24T11:00:00+00:00",
        source_channel_id="ch_a",
    )])
    idx = model.index(0, COL_CHANNEL)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "ch_a"
