"""GUI smoke tests: every view opens, theme toggles, prompts render,
backup roundtrips. No assertion on visual correctness — just
"constructs without raising".
"""
from __future__ import annotations

from pathlib import Path

import pytest


# --- View construction ---------------------------------------------------


def test_live_view_constructs(qtbot, tmp_stack):
    from src.gui.views.live_view import LiveView
    view = LiveView(tmp_stack)
    qtbot.addWidget(view)
    assert view.isVisible() is False  # not shown explicitly, just constructed


def test_journal_view_constructs(qtbot, tmp_stack):
    from src.gui.views.journal_view import JournalView
    view = JournalView(tmp_stack)
    qtbot.addWidget(view)


def test_rejected_view_constructs(qtbot, tmp_stack):
    from src.gui.views.rejected_view import RejectedView
    view = RejectedView(tmp_stack)
    qtbot.addWidget(view)


def test_cost_view_constructs(qtbot, tmp_stack):
    from src.gui.views.cost_view import CostView
    view = CostView(tmp_stack)
    qtbot.addWidget(view)


def test_replay_view_constructs(qtbot, tmp_stack):
    from src.gui.views.replay_view import ReplayView
    view = ReplayView(tmp_stack)
    qtbot.addWidget(view)


def test_profile_view_constructs(qtbot, tmp_stack):
    from src.gui.views.profile_view import ProfileView
    view = ProfileView(tmp_stack)
    qtbot.addWidget(view)


def test_prompts_view_constructs(qtbot, tmp_stack):
    from src.gui.views.prompts_view import PromptsView
    view = PromptsView(tmp_stack)
    qtbot.addWidget(view)


def test_settings_view_constructs(qtbot, tmp_stack):
    from src.gui.views.settings_view import SettingsView
    view = SettingsView(tmp_stack)
    qtbot.addWidget(view)


# --- Theme toggle --------------------------------------------------------


def test_theme_toggle_swaps_palette(qapp, tmp_stack):
    from src.gui.theme import apply_theme, current_palette
    apply_theme(qapp, "dark")
    assert current_palette().name == "dark"
    apply_theme(qapp, "light")
    assert current_palette().name == "light"
    apply_theme(qapp, "dark")
    assert current_palette().name == "dark"


def test_theme_persists_to_state(qapp, tmp_stack, monkeypatch):
    from src.gui.theme import apply_theme, load_persisted, persist
    persist("light")
    assert load_persisted() == "light"
    persist("dark")
    assert load_persisted() == "dark"


# --- Prompts inspector --------------------------------------------------


def test_prompts_render_demo_mode(tmp_stack):
    from src.prompt_inspector import PROMPT_IDS, render
    for pid in PROMPT_IDS:
        rp = render(pid, tmp_stack.db_path, mode="demo")
        assert rp.title, f"{pid}: empty title"
        assert rp.user_content, f"{pid}: empty user_content"


def test_prompts_render_live_mode_no_data(tmp_stack):
    """Live mode with an empty DB should fall back gracefully, never raise."""
    from src.prompt_inspector import PROMPT_IDS, render
    for pid in PROMPT_IDS:
        rp = render(pid, tmp_stack.db_path, mode="live")
        # Demo fallback ensures user_content is never blank.
        assert rp.user_content, f"{pid}: empty user_content in live mode"


# --- Backup round-trip --------------------------------------------------


def test_backup_creates_zip(tmp_stack, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_stack.db_path.parent.parent.parent))
    from src.gui.services.backup_io import make_backup
    result = make_backup(target_dir=tmp_path / "backups")
    assert result.zip_path.exists()
    assert result.stack_count >= 1
    assert result.total_bytes > 0


def test_backup_lists_recent(tmp_stack, tmp_path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_stack.db_path.parent.parent.parent))
    from src.gui.services.backup_io import list_backups, make_backup
    backups_dir = tmp_path / "backups"
    make_backup(target_dir=backups_dir)
    listed = list_backups(target_dir=backups_dir)
    assert len(listed) == 1


# --- Cost guard --------------------------------------------------------


def test_cost_guard_no_op_when_budget_zero(tmp_stack):
    import sqlite3
    from src.cost_guard import check_and_enforce
    conn = sqlite3.connect(str(tmp_stack.db_path))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('cost_daily_budget_usd', '0')")
    conn.commit()
    halted, _ = check_and_enforce(conn, Path(tmp_stack.project_path) / "nonexistent.jsonl")
    assert halted is False
    conn.close()


def test_cost_guard_trips_when_over_cap(tmp_stack, tmp_path):
    import json
    import sqlite3
    from datetime import datetime, timezone
    from src.cost_guard import check_and_enforce

    conn = sqlite3.connect(str(tmp_stack.db_path))
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('cost_daily_budget_usd', '1.00')")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('cost_cap_multiplier', '1.2')")
    conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES('kill_switch', 'off')")
    conn.commit()

    # Stage today's spend at $1.50 — over $1.00 × 1.2 = $1.20 cap.
    log = tmp_path / "ai_calls.jsonl"
    ts = datetime.now(timezone.utc).isoformat()
    log.write_text(json.dumps({"ts": ts, "cost": 1.50}) + "\n", encoding="utf-8")

    halted, reason = check_and_enforce(conn, log)
    assert halted is True
    assert "$1.50" in reason
    row = conn.execute("SELECT value FROM settings WHERE key='kill_switch'").fetchone()
    assert row[0] == "on"
    conn.close()
