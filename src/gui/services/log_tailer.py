"""Tail a UTF-8 log file. Emits new lines as they're written.

Poll-based (500 ms) so it survives rotating log files: when the file
shrinks (rotation) we reset the read offset to the new beginning.
"""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


_INITIAL_TAIL_BYTES = 16 * 1024


class LogTailer(QObject):
    line = Signal(str)
    rotated = Signal()

    def __init__(self, path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._path = path
        self._offset = 0
        self._buf = ""
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        if not self._path.exists():
            self._timer.start()
            return
        size = self._path.stat().st_size
        self._offset = max(0, size - _INITIAL_TAIL_BYTES)
        self._read_new()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def rebind(self, path: Path) -> None:
        self.stop()
        self._path = path
        self._offset = 0
        self._buf = ""
        self.start()

    def _tick(self) -> None:
        if not self._path.exists():
            return
        try:
            size = self._path.stat().st_size
        except OSError:
            return
        if size < self._offset:
            self._offset = 0
            self._buf = ""
            self.rotated.emit()
        if size == self._offset:
            return
        self._read_new()

    def _read_new(self) -> None:
        try:
            with self._path.open("rb") as f:
                f.seek(self._offset)
                chunk = f.read()
                self._offset = f.tell()
        except OSError:
            return
        if not chunk:
            return
        text = self._buf + chunk.decode("utf-8", errors="replace")
        lines = text.split("\n")
        self._buf = lines.pop()
        for line in lines:
            self.line.emit(line)
