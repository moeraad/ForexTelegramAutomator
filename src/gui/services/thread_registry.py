"""Tracks live QThread instances so MainWindow.closeEvent can stop them
cleanly instead of leaving the dreaded
``QThread: Destroyed while thread '' is still running`` warning at
shutdown.

Each long-lived thread registers itself; it auto-unregisters when its
``finished`` signal fires. ``stop_all`` is called once from
MainWindow.closeEvent.
"""
from __future__ import annotations

import logging
from typing import Callable

from PySide6.QtCore import QThread


_log = logging.getLogger("gui.threads")
_entries: list[tuple[QThread, Callable[[], None] | None]] = []


def register(thread: QThread, stop_fn: Callable[[], None] | None = None) -> None:
    """Track a thread. ``stop_fn`` is called before ``quit()`` to ask the
    thread to break out of any blocking work (e.g. a poll loop)."""
    _entries.append((thread, stop_fn))
    thread.finished.connect(lambda t=thread: _unregister(t))


def _unregister(thread: QThread) -> None:
    global _entries
    _entries = [(t, s) for t, s in _entries if t is not thread]


def stop_all(timeout_ms: int = 2000) -> None:
    """Signal every registered thread to stop, then wait briefly for each.

    Errors are logged and swallowed — shutdown must never raise.
    """
    snapshot = list(_entries)
    for thread, stop_fn in snapshot:
        try:
            if not thread.isRunning():
                continue
            if stop_fn is not None:
                try:
                    stop_fn()
                except Exception as e:  # noqa: BLE001
                    _log.debug("stop_fn failed for %s: %s", thread, e)
            thread.requestInterruption()
            thread.quit()
        except RuntimeError as e:
            # Already destroyed C++ side — ignore.
            _log.debug("thread already gone: %s", e)
    for thread, _stop_fn in snapshot:
        try:
            if thread.isRunning():
                thread.wait(timeout_ms)
        except RuntimeError:
            pass
