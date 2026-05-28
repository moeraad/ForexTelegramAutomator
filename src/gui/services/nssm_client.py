"""Thin wrappers around `where`, `sc query`, and `nssm` CLI calls."""
from __future__ import annotations

import shutil
import subprocess
import time


_CREATE_NO_WINDOW = 0x08000000


def _decode(b: bytes) -> str:
    """NSSM emits UTF-16LE on Windows when stdout is a pipe; sc/where emit OEM."""
    if not b:
        return ""
    if len(b) >= 2 and b[1] == 0:
        try:
            return b.decode("utf-16-le").replace("\x00", "")
        except UnicodeDecodeError:
            pass
    for enc in ("utf-8", "cp1252", "cp850"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", errors="replace")


def _run(args: list[str], timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            timeout=timeout,
            creationflags=_CREATE_NO_WINDOW,
        )
        return subprocess.CompletedProcess(
            args,
            returncode=proc.returncode,
            stdout=_decode(proc.stdout),
            stderr=_decode(proc.stderr),
        )
    except (FileNotFoundError, OSError) as e:
        return subprocess.CompletedProcess(args, returncode=127, stdout="", stderr=str(e))
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(args, returncode=124, stdout="", stderr=str(e))


def nssm_available() -> bool:
    return shutil.which("nssm") is not None


def service_exists(name: str) -> bool:
    proc = _run(["sc", "query", name])
    if proc.returncode != 0:
        return False
    return "SERVICE_NAME" in proc.stdout or "STATE" in proc.stdout


def service_running(name: str) -> bool:
    proc = _run(["sc", "query", name])
    if proc.returncode != 0:
        return False
    return "RUNNING" in proc.stdout


def _is_service_paused_throttle(output: str) -> bool:
    """NSSM emits "Unexpected status SERVICE_PAUSED in response to START
    control." when the target service is sitting in its post-crash
    restart-throttle window. The state is transient: NSSM will auto-
    resume after AppRestartDelay. Callers should wait and retry rather
    than treat this as a hard failure.
    """
    lo = output.lower()
    return "service_paused" in lo and "start control" in lo


def _is_service_start_pending(output: str) -> bool:
    """NSSM emits "Unexpected status SERVICE_START_PENDING in response
    to START control." when the SCM accepted the start request and the
    service is mid-launch (transitional state on the way to RUNNING).
    This is *success*, not failure — NSSM just doesn't wait for the
    state to settle. Callers should poll service_running() instead of
    treating it as an error. Concrete instance: clicking Start ALL on
    CT-Bot-bot_smc surfaces the spurious error even though the bot
    starts normally a second later.
    """
    lo = output.lower()
    return "service_start_pending" in lo and "start control" in lo


def nssm_start(name: str) -> tuple[bool, str]:
    """Start an NSSM service, transparently riding out SERVICE_PAUSED
    throttle and SERVICE_START_PENDING transient states.

    Some operator flows (e.g. clicking Start before the relevant config
    is in place) can leave the service in NSSM's restart-throttle window.
    A naive `nssm start` returns the throttle artifact instead of doing
    anything useful. We detect that, send `nssm continue` to clear the
    paused state, and retry.

    Separately, SCM-side latency means the service often reports
    SERVICE_START_PENDING when NSSM checks immediately after issuing the
    start — NSSM bubbles this up as a non-zero exit even though the start
    actually succeeded. We poll service_running() to confirm the real
    state before declaring failure.

    Total retry budget caps at ~30s so the GUI splash never hangs longer
    than the AppRestartDelay (5s) plus margin.
    """
    deadline = time.monotonic() + 30.0
    last_out = ""
    while True:
        proc = _run(["nssm", "start", name], timeout=30.0)
        out = (proc.stdout + proc.stderr).strip()
        last_out = out
        if proc.returncode == 0 or "already" in out.lower():
            return True, out
        if _is_service_start_pending(out):
            # Service is mid-launch. Poll briefly to confirm it reaches
            # RUNNING — that's the real success signal. Caps at ~10s to
            # match Windows' default start-pending budget.
            poll_until = min(time.monotonic() + 10.0, deadline)
            while time.monotonic() < poll_until:
                if service_running(name):
                    return True, f"{out} (reached RUNNING)"
                time.sleep(0.5)
            # If it never reached RUNNING within the window, fall through
            # to the generic failure return below.
            return False, f"{out} (still not RUNNING after 10s)"
        if _is_service_paused_throttle(out) and time.monotonic() < deadline:
            # Best-effort resume; ignore output. NSSM accepts `continue`
            # to take a service out of SERVICE_PAUSED.
            _run(["nssm", "continue", name], timeout=10.0)
            time.sleep(2.0)
            continue
        return False, out
    # unreachable; keeps type-checkers happy
    return False, last_out


def nssm_stop(name: str) -> tuple[bool, str]:
    proc = _run(["nssm", "stop", name], timeout=30.0)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def nssm_restart(name: str) -> tuple[bool, str]:
    proc = _run(["nssm", "restart", name], timeout=30.0)
    return proc.returncode == 0, (proc.stdout + proc.stderr).strip()


def nssm_status(name: str) -> str:
    proc = _run(["nssm", "status", name])
    return proc.stdout.strip() or proc.stderr.strip()


def nssm_get(name: str, key: str) -> str:
    """Read a single NSSM parameter (e.g. AppParameters, Application)."""
    proc = _run(["nssm", "get", name, key])
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()
