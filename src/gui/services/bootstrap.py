"""Bootstrap state machine: NSSM → services registered → running → /health.

Emits Qt signals so the splash window can render per-stack progress.
"""
from __future__ import annotations

import sys
from enum import Enum
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from src.gui.services import nssm_client
from src.gui.services.elevation import run_elevated_python
from src.gui.services.health_pinger import ping_with_retry
from src.gui.services.stack_registry import Stack


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
        missing = [n for n in self.stack.service_names if not nssm_client.service_exists(n)]
        stale = [n for n in self.stack.service_names
                 if nssm_client.service_exists(n)
                 and not self._service_args_ok(n)]
        if not missing and not stale:
            return True, ""
        ok = run_elevated_python(
            "src.gui.helpers.bootstrap_services_install",
            [
                self.stack.name,
                str(self.stack.project_path),
                *self.stack.service_names,
                str(self.stack.db_path),
            ],
        )
        if not ok:
            return False, "elevation cancelled or ShellExecuteW failed"
        for _ in range(30):
            if all(nssm_client.service_exists(n) for n in self.stack.service_names):
                return True, ""
            QThread.msleep(500)
        return False, f"still missing services: {','.join(missing)}"

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
                errors.append(f"{name}: {msg}")
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
