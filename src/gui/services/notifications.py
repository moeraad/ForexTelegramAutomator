"""Watch the active stack's DB and emit desktop-notification events.

Polls every 5 s. Tracks last-seen action.id and position.id so each event
fires exactly once per row. Skips initial backlog (so launching the app
doesn't fire 100 notifications for old rows).
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


@dataclass(frozen=True)
class NotifEvent:
    kind: str
    title: str
    body: str
    severity: str


class NotificationWatcher(QObject):
    event = Signal(object)

    def __init__(self, db_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._last_action_id: int | None = None
        self._last_position_closed_id: int | None = None
        self._last_halt_state: str | None = None
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._prime()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def rebind(self, db_path: Path) -> None:
        self._db_path = db_path
        self._last_action_id = None
        self._last_position_closed_id = None
        self._last_halt_state = None
        self._prime()

    def _prime(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                row = conn.execute("SELECT MAX(id) FROM actions").fetchone()
                self._last_action_id = int(row[0]) if row and row[0] else 0
                row = conn.execute(
                    "SELECT MAX(id) FROM positions WHERE status='closed'"
                ).fetchone()
                self._last_position_closed_id = int(row[0]) if row and row[0] else 0
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='kill_switch'"
                ).fetchone()
                self._last_halt_state = str(row[0]) if row else "off"
        except sqlite3.Error:
            return

    def _tick(self) -> None:
        if not self._db_path.exists():
            return
        try:
            with sqlite3.connect(str(self._db_path)) as conn:
                conn.row_factory = sqlite3.Row
                self._scan_actions(conn)
                self._scan_closed_positions(conn)
                self._scan_halt(conn)
        except sqlite3.Error:
            return

    def _scan_actions(self, conn: sqlite3.Connection) -> None:
        if self._last_action_id is None:
            return
        for r in conn.execute(
            "SELECT id, action_type, status, payload_json FROM actions "
            "WHERE id > ? ORDER BY id ASC LIMIT 50",
            (self._last_action_id,),
        ).fetchall():
            self._last_action_id = int(r["id"])
            ev = self._action_event(r)
            if ev is not None:
                self.event.emit(ev)

    def _action_event(self, row: sqlite3.Row) -> NotifEvent | None:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except json.JSONDecodeError:
            payload = {}
        action_type = str(row["action_type"])
        status = str(row["status"])
        if action_type == "OPEN" and status == "executed":
            side = str(payload.get("side", "?")).upper()
            entry = payload.get("entry_low", "?")
            sl = payload.get("sl", "?")
            return NotifEvent(
                kind="open_executed",
                title="OPEN executed",
                body=f"{side}  @  {entry}  ·  SL {sl}",
                severity="info",
            )
        if action_type == "ALERT":
            level = str(payload.get("level", "info")).lower()
            text = str(payload.get("text", ""))[:160]
            severity = "warning" if level in ("warning", "caution") else (
                "error" if level in ("error", "critical") else "info"
            )
            return NotifEvent(
                kind="alert",
                title=f"ALERT ({level})",
                body=text or "(no detail)",
                severity=severity,
            )
        if action_type == "CLOSE_PARTIAL" and status == "executed":
            frac = payload.get("fraction", 0.5)
            return NotifEvent(
                kind="partial",
                title="Partial close",
                body=f"closed {float(frac) * 100:.0f}% of position",
                severity="info",
            )
        if action_type == "CLOSE_FULL" and status == "executed":
            return NotifEvent(
                kind="close_full",
                title="Position closed (full)",
                body="channel issued CLOSE_FULL",
                severity="info",
            )
        return None

    def _scan_closed_positions(self, conn: sqlite3.Connection) -> None:
        if self._last_position_closed_id is None:
            return
        for r in conn.execute(
            "SELECT id, mt5_ticket, side, realized_pnl, close_reason FROM positions "
            "WHERE status='closed' AND id > ? ORDER BY id ASC LIMIT 50",
            (self._last_position_closed_id,),
        ).fetchall():
            self._last_position_closed_id = int(r["id"])
            pnl = float(r["realized_pnl"] or 0)
            severity = "info" if pnl >= 0 else "warning"
            self.event.emit(NotifEvent(
                kind="position_closed",
                title="Position closed",
                body=f"#{r['mt5_ticket']}  {r['side']}  ${pnl:+.2f}  ·  {r['close_reason'] or 'no reason'}",
                severity=severity,
            ))

    def _scan_halt(self, conn: sqlite3.Connection) -> None:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='kill_switch'"
        ).fetchone()
        new = str(row["value"]) if row else "off"
        if self._last_halt_state is None:
            self._last_halt_state = new
            return
        if new == self._last_halt_state:
            return
        if new == "on":
            reason_row = conn.execute(
                "SELECT value FROM settings WHERE key='kill_switch_reason'"
            ).fetchone()
            reason = str(reason_row["value"]) if reason_row else "manual"
            self.event.emit(NotifEvent(
                kind="halt",
                title="Trading HALTED",
                body=f"reason: {reason}",
                severity="error",
            ))
        else:
            self.event.emit(NotifEvent(
                kind="resume",
                title="Trading resumed",
                body="kill_switch=off",
                severity="info",
            ))
        self._last_halt_state = new
