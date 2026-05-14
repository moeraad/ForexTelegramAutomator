"""Elevated one-shot: copy bundled nssm.exe into System32.

Invoked via ShellExecuteW "runas" from src.gui.services.bootstrap, either
as `python -m src.gui.helpers.bootstrap_nssm_install` (dev) or as
`CopyTrades.exe --helper bootstrap_nssm_install` (frozen).
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

from src.gui.helpers._helper_log import error, info


def _bundled_path() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys._MEIPASS) / "src" / "gui" / "resources" / "nssm.exe"  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent.parent / "resources" / "nssm.exe"


def main() -> int:
    bundled = _bundled_path()
    if not bundled.exists():
        error("CopyTrades — NSSM install", f"bundled nssm.exe missing at {bundled}")
        return 2
    target = Path(r"C:\Windows\System32\nssm.exe")
    try:
        shutil.copy2(bundled, target)
    except OSError as e:
        error("CopyTrades — NSSM install", f"copy failed: {e}")
        return 4
    info(f"installed nssm.exe → {target}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
