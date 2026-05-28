"""Bootstrap state machine: NSSM → services registered → running → /health.

Emits Qt signals so the splash window can render per-stack progress.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.gui.services import nssm_client
from src.gui.services.elevation import run_elevated_python
from src.gui.services.health_pinger import ping_with_retry
from src.gui.services.stack_registry import Stack


log = logging.getLogger("bootstrap")


def _read_service_crashlog_tail(service_name: str, max_chars: int = 1200) -> str:
    """Return the tail of the matching gui_launcher crashlog, if any.

    Service names follow CT-<STACK>-(Api|Bot|Listener); the per-target
    crashlog lives at %APPDATA%\\CopyTrades\\service-<target>-crash.log
    (LocalSystem's %APPDATA% when NSSM runs as LocalSystem). Surfacing
    the tail in the failure message turns opaque NSSM throttle errors
    into actionable tracebacks.
    """
    suffix = service_name.rsplit("-", 1)[-1].lower()
    if suffix not in {"api", "bot", "listener"}:
        return ""
    candidates: list[Path] = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        candidates.append(Path(appdata) / "CopyTrades" / f"service-{suffix}-crash.log")
    candidates.append(
        Path(r"C:\Windows\System32\config\systemprofile\AppData\Roaming\CopyTrades")
        / f"service-{suffix}-crash.log"
    )
    for path in candidates:
        try:
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            tail = text[-max_chars:]
            return f"crashlog ({path}):\n{tail}"
        except OSError:
            continue
    return ""


_CREATE_NO_WINDOW = 0x08000000


def _list_installed_services() -> list[str]:
    """Return all installed service names. Uses ``sc query state= all``
    which lists every service regardless of state. No NSSM dependency."""
    try:
        proc = subprocess.run(
            ["sc.exe", "query", "type=", "service", "state=", "all"],
            capture_output=True, text=True, timeout=20.0,
            creationflags=_CREATE_NO_WINDOW,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    names: list[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("SERVICE_NAME:"):
            name = stripped[len("SERVICE_NAME:"):].strip()
            if name:
                names.append(name)
    return names


class Step(str, Enum):
    NSSM = "NSSM"
    SERVICES = "Services"
    RUNNING = "Running"
    API = "API"


class _StackWorker(QThread):
    step_started = Signal(str, str)
    step_succeeded = Signal(str, str)
    step_failed = Signal(str, str, str)
    stack_completed = Signal(str)

    def __init__(self, stack: Stack, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.stack = stack

    def run(self) -> None:
        if not self._run_step(Step.NSSM, self._ensure_nssm):
            return
        if not self._run_step(Step.SERVICES, self._ensure_services_registered):
            return
        if not self._run_step(Step.RUNNING, self._ensure_services_running):
            return
        if not self._run_step(Step.API, self._ensure_api_reachable):
            return
        self.stack_completed.emit(self.stack.name)

    def _run_step(self, step: Step, fn) -> bool:
        self.step_started.emit(self.stack.name, step.value)
        ok, err = fn()
        if ok:
            self.step_succeeded.emit(self.stack.name, step.value)
            return True
        self.step_failed.emit(self.stack.name, step.value, err)
        return False

    def _ensure_nssm(self) -> tuple[bool, str]:
        if nssm_client.nssm_available():
            return True, ""
        ok = run_elevated_python("src.gui.helpers.bootstrap_nssm_install", [])
        if not ok:
            return False, "elevation cancelled or ShellExecuteW failed"
        # Re-check after elevated install.
        for _ in range(20):
            if nssm_client.nssm_available():
                return True, ""
            QThread.msleep(500)
        return False, "nssm still not on PATH after elevated install"

    def _ensure_services_registered(self) -> tuple[bool, str]:
        """Phase-4 v2 cleanup: per-stack install now uses the entity-driven
        v2 spec helper instead of the rigid 3-tuple call.

        Why: with Phase 1's broader ``service_names`` tuple, the legacy
        helper silently misses extra bots/accounts (it only registered
        positions 0-2 as api/bot/listener). Routing through the v2 spec
        helper picks up every service the destination actually needs.
        """
        missing = [n for n in self.stack.service_names if not nssm_client.service_exists(n)]
        stale = [n for n in self.stack.service_names
                 if nssm_client.service_exists(n)
                 and not self._service_args_ok(n)]
        if not missing and not stale:
            return True, ""

        # Derive the spec for THIS destination only (not the whole config)
        # so the per-stack flow stays scoped. Falls back to the legacy
        # 3-tuple call when v2 config isn't loadable (single-stack v1
        # installs that haven't been migrated yet).
        spec = self._derive_per_stack_spec()
        if not spec:
            ok = run_elevated_python(
                "src.gui.helpers.bootstrap_services_install",
                [
                    self.stack.name,
                    str(self.stack.project_path),
                    *self.stack.service_names,
                    str(self.stack.db_path),
                ],
            )
        else:
            import json as _json
            import tempfile
            fd, spec_path = tempfile.mkstemp(
                prefix="ct_v2_spec_stack_", suffix=".json", text=True,
            )
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    _json.dump(spec, f, indent=2)
            except Exception:
                try:
                    os.close(fd)
                except Exception:
                    pass
                raise
            from src.config import BASE_DIR
            ok = run_elevated_python(
                "src.gui.helpers.bootstrap_v2_install",
                [spec_path, str(BASE_DIR)],
            )

        if not ok:
            return False, "elevation cancelled or ShellExecuteW failed"
        for _ in range(30):
            if all(nssm_client.service_exists(n) for n in self.stack.service_names):
                return True, ""
            QThread.msleep(500)
        return False, f"still missing services: {','.join(missing)}"

    def _derive_per_stack_spec(self) -> list[dict]:
        """Return the v2-spec entries for the destination this stack
        represents. Empty when v2 config can't be loaded — caller falls
        back to the legacy 3-tuple helper."""
        try:
            from src import config_v2
            from src.gui.services.v2_service_spec import (
                derive_v2_service_spec,
            )
            cfg_path = config_v2.config_path()
            if not config_v2.is_v2(cfg_path):
                return []
            cfg = config_v2.load_v2(cfg_path)
            if cfg is None:
                return []
            full = derive_v2_service_spec(cfg)
            # Narrow to the services touching THIS stack — match by the
            # full service_names list (Phase 1's union of api+bots+accts).
            wanted = set(self.stack.service_names)
            return [e for e in full if e["service"] in wanted]
        except Exception:
            log.exception("per-stack v2 spec derivation failed; falling back")
            return []

    def _service_args_ok(self, name: str) -> bool:
        """Check that the installed service points at the stack's db_path."""
        params = nssm_client.nssm_get(name, "AppParameters")
        if not params:
            return False
        want = str(self.stack.db_path).lower()
        return want in params.lower()

    def _ensure_services_running(self) -> tuple[bool, str]:
        errors: list[str] = []
        for name in self.stack.service_names:
            if nssm_client.service_running(name):
                continue
            ok, msg = nssm_client.nssm_start(name)
            if not ok:
                crash_tail = _read_service_crashlog_tail(name)
                detail = f"{msg}\n\n{crash_tail}" if crash_tail else msg
                errors.append(f"{name}: {detail}")
        if errors:
            return False, " | ".join(errors)
        # Confirm running state.
        for _ in range(20):
            if all(nssm_client.service_running(n) for n in self.stack.service_names):
                return True, ""
            QThread.msleep(500)
        return False, "services not RUNNING within 10s"

    def _ensure_api_reachable(self) -> tuple[bool, str]:
        ok = ping_with_retry(f"{self.stack.api_url}/health", total_timeout=30.0)
        return (True, "") if ok else (False, f"GET {self.stack.api_url}/health failed within 30s")


class BootstrapManager(QObject):
    step_started = Signal(str, str)
    step_succeeded = Signal(str, str)
    step_failed = Signal(str, str, str)
    stack_completed = Signal(str)
    all_completed = Signal()

    def __init__(self, stacks: list[Stack], parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stacks = stacks
        self._workers: list[_StackWorker] = []
        self._remaining = len(stacks)

    def start(self) -> None:
        if not self._stacks:
            self.all_completed.emit()
            return
        # Step 8: one-shot migration to remove legacy per-stack listener
        # services after the v1→v2 config migration repointed each Stack's
        for stack in self._stacks:
            worker = _StackWorker(stack, parent=self)
            worker.step_started.connect(self.step_started)
            worker.step_succeeded.connect(self.step_succeeded)
            worker.step_failed.connect(self.step_failed)
            worker.step_failed.connect(self._on_stack_done)
            worker.stack_completed.connect(self.stack_completed)
            worker.stack_completed.connect(self._on_stack_done)
            worker.finished.connect(worker.deleteLater)
            self._workers.append(worker)
            from src.gui.services.thread_registry import register
            register(worker, stop_fn=worker.quit)
            worker.start()

    def _on_stack_done(self, *_args: object) -> None:
        self._remaining -= 1
        if self._remaining <= 0:
            self.all_completed.emit()


def install_v2_services_all() -> tuple[bool, str]:
    """Phase-3 v2 cleanup: register EVERY v2 service in one elevated pass.

    Reads ``stacks_config.json``, derives the full N-service spec
    (1 API per Destination + 1 per Bot + 1 per Account), writes it to
    a temp JSON file, and ShellExecuteW's the elevated
    ``bootstrap_v2_install`` helper.

    Returns ``(ok, message)``. ``ok=False`` when:
      - v2 config missing or unloadable
      - spec is empty (nothing to install)
      - elevation cancelled or helper returned non-zero

    Caller (the Settings view's "Install ALL v2 services" button) uses
    the ``message`` for the result dialog. Wins over the per-stack path
    in multi-bot / multi-account setups where the legacy 3-tuple
    install helper would silently miss extras.
    """
    import json as _json
    import tempfile
    from pathlib import Path as _Path
    from src import config_v2
    from src.gui.services.v2_service_spec import (
        derive_v2_service_spec,
    )

    cfg_path = config_v2.config_path()
    if not config_v2.is_v2(cfg_path):
        return False, (
            "v2 config not found — open Settings → Add Stack to migrate."
        )
    cfg = config_v2.load_v2(cfg_path)
    if cfg is None:
        return False, "v2 config could not be loaded."

    spec = derive_v2_service_spec(cfg)
    if not spec:
        return False, (
            "v2 config has no installable services. Add at least one "
            "Destination + a Channel routing through it first."
        )

    # Spec lives in TEMP for the duration of the elevated call. We don't
    # bother cleaning up — Windows wipes %TEMP% on its own schedule, the
    # spec is tiny, and operator might want to inspect it after a
    # failure.
    fd, spec_path = tempfile.mkstemp(
        prefix="ct_v2_spec_", suffix=".json", text=True,
    )
    try:
        with open(fd, "w", encoding="utf-8") as f:
            _json.dump(spec, f, indent=2)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise

    from src.config import BASE_DIR
    ok = run_elevated_python(
        "src.gui.helpers.bootstrap_v2_install",
        [spec_path, str(BASE_DIR)],
    )
    if not ok:
        return False, (
            "Elevation cancelled or helper failed. Spec was written to:\n"
            f"  {spec_path}\n\nYou can re-run with admin rights manually if needed."
        )
    return True, (
        f"Registered {len(spec)} v2 service(s). Open Services.msc to confirm "
        "or start them via Settings."
    )


def install_v2_service_subset(entries: list[dict]) -> tuple[bool, str]:
    """Install ONE or more named v2 services in a single elevated call.

    Caller provides the spec entries directly (typically derived from
    ``derive_v2_service_spec`` and filtered down to the desired subset
    — e.g. a single row clicked in the Services tab). Returns the same
    ``(ok, message)`` shape as ``install_v2_services_all``.
    """
    import json as _json
    import tempfile

    if not entries:
        return False, "no service entries supplied"

    fd, spec_path = tempfile.mkstemp(
        prefix="ct_v2_spec_sub_", suffix=".json", text=True,
    )
    try:
        with open(fd, "w", encoding="utf-8") as f:
            _json.dump(entries, f, indent=2)
    except Exception:
        try:
            os.close(fd)
        except Exception:
            pass
        raise

    from src.config import BASE_DIR
    ok = run_elevated_python(
        "src.gui.helpers.bootstrap_v2_install",
        [spec_path, str(BASE_DIR)],
    )
    if not ok:
        names = ", ".join(e.get("service", "") for e in entries) or "(unnamed)"
        return False, (
            f"Elevation cancelled or helper failed for: {names}.\n"
            f"Spec was written to:\n  {spec_path}"
        )
    names = ", ".join(e.get("service", "") for e in entries)
    return True, f"Registered: {names}"
