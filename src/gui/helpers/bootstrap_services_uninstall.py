"""Elevated one-shot: stop + delete CT-<NAME>-Api/Bot/Listener services.

argv: <api_svc> <bot_svc> <listener_svc>

Uses sc.exe rather than nssm so the helper has no dependency on the
bundled nssm.exe path (which moves whenever PyInstaller's layout
changes). sc.exe is part of base Windows and always present.

Sequencing matters: a `sc delete` issued against a RUNNING service
marks the service for deletion but leaves it active until the next
reboot, which defeats the point of an "uninstall services" button.
We stop first, give the SCM up to ~10s to transition out of RUNNING,
then delete.
"""
from __future__ import annotations

import subprocess
import sys
import time

from src.gui.helpers._helper_log import error, info

_CREATE_NO_WINDOW = 0x08000000


def _sc(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sc.exe", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        creationflags=_CREATE_NO_WINDOW,
    )


def _service_exists(name: str) -> bool:
    proc = _sc("query", name)
    return proc.returncode == 0 and ("SERVICE_NAME" in proc.stdout or "STATE" in proc.stdout)


def _wait_stopped(name: str, timeout: float = 10.0) -> bool:
    """Poll `sc query` until the service is STOPPED or the deadline expires."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        proc = _sc("query", name)
        if proc.returncode != 0:
            return True  # service vanished — treat as stopped
        if "STOPPED" in proc.stdout:
            return True
        time.sleep(0.5)
    return False


def _remove_one(name: str) -> tuple[bool, str]:
    if not _service_exists(name):
        info(f"{name}: not registered, skipping")
        return True, "not registered"

    info(f"{name}: stopping")
    _sc("stop", name)  # ignore rc — already-stopped returns 1062

    if not _wait_stopped(name):
        info(f"{name}: did not reach STOPPED within 10s, deleting anyway")

    info(f"{name}: deleting")
    proc = _sc("delete", name)
    if proc.returncode != 0:
        msg = (proc.stdout + proc.stderr).strip()
        return False, f"sc delete failed (rc={proc.returncode}): {msg}"
    return True, "removed"


def main(argv: list[str]) -> int:
    if len(argv) < 3:
        error(
            "CopyTrades - services uninstall",
            f"helper invoked with {len(argv)} args, need 3:\n"
            "<api_svc> <bot_svc> <listener_svc>",
        )
        return 2

    api_svc, bot_svc, listener_svc = argv[:3]
    failures: list[str] = []
    for svc in (api_svc, bot_svc, listener_svc):
        ok, msg = _remove_one(svc)
        info(f"{svc}: {msg}")
        if not ok:
            failures.append(f"{svc}: {msg}")

    if failures:
        error(
            "CopyTrades - services uninstall",
            "Some services could not be removed:\n\n" + "\n".join(failures),
        )
        return 4
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
