"""Thin wrappers around `where`, `sc query`, and `nssm` CLI calls."""
from __future__ import annotations

import shutil
import subprocess


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


def nssm_start(name: str) -> tuple[bool, str]:
    proc = _run(["nssm", "start", name], timeout=30.0)
    ok = proc.returncode == 0 or "already" in (proc.stdout + proc.stderr).lower()
    return ok, (proc.stdout + proc.stderr).strip()


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
