"""Elevated one-shot: stop + delete one or more NSSM services.

argv: <svc_name> [<svc_name> ...]

Idempotent: services that aren't registered are skipped silently. A
running service is stopped first (with a 10s SCM transition wait) so
the subsequent ``sc delete`` doesn't leave a marked-for-deletion ghost
until next reboot.

Uses sc.exe rather than nssm so the helper has no dependency on the
bundled nssm.exe path (which moves whenever PyInstaller's layout
changes). sc.exe is part of base Windows and always present.

Two callers historically:

  - The legacy "uninstall services" GUI button passed exactly three
    args (api_svc, bot_svc, listener_svc) to remove the full per-stack
    triple. That invocation still works — the helper accepts ANY number
    of service names ≥1.

  - Step 8 of the multi-channel plan passes the list of dangling legacy
    ``CT-<NAME>-Listener`` services found in the registry but no longer
    named in v2 config. The list length is N (one per migrated stack).
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
    # Accept any number of service names ≥1. The legacy 3-arg form keeps
    # working; new callers (Step 8 listener migration) can pass any N.
    names = [a.strip() for a in argv if a and a.strip()]
    if not names:
        # Most common cause: caller passed a tuple containing only empty
        # strings (e.g., v2 entities whose service_name field was left
        # blank). Distinguish "operator passed nothing" from "operator
        # passed blanks" — the latter is a config-level issue, not a
        # helper-invocation bug.
        if argv:
            msg = (
                "no usable service names to remove — every name passed was "
                "blank. This usually means the channel's Account / Destination "
                "/ Bot v2 entries have empty service_name fields. Open "
                "stacks_config.json and fill them in (e.g. CT-MyApi, "
                "CT-MyBot, CT-Listener-myacct), then retry.\n\n"
                f"Argv received: {argv!r}"
            )
        else:
            msg = (
                "helper invoked with no service names; expected at least one."
            )
        error("CopyTrades - services uninstall", msg)
        return 2

    failures: list[str] = []
    for svc in names:
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
