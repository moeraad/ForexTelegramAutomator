"""QApplication wiring: stack discovery, picker, MainWindow.

Services no longer auto-start on launch. The user starts them from
Settings after completing the setup wizard. If critical settings are
missing, the wizard auto-opens before MainWindow becomes interactive.
"""
from __future__ import annotations

import logging
import os
import shutil
import sys
from dataclasses import replace
from pathlib import Path

from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMessageBox

from src.gui.services.stack_registry import Stack, discover_stacks
from src.gui.services.stack_state import AppState, load_state, save_state
from src.gui.windows.main_window import MainWindow
from src.gui.windows.picker_window import PickerWindow


_log = logging.getLogger("gui")


def _icon_path() -> Path | None:
    if getattr(sys, "frozen", False):
        candidate = Path(sys._MEIPASS) / "copytrades.ico"  # type: ignore[attr-defined]
        return candidate if candidate.exists() else None
    candidate = Path(__file__).resolve().parent.parent.parent / "copytrades.ico"
    return candidate if candidate.exists() else None


def _pick_stack(stacks: list[Stack], state: AppState, force_picker: bool) -> Stack | None:
    if not force_picker and state.last_stack:
        for s in stacks:
            if s.name == state.last_stack:
                return s
    dlg = PickerWindow(stacks)
    if dlg.exec() and dlg.selected_stack is not None:
        return dlg.selected_stack
    return None


def _ensure_db_ready(stack: Stack) -> None:
    target = stack.db_path
    target.parent.mkdir(parents=True, exist_ok=True)
    candidates = [
        stack.project_path / "copytrades.db",
        target.parent.parent / f"{stack.name}.db",   # legacy: <APPDATA>/CopyTrades/<name>.db
    ]
    if not target.exists():
        for legacy in candidates:
            if legacy.exists() and legacy.resolve() != target.resolve():
                try:
                    shutil.copy2(legacy, target)
                    _log.info("copied legacy DB %s -> %s", legacy, target)
                    break
                except OSError as e:
                    _log.warning("legacy DB copy failed (%s): %s", legacy, e)
    from src import config, db
    config.DB_PATH = str(target)
    config.LOGS_DIR = target.parent / "logs"
    config.LOGS_DIR.mkdir(parents=True, exist_ok=True)
    config.invalidate_cache()
    with db.connect(str(target)) as conn:
        db.init_schema(conn)
    _migrate_legacy_session_file(stack, target)


def _migrate_legacy_session_file(stack: Stack, db_path: Path) -> None:
    """If an old .session file exists, store its auth as a string in the DB."""
    from src import db_settings
    if db_settings.is_set(db_path, "tg_session_blob"):
        return
    appdata_root = db_path.parent.parent
    candidates = [
        appdata_root / "sessions" / f"{stack.name}.session",
        stack.project_path / f"{stack.name}.session",
        stack.project_path / "copytrades_session.session",
    ]
    legacy_session = next((p for p in candidates if p.exists()), None)
    if legacy_session is None:
        return
    try:
        from telethon.sessions import SQLiteSession, StringSession
        sqlite_session = SQLiteSession(str(legacy_session.with_suffix("")))
        string_session = StringSession.save(sqlite_session)
        db_settings.set_str(db_path, "tg_session_blob", string_session)
        _log.info("migrated session %s -> tg_session_blob", legacy_session)
    except Exception as e:
        _log.warning("session migration failed (%s): %s", legacy_session, e)


def _maybe_run_setup_wizard(stack: Stack, parent_widget) -> None:
    from src import db_settings
    from src.gui.windows.telegram_wizard import TelegramWizard
    missing = db_settings.missing_critical_keys(stack.db_path)
    if not missing:
        return
    QMessageBox.information(
        parent_widget,
        "Setup required",
        "This stack is missing critical settings:\n  - "
        + "\n  - ".join(missing)
        + "\n\nThe setup wizard will open now.",
    )
    TelegramWizard(stack, parent_widget).exec()


def run(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    app = QApplication(argv)
    app.setApplicationName("CopyTrades")
    app.setOrganizationName("CopyTrades")

    qss_path = Path(__file__).resolve().parent / "styles.qss"
    if qss_path.exists():
        app.setStyleSheet(qss_path.read_text(encoding="utf-8"))

    icon_path = _icon_path()
    if icon_path is not None and icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    stacks = discover_stacks()
    state = load_state()

    if not stacks:
        from src.gui.windows.telegram_wizard import TelegramWizard
        wiz = TelegramWizard(None)
        if not wiz.exec() or wiz.stack is None:
            _log.info("user cancelled first-launch wizard; exiting")
            return 0
        active = wiz.stack
    else:
        force_picker = os.environ.get("GUI_FORCE_PICKER", "") == "1"
        active = _pick_stack(stacks, state, force_picker)
        if active is None:
            _log.info("user cancelled stack picker; exiting")
            return 0

    save_state(replace(state, last_stack=active.name))

    _ensure_db_ready(active)

    win = MainWindow(active)
    win.show()
    _maybe_run_setup_wizard(active, win)

    return app.exec()
