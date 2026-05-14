"""Spawn a one-shot elevated helper via ShellExecuteW 'runas'."""
from __future__ import annotations

import ctypes
import sys
import time
from pathlib import Path


SW_HIDE = 0
SW_SHOWNORMAL = 1


def run_elevated(exe: str, params: str, show: int = SW_SHOWNORMAL) -> bool:
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, show)
    return int(rc) > 32


def run_elevated_python(module: str, args: list[str], show: int = SW_SHOWNORMAL) -> bool:
    python = sys.executable
    if getattr(sys, "frozen", False):
        helper = module.rsplit(".", 1)[-1]
        params = f"--helper {helper}"
    else:
        params = "-m " + module
    if args:
        params += " " + " ".join(f'"{a}"' for a in args)
    return run_elevated(python, params, show)


def wait_for_marker(marker: Path, timeout: float = 60.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if marker.exists():
            return True
        time.sleep(0.5)
    return False
