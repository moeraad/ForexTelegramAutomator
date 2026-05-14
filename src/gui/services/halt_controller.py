"""Read/write settings.kill_switch in a given stack's SQLite DB.

Polls every 2 s so external changes (bot /halt, /resume) are reflected.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


class HaltController(QObject):
    state_changed = Signal(bool)

    def __init__(self, db_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._last: bool | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self._poll)

    def start(self) -> None:
        self._poll()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def rebind(self, db_path: Path) -> None:
        self._db_path = db_path
        self._last = None
        self._poll()

    def is_halted(self) -> bool:
        return self._read() == "on"

    def toggle(self) -> bool:
        new = "off" if self.is_halted() else "on"
        self._write(new)
        self._poll()
        return new == "on"

    def _read(self) -> str:
        if not self._db_path.exists():
            return "off"
        with sqlite3.connect(str(self._db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='kill_switch'"
            ).fetchone()
        return (row[0] if row else "off") or "off"

    def _write(self, value: str) -> None:
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES('kill_switch', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (value,),
            )
            conn.commit()

    def _poll(self) -> None:
        halted = self._read() == "on"
        if halted != self._last:
            self._last = halted
            self.state_changed.emit(halted)
