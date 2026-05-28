"""Live dashboard: per-channel attribution on the actions table.

Verifies the Channel column resolves v2 channel IDs to display names
and falls back gracefully when the v2 config doesn't know the id.
"""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QModelIndex, Qt

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
from src.gui.models.actions_model import (
    ActionRow,
    ActionsModel,
    COL_CHANNEL,
    HEADERS,
    _channel_display_name,
    clear_channel_name_cache,
)


def _make_row(source_channel_id: str = "ch_a") -> ActionRow:
    return ActionRow(
        id=42, action_type="OPEN", side="BUY", status="executed",
        created_at="2026-05-24T12:00:00+00:00", payload={},
        quality_score=None, quality_verdict=None, multiplier=None,
        entry_price=None, exit_price=None, realized_pnl=None,
        close_reason="", ea_response="",
        source_channel_id=source_channel_id,
    )


def _seed_v2_cfg(monkeypatch, tmp_path: Path) -> None:
    appdata = tmp_path / "appdata"
    monkeypatch.setenv("APPDATA", str(appdata))
    cfg = ConfigV2(
        accounts=(Account(id="acc_a", name="A", phone="",
                          session_path="", service_name="CT-Listener-acc_a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="SMC Daily", account_id="acc_a",
                    chat_id=-1, profile_id="p"),
            Channel(id="ch_b", name="Forex Engineer", account_id="acc_a",
                    chat_id=-2, profile_id="p"),
        ),
        destinations=(Destination(
            id="dest_x", name="X", db_path="/x.db",
            api_host="127.0.0.1", api_port=8765,
            service_name="CT-X-Api",
        ),),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-B-Bot"),),
        routes=(Route(id="r", channel_id="ch_a",
                      destination_id="dest_x"),),
    )
    config_v2.save_v2(cfg)


def test_channel_header_present_in_visible_columns():
    """Channel column must show up between Status and Score."""
    assert "Channel" in HEADERS
    assert HEADERS[COL_CHANNEL] == "Channel"


def test_action_row_default_source_channel_id_blank():
    """Legacy / pre-Step-11 rows have no channel tag."""
    row = ActionRow(
        id=1, action_type="ALERT", side="", status="executed",
        created_at="2026-05-24T12:00:00+00:00", payload={},
        quality_score=None, quality_verdict=None, multiplier=None,
        entry_price=None, exit_price=None, realized_pnl=None,
        close_reason="", ea_response="",
    )
    assert row.source_channel_id == ""


def test_channel_display_name_resolves_via_v2_config(
    qapp, monkeypatch, tmp_path: Path,
):
    clear_channel_name_cache()
    _seed_v2_cfg(monkeypatch, tmp_path)
    assert _channel_display_name("ch_a") == "SMC Daily"
    assert _channel_display_name("ch_b") == "Forex Engineer"


def test_channel_display_name_falls_back_to_id_for_unknown(
    qapp, monkeypatch, tmp_path: Path,
):
    """Channel id not in v2 cfg → show the raw id rather than blank."""
    clear_channel_name_cache()
    _seed_v2_cfg(monkeypatch, tmp_path)
    assert _channel_display_name("ch_deleted") == "ch_deleted"


def test_channel_display_name_falls_back_when_v2_absent(
    qapp, monkeypatch, tmp_path: Path,
):
    clear_channel_name_cache()
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    assert _channel_display_name("ch_x") == "ch_x"


def test_channel_display_name_blank_for_empty_input(qapp):
    clear_channel_name_cache()
    assert _channel_display_name("") == ""


def test_model_renders_dash_for_legacy_row(qapp):
    """Rows without source_channel_id (pre-Step-11) show '—' so the
    operator sees that the column exists but isn't tagged."""
    clear_channel_name_cache()
    model = ActionsModel()
    # Bypass set_rows (which parses sqlite3.Row); inject ActionRow directly.
    model._rows = [_make_row(source_channel_id="")]
    idx = model.index(0, COL_CHANNEL)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "—"


def test_model_renders_channel_name_for_tagged_row(
    qapp, monkeypatch, tmp_path: Path,
):
    clear_channel_name_cache()
    _seed_v2_cfg(monkeypatch, tmp_path)
    model = ActionsModel()
    model._rows = [_make_row(source_channel_id="ch_a")]
    idx = model.index(0, COL_CHANNEL)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "SMC Daily"


def test_model_renders_raw_id_when_channel_unknown(
    qapp, monkeypatch, tmp_path: Path,
):
    clear_channel_name_cache()
    _seed_v2_cfg(monkeypatch, tmp_path)
    model = ActionsModel()
    model._rows = [_make_row(source_channel_id="ch_orphan")]
    idx = model.index(0, COL_CHANNEL)
    assert model.data(idx, Qt.ItemDataRole.DisplayRole) == "ch_orphan"


def test_channel_cache_avoids_repeated_v2_reads(
    qapp, monkeypatch, tmp_path: Path,
):
    """Second call for the same id hits the cache (no v2 reload)."""
    clear_channel_name_cache()
    _seed_v2_cfg(monkeypatch, tmp_path)
    name1 = _channel_display_name("ch_a")
    # Wipe v2 cfg between calls — cache should still serve the original name.
    monkeypatch.setenv("APPDATA", str(tmp_path / "no_appdata"))
    name2 = _channel_display_name("ch_a")
    assert name1 == name2 == "SMC Daily"
