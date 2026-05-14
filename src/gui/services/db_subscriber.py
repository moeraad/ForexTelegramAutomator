"""Polls the active stack's copytrades.db at 250 ms and emits Qt signals on diff.

Models attach to the `query_changed` signal for their SQL of interest.
A single QTimer per stack keeps load light; each subscription has its own
content-hash so unrelated changes don't trigger redundant model resets.
"""
from __future__ import annotations

import hashlib
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer, Signal


@dataclass
class _Subscription:
    sql: str
    params: tuple
    hasher: Callable[[list[sqlite3.Row]], str]
    last_hash: str = ""
    callback: Callable[[list[sqlite3.Row]], None] | None = None


def _default_hash(rows: list[sqlite3.Row]) -> str:
    h = hashlib.sha1()
    for r in rows:
        h.update(repr(tuple(r)).encode("utf-8"))
        h.update(b"|")
    return h.hexdigest()


class DBSubscriber(QObject):
    query_changed = Signal(str, list)

    def __init__(self, db_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._subs: dict[str, _Subscription] = {}
        self._timer = QTimer(self)
        self._timer.setInterval(250)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._tick()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def rebind(self, db_path: Path) -> None:
        self._db_path = db_path
        for sub in self._subs.values():
            sub.last_hash = ""
        self._tick()

    def subscribe(
        self,
        key: str,
        sql: str,
        params: tuple = (),
        hasher: Callable[[list[sqlite3.Row]], str] | None = None,
        callback: Callable[[list[sqlite3.Row]], None] | None = None,
    ) -> None:
        self._subs[key] = _Subscription(
            sql=sql, params=params, hasher=hasher or _default_hash, callback=callback
        )

    def unsubscribe(self, key: str) -> None:
        self._subs.pop(key, None)

    def _tick(self) -> None:
        if not self._db_path.exists():
            return
        with sqlite3.connect(str(self._db_path)) as conn:
            conn.row_factory = sqlite3.Row
            for key, sub in self._subs.items():
                rows = list(conn.execute(sub.sql, sub.params).fetchall())
                new_hash = sub.hasher(rows)
                if new_hash == sub.last_hash:
                    continue
                sub.last_hash = new_hash
                if sub.callback is not None:
                    sub.callback(rows)
                self.query_changed.emit(key, rows)
