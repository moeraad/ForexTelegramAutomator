"""Polls each stack service for crash signals.

A "crash" is one of:
  - service was RUNNING last tick, now isn't (process died, NSSM about
    to restart or gave up)
  - err-log file size grew since last tick (process restarted or wrote
    a fresh traceback)

Emits ``crashed(service_name, tail_lines, log_path)`` on each event so
the banner can show the last 10 stderr lines.
"""
from __future__ import annotations

import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QThread, Signal

from src.gui.services import nssm_client
from src.gui.services.stack_registry import Stack


_log = logging.getLogger("gui.crash_watcher")


@dataclass
class _ServiceState:
    running: bool
    log_size: int


def _service_to_log_tag(service_name: str) -> str:
    """CT-FOREXENGINEER-Api -> 'api'."""
    last = service_name.rsplit("-", 1)[-1]
    return last.lower()


def _tail(path: Path, lines: int = 10, max_bytes: int = 64 * 1024) -> list[str]:
    """Return the last N non-empty lines from a file. Cheap; bounded read."""
    try:
        size = path.stat().st_size
    except OSError:
        return []
    if size == 0:
        return []
    read = min(size, max_bytes)
    try:
        with path.open("rb") as f:
            f.seek(size - read)
            chunk = f.read(read)
    except OSError:
        return []
    text = chunk.decode("utf-8", errors="replace")
    out = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return out[-lines:]


class CrashWatcher(QThread):
    crashed = Signal(str, list, str)   # service_name, tail_lines, log_path
    recovered = Signal(str)            # service_name (running again)

    def __init__(self, stack: Stack, parent=None) -> None:
        super().__init__(parent)
        self._stack = stack
        self._stop = threading.Event()
        self._state: dict[str, _ServiceState] = {}

    def set_stack(self, stack: Stack) -> None:
        self._stack = stack
        self._state.clear()

    def stop(self) -> None:
        self._stop.set()

    def _log_path(self, service: str) -> Path:
        tag = _service_to_log_tag(service)
        return self._stack.db_path.parent / "logs" / f"nssm-{tag}.err.log"

    def _read_state(self, service: str) -> _ServiceState:
        running = nssm_client.service_running(service)
        log_size = 0
        log = self._log_path(service)
        try:
            log_size = log.stat().st_size if log.exists() else 0
        except OSError:
            log_size = 0
        return _ServiceState(running=running, log_size=log_size)

    def run(self) -> None:
        # Seed initial state so we don't fire on first tick.
        for svc in self._stack.service_names:
            self._state[svc] = self._read_state(svc)
        while not self._stop.is_set():
            for svc in self._stack.service_names:
                prev = self._state.get(svc)
                curr = self._read_state(svc)
                if prev is None:
                    self._state[svc] = curr
                    continue
                # Only treat RUNNING -> NOT-RUNNING as a crash. The
                # earlier "err-log grew" signal was too noisy because
                # NSSM redirects every stderr write (including INFO
                # logging lines) to the err-log, so it fired on every
                # heartbeat. Operators can inspect logs/<svc>.err.log
                # directly if they want runtime warnings.
                if prev.running and not curr.running:
                    self._emit_crash(svc)
                elif not prev.running and curr.running:
                    self.recovered.emit(svc)
                self._state[svc] = curr
            self._stop.wait(timeout=5.0)

    def _emit_crash(self, service: str) -> None:
        log = self._log_path(service)
        tail = _tail(log, lines=10)
        self.crashed.emit(service, tail, str(log))
