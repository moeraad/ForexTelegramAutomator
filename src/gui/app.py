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

from PySide6.QtCore import QLockFile, QStandardPaths
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


def _ensure_empty_v2_config() -> None:
    """Write an empty v2 ``stacks_config.json`` if no config exists.

    Without this, V2 Config view sees "no v2 config found" and Add
    dialogs reject because ``self._cfg is None``. Seeding an empty
    ``ConfigV2()`` (zero entities) lets the operator start adding
    entities immediately — the file is real on disk and the first
    save_v2 just appends to it.
    """
    from src import config_v2
    cfg_path = config_v2.config_path()
    if cfg_path.exists():
        return  # don't clobber an existing config (v1 or v2)
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    config_v2.save_v2(config_v2.ConfigV2(), cfg_path)
    _log.info("seeded empty v2 config at %s", cfg_path)


def _empty_stack_for_v2_setup() -> Stack:
    """Construct a placeholder Stack so MainWindow can render before any
    v2 entity has been configured.

    The operator clicked "Skip — use V2 Config" on the first-launch
    wizard. We need MainWindow to open so they can use V2 Config's
    Add Account / Profile / Destination / Bot / Channel dialogs. The
    placeholder Stack points at a throwaway in-APPDATA DB path that
    won't get queried (every per-destination view shows empty data
    until the operator wires a real Destination + restarts the GUI).
    """
    import os
    from pathlib import Path as _Path
    appdata = _Path(os.environ.get("APPDATA", str(_Path.home())))
    placeholder_dir = appdata / "CopyTrades" / "_v2_setup_placeholder"
    placeholder_dir.mkdir(parents=True, exist_ok=True)
    placeholder_db = placeholder_dir / "copytrades.db"
    # Touch the DB so the schema init in Live/Journal/etc views doesn't
    # raise on file-not-found. Empty schema = empty tables = clean
    # "no data yet" rendering.
    if not placeholder_db.exists():
        from src.db import connect, init_schema
        conn = connect(str(placeholder_db))
        init_schema(conn)
        conn.close()
    return Stack(
        name="(setup — use V2 Config)",
        profile_path=placeholder_dir / "profile.json",
        project_path=_Path.cwd(),
        db_path=placeholder_db,
        api_host="127.0.0.1", api_port=8765,
        service_names=("(placeholder)",),
        primary_api_service="(placeholder)",
        primary_bot_service="(placeholder)",
        primary_listener_service="(placeholder)",
    )


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
    _migrate_legacy_channel_profile(stack)


def _migrate_legacy_channel_profile(stack: Stack) -> Stack:
    """Move a legacy <project>/channels/<name>.json profile into the
    stack's APPDATA folder, then rewrite the registry entry to point at
    the new location. No-op if the profile is already where it belongs.
    """
    from src.gui.services.stack_registry import _default_profile_path
    from src.gui.services.stacks_config_io import (
        StackEntry,
        load_entries,
        save_entries,
    )

    target = _default_profile_path(stack.name)
    src_path = stack.profile_path
    if src_path.resolve() == target.resolve():
        return stack  # already migrated
    target.parent.mkdir(parents=True, exist_ok=True)
    try:
        if src_path.exists() and not target.exists():
            shutil.copy2(src_path, target)
            _log.info("migrated channel profile %s -> %s", src_path, target)
    except OSError as e:
        _log.warning("channel profile copy failed (%s): %s", src_path, e)
        return stack
    # Update the registry entry so the GUI uses the new path next launch.
    entries = load_entries()
    changed = False
    for e in entries:
        if e.name == stack.name and Path(e.profile_path) == src_path:
            e_new = StackEntry(
                name=e.name,
                profile_path=str(target),
                project_path=e.project_path,
                db_path=e.db_path,
                service_names=list(e.service_names),
            )
            entries[entries.index(e)] = e_new
            changed = True
            break
    if changed:
        save_entries(entries)
    return stack


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


def _acquire_single_instance_lock(app: QApplication) -> QLockFile | None:
    """Acquire a single-instance lock for the GUI process.

    QLockFile sits at <temp>/copytrades-gui.lock. The default 30s
    stale-lock-recovery is fine: if a previous GUI crashed and Windows
    didn't get to release the file handle, the next launch (after the
    grace window) reclaims it instead of being permanently locked out.

    Returns the QLockFile pinned to `app` (so its lifetime matches the
    process and the lock isn't GC'd mid-run) when acquired, or None
    when another instance already holds it — caller pops a message
    box and exits.

    Service entry points (CopyTrades.exe --service api|bot|listener)
    bypass `run()` entirely (see gui_launcher.py), so they're not
    gated by this lock. Per-stack service uniqueness is enforced
    by NSSM at the SCM level.
    """
    lock_dir = Path(QStandardPaths.writableLocation(
        QStandardPaths.StandardLocation.TempLocation
    ))
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / "copytrades-gui.lock"
    lock = QLockFile(str(lock_path))
    # 3s stale-lock window: long enough to dodge the rare GC race when
    # a fast quit→relaunch happens, short enough that a real crash
    # doesn't lock the operator out for half a minute. The original 30s
    # default surfaced as "another instance is open" complaints whenever
    # someone clicked Quit-from-tray and immediately relaunched.
    lock.setStaleLockTime(3_000)
    if lock.tryLock(0):
        # Pin to the QApplication so the QLockFile (and thus the OS-
        # level lock) outlives this function. Without the attachment
        # Python would GC it, releasing the lock immediately.
        app._copytrades_single_instance_lock = lock  # type: ignore[attr-defined]
        # Belt-and-braces: explicitly unlock when the QApplication is
        # tearing down, so a fast relaunch sees the slot free instead
        # of waiting for the GC to release the QLockFile destructor.
        app.aboutToQuit.connect(lambda l=lock: l.unlock())
        return lock
    return None


def run(argv: list[str] | None = None) -> int:
    argv = list(argv if argv is not None else sys.argv)
    app = QApplication(argv)
    app.setApplicationName("CopyTrades")
    app.setOrganizationName("CopyTrades")

    # Single-instance guard: refuse to start if another GUI is already
    # running. Two concurrent GUIs against the same DB cause confusing
    # conflicts (each rebuilds the same trigger cache, each runs its
    # own crash watcher, both write to logs/ at the same time, etc.).
    # Better to surface the duplicate clearly and let the operator
    # find the existing window.
    if _acquire_single_instance_lock(app) is None:
        QMessageBox.information(
            None,  # type: ignore[arg-type]  # app-modal, no parent yet
            "CopyTrades already running",
            "Another instance of CopyTrades is already open. Look for "
            "its window in the taskbar or system tray (right-click the "
            "tray icon → Quit CopyTrades to close it cleanly).\n\n"
            "If you JUST quit and got this anyway, wait ~3 seconds and "
            "try again — the lock takes a moment to release.",
        )
        return 0

    # Apply the persisted theme BEFORE any view is built so qfluentwidgets
    # and inline-styled widgets get the right palette on first paint.
    from src.gui.theme import apply_theme, load_persisted
    apply_theme(app, load_persisted())

    icon_path = _icon_path()
    if icon_path is not None and icon_path.exists():
        app.setWindowIcon(QIcon(str(icon_path)))

    stacks = discover_stacks()
    state = load_state()

    if not stacks:
        # First launch: do NOT auto-open the wizard. Just write an empty
        # v2 config + open the main window pointed at a placeholder
        # destination so the operator can use V2 Config to add Account
        # / Profile / Destination / Bot / Channel one at a time. The
        # wizard remains reachable via Settings → "Setup wizard" for
        # operators who want the guided flow.
        _ensure_empty_v2_config()
        active = _empty_stack_for_v2_setup()
        _log.info("first launch: opened V2 Config with empty config")
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
    return app.exec()
