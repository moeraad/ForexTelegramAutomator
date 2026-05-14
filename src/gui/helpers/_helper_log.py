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


def error(title: str, msg: str, popup: bool = True) -> None:
    _to_file(f"ERROR  {title}: {msg}")
    try:
        if sys.stderr is not None:
            sys.stderr.write(f"{title}: {msg}\n")
    except Exception:
        pass
    if popup:
        try:
            ctypes.windll.user32.MessageBoxW(None, msg, title, _MB_ICONERROR)
        except Exception:
            pass


def log_path() -> str:
    return _LOG_PATH
