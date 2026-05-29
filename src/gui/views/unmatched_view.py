"""Operator queue for Telegram messages the trigger matcher missed.

Populated by `src.unmatched_store` on the live Sonnet-fallback path. Each
row is one message that slipped past both deterministic and embedding
trigger layers but produced a Sonnet emission of a deterministic-
emittable action type — i.e. a real candidate for a future trigger.

Two terminal actions per row:

  - **Promote** opens the existing Add Trigger dialog pre-filled with
    Sonnet's inferred action_type and the full message as `samples[0]`.
    On accept, the trigger is appended to the profile, the profile is
    saved, and the row is deleted from the queue. A `trigger_promoted`
    signal lets the parent reload the Triggers tab so the new entry
    appears without a tab switch.
  - **Dismiss** just deletes the row.

Both terminal paths remove the row entirely (the queue is pending-only
by design). Polling is timer-driven because SQLite has no change-
notification and the listener writes from a different process.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src import unmatched_store
from src.gui.services import profile_io
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


_REFRESH_MS = 15_000


class UnmatchedView(QWidget):
    """Pane listing queued unmatched messages with promote/dismiss controls.

    Post-v2 fix: ``get_profile_name`` callback ties promote-to-trigger
    writes to the ProfileView's picker so the operator promotes INTO
    the right profile. Falls back to ``stack.name`` when no callback
    is given (v1 / single-channel).
    """

    trigger_promoted = Signal()

    def __init__(
        self, stack: Stack, parent: QWidget | None = None,
        get_profile_name=None,
    ) -> None:
        super().__init__(parent)
        self._stack = stack
        self._get_profile_name = get_profile_name
        self._build_ui()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def _active_profile_name(self) -> str:
        if self._get_profile_name is not None:
            picked = self._get_profile_name()
            if picked:
                return picked
        return self._stack.name

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("<b>Unmatched messages</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        self._count_label = QLabel("(0)")
        self._count_label.setStyleSheet(f"color: {current_palette().text_muted};")
        header.addWidget(self._count_label)
        header.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload unmatched")
        self._refresh_btn.clicked.connect(self._refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        _tm = current_palette().text_muted
        hint = QLabel(
            f"<span style='color:{_tm};'>Messages the trigger matcher "
            "missed but the AI classified as a deterministic-emittable "
            "action. Promote a row to add it as a real trigger, or "
            "dismiss to drop it from the queue.</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._table = QTableWidget(0, 5)
        self._table.setHorizontalHeaderLabels(
            ["When", "Suggested type", "Message", "", ""]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        self._table.setWordWrap(True)
        layout.addWidget(self._table, 1)

    def _db_connect(self) -> sqlite3.Connection | None:
        db_path: Path = self._stack.db_path
        if not db_path.exists():
            return None
        try:
            conn = sqlite3.connect(str(db_path), isolation_level=None)
            conn.row_factory = sqlite3.Row
            return conn
        except sqlite3.Error:
            return None

    def _refresh(self) -> None:
        conn = self._db_connect()
        if conn is None:
            self._populate([])
            return
        try:
            rows = unmatched_store.list_pending(conn)
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        self._populate(rows)

    def _populate(self, rows: list[unmatched_store.UnmatchedRow]) -> None:
        self._table.setRowCount(0)
        for r in rows:
            row_idx = self._table.rowCount()
            self._table.insertRow(row_idx)
            when_item = QTableWidgetItem(self._format_time(r.created_at))
            when_item.setData(Qt.ItemDataRole.UserRole, r.id)
            type_item = QTableWidgetItem(r.suggested_action_type or "?")
            msg_item = QTableWidgetItem(r.text)
            msg_item.setToolTip(r.text)
            self._table.setItem(row_idx, 0, when_item)
            self._table.setItem(row_idx, 1, type_item)
            self._table.setItem(row_idx, 2, msg_item)
            from src.gui._button_helpers import make_icon_button
            promote_btn = make_icon_button(
                "ACCEPT", "Promote this message into an action",
                variant="success", fallback_text="Promote",
            )
            promote_btn.clicked.connect(
                lambda _checked=False, rid=r.id: self._on_promote(rid)
            )
            dismiss_btn = make_icon_button(
                "CLOSE", "Dismiss — never replay this message",
                variant="danger", fallback_text="Dismiss",
            )
            dismiss_btn.clicked.connect(
                lambda _checked=False, rid=r.id: self._on_dismiss(rid)
            )
            self._table.setCellWidget(row_idx, 3, promote_btn)
            self._table.setCellWidget(row_idx, 4, dismiss_btn)
        self._table.resizeRowsToContents()
        self._count_label.setText(f"({len(rows)})")

    @staticmethod
    def _format_time(created_at: str) -> str:
        if " " in created_at:
            _, _, time_part = created_at.partition(" ")
            return time_part
        return created_at

    def _on_promote(self, row_id: int) -> None:
        conn = self._db_connect()
        if conn is None:
            QMessageBox.warning(
                self, "DB unavailable",
                "Channel DB is not present. Start the listener once to "
                "initialize it.",
            )
            return
        try:
            row = self._fetch_row(conn, row_id)
            if row is None:
                self._refresh()
                return
            from src.gui.views.triggers_view import _AddTriggerDialog
            dlg = _AddTriggerDialog(
                row["suggested_action_type"] or "CLOSE_PARTIAL",
                self,
                default_sample=row["text"],
                default_phrase=row["text"],
            )
            if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result:
                return
            try:
                self._append_to_profile(dlg.result)
            except OSError as e:
                QMessageBox.critical(self, "Save failed", str(e))
                return
            unmatched_store.delete(conn, row_id)
        finally:
            conn.close()
        self._refresh()
        self.trigger_promoted.emit()
        QMessageBox.information(
            self, "Promoted",
            "Trigger added to the profile and saved. "
            "Use PROFILE > Reload Listener to apply.",
        )

    def _on_dismiss(self, row_id: int) -> None:
        conn = self._db_connect()
        if conn is None:
            return
        try:
            unmatched_store.delete(conn, row_id)
        finally:
            conn.close()
        self._refresh()

    @staticmethod
    def _fetch_row(conn: sqlite3.Connection, row_id: int) -> sqlite3.Row | None:
        return conn.execute(
            "SELECT text, suggested_action_type FROM unmatched_messages "
            "WHERE id=?",
            (row_id,),
        ).fetchone()

    def _append_to_profile(self, trigger: dict) -> None:
        active = self._active_profile_name()
        data = profile_io.load_profile(active)
        triggers = list(profile_io.load_triggers(data))
        triggers.append(trigger)
        data["triggers"] = triggers
        profile_io.save_profile(active, data)
