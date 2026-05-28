"""Safe logging for elevated helpers running under a windowed PyInstaller .exe.

In windowed mode `sys.stderr` is None, so direct writes raise AttributeError.
We tee every helper message to `%TEMP%\\copytrades_helper.log` and (for
errors) pop a native MessageBox so the user actually sees the failure.
"""
from __future__ import annotations

import ctypes
import os
import sys
import tempfile
from datetime import datetime, timezone

_LOG_PATH = os.path.join(tempfile.gettempdir(), "copytrades_helper.log")

_MB_ICONERROR = 0x10


def _to_file(msg: str) -> None:
    try:
        with open(_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now(timezone.utc).isoformat()}  {msg}\n")
    except OSError:
        pass


def info(msg: str) -> None:
    _to_file(msg)
    try:
        if sys.stderr is not None:
            sys.stderr.write(msg + "\n")
    except Exception:
        pass


def _popup_suppressed() -> bool:
    """Skip the MessageBox when running under pytest or any other non-
    interactive context. Tests that exercise the helper's error paths
    would otherwise pop a UAC-style dialog on the developer's screen.

    Two signals:
      - PYTEST_CURRENT_TEST is set by pytest while collecting + running
        each test (auto-cleared between tests).
      - CT_HELPER_NO_POPUP=1 lets CI / scripts opt out explicitly.
    """
    if os.environ.get("CT_HELPER_NO_POPUP") == "1":
        return True
    if os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def error(title: str, msg: str, popup: bool = True) -> None:
    _to_file(f"ERROR  {title}: {msg}")
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"{title}: {msg}\n")
    except Exception:
        pass
    if popup and not _popup_suppressed():
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, title, _MB_ICONERROR)
        except Exception:
            pass


def log_path() -> str:
    return _LOG_PATH
