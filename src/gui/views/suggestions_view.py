"""Operator review queue for channel-learning rule proposals.

Each row is a Suggestion produced by the learning pipeline. The operator can:
  - Accept: writes the rule into profile.json and marks the suggestion accepted.
  - Dismiss: marks the suggestion dismissed without writing any rule.

Rows where evidence["one_tap_safe"] is False show a red warning and require a
confirmation dialog before the Accept action proceeds.

Polling is timer-driven (same pattern as UnmatchedView) because SQLite has no
change-notification and the learning pipeline writes from a different process.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,
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

from src import suggestion_store
from src.gui.services.stack_registry import Stack
from src.gui.views.suggestions_logic import (
    accept_suggestion,
    dismiss_suggestion,
    rank_suggestions,
    resolve_channel_profile_path,
)


_REFRESH_MS = 15_000

# Evidence field summaries per rule kind.
_KIND_LABELS = {
    "noise": "noise",
    "context_drop": "context_drop",
    "keep_trigger": "keep_trigger",
    "action_trigger": "action_trigger",
}


def _evidence_summary(s: suggestion_store.Suggestion) -> str:
    ev = s.evidence
    kind = s.rule_kind
    if kind in ("noise", "context_drop"):
        parts = []
        ws = ev.get("would_suppress")
        if ws is not None:
            parts.append(f"would_suppress={ws}")
        fsc = ev.get("false_suppression_count")
        if fsc is not None:
            parts.append(f"false_suppress={fsc}")
        vb = ev.get("verdict_breakdown")
        if vb:
            parts.append(f"breakdown={vb}")
        return "  ".join(parts) if parts else ""
    if kind == "action_trigger":
        parts = []
        sup = ev.get("support")
        if sup is not None:
            parts.append(f"support={sup}")
        pur = ev.get("purity")
        if pur is not None:
            parts.append(f"purity={pur}")
        conf = ev.get("conflicts")
        if conf is not None:
            parts.append(f"conflicts={conf}")
        return "  ".join(parts) if parts else ""
    if kind == "keep_trigger":
        tfn = ev.get("triage_false_negatives")
        if tfn is not None:
            return f"triage_false_negatives={tfn}"
        return ""
    return ""


class SuggestionsView(QWidget):
    """Pane listing proposed learning-loop suggestions with accept/dismiss controls."""

    def __init__(self, stack: Stack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stack = stack
        self._build_ui()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def rebind(self, stack: Stack) -> None:
        """Called by MainWindow when the header switcher changes stack."""
        self._stack = stack
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel("<b>Learning suggestions</b>")
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        self._count_label = QLabel("(0)")
        self._count_label.setStyleSheet("color: #787b86;")
        header.addWidget(self._count_label)
        header.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload suggestions")
        self._refresh_btn.clicked.connect(self._refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        hint = QLabel(
            "<span style='color:#787b86;'>Rules proposed by the channel learning "
            "loop. Accept to write the rule into profile.json, or Dismiss to skip. "
            "Rows with a <b style='color:#c0392b;'>red warning</b> require "
            "confirmation — the evidence suggests a non-trivial suppression "
            "risk.</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._table = QTableWidget(0, 6)
        self._table.setHorizontalHeaderLabels(
            ["Kind", "Phrase", "Evidence", "Safe?", "", ""]
        )
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(5, QHeaderView.ResizeMode.ResizeToContents)
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
            rows = suggestion_store.list_proposed(conn, None)
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        self._populate(rank_suggestions(rows))

    def _populate(self, suggestions: list[suggestion_store.Suggestion]) -> None:
        self._table.setRowCount(0)
        for s in suggestions:
            row_idx = self._table.rowCount()
            self._table.insertRow(row_idx)

            kind_item = QTableWidgetItem(_KIND_LABELS.get(s.rule_kind, s.rule_kind))
            kind_item.setData(Qt.ItemDataRole.UserRole, s.id)
            self._table.setItem(row_idx, 0, kind_item)

            phrase = str(s.payload.get("phrase") or "")
            phrase_item = QTableWidgetItem(phrase)
            phrase_item.setToolTip(phrase)
            self._table.setItem(row_idx, 1, phrase_item)

            ev_text = _evidence_summary(s)
            ev_item = QTableWidgetItem(ev_text)
            ev_item.setToolTip(ev_text)
            self._table.setItem(row_idx, 2, ev_item)

            is_safe = bool(s.evidence.get("one_tap_safe"))
            if is_safe:
                safe_item = QTableWidgetItem("yes")
                safe_item.setForeground(Qt.GlobalColor.darkGreen)
            else:
                safe_item = QTableWidgetItem("WARNING")
                safe_item.setForeground(Qt.GlobalColor.red)
                safe_item.setToolTip(
                    "Evidence suggests suppression risk — confirm before accepting."
                )
            self._table.setItem(row_idx, 3, safe_item)

            from src.gui._button_helpers import make_icon_button
            accept_btn = make_icon_button(
                "ACCEPT", "Accept — write this rule into profile.json",
                variant="success", fallback_text="Accept",
            )
            accept_btn.clicked.connect(
                lambda _checked=False, sid=s.id, safe=is_safe: self._on_accept(sid, safe)
            )
            self._table.setCellWidget(row_idx, 4, accept_btn)

            dismiss_btn = make_icon_button(
                "CLOSE", "Dismiss — skip this suggestion",
                variant="danger", fallback_text="Dismiss",
            )
            dismiss_btn.clicked.connect(
                lambda _checked=False, sid=s.id: self._on_dismiss(sid)
            )
            self._table.setCellWidget(row_idx, 5, dismiss_btn)

        self._table.resizeRowsToContents()
        self._count_label.setText(f"({len(suggestions)})")

    def _on_accept(self, sid: int, is_safe: bool) -> None:
        if not is_safe:
            reply = QMessageBox.question(
                self,
                "Confirm accept",
                "This suggestion is not marked one-tap-safe.\n"
                "The evidence suggests a non-trivial suppression risk.\n\n"
                "Accept anyway and write the rule into profile.json?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if reply != QMessageBox.StandardButton.Yes:
                return

        conn = self._db_connect()
        if conn is None:
            QMessageBox.warning(
                self, "DB unavailable",
                "Channel DB is not present. Start the listener once to initialize it.",
            )
            return
        try:
            s = suggestion_store.get(conn, sid)
            if s is None:
                QMessageBox.warning(
                    self, "Not found",
                    "This suggestion no longer exists (already decided?).",
                )
                return
            # Resolve the LIVE profile this suggestion's channel actually uses
            # (channel -> route -> destination -> db-adjacent profile.json),
            # with the active stack as fallback. Not the legacy registry path.
            profile_path = resolve_channel_profile_path(
                s.source_channel_id,
                fallback_db_path=self._stack.db_path,
                fallback_legacy=self._stack.profile_path,
            )
            if not profile_path.exists():
                QMessageBox.warning(
                    self, "Profile not found",
                    f"profile.json not found at:\n{profile_path}\n\n"
                    "Create or load a profile first.",
                )
                return
            accept_suggestion(conn, sid, profile_path=profile_path)
        except Exception as exc:
            QMessageBox.critical(self, "Accept failed", str(exc))
            return
        finally:
            conn.close()
        self._refresh()
        QMessageBox.information(
            self, "Accepted",
            "Rule written to profile.json. "
            "Use PROFILE > Reload Listener to apply.",
        )

    def _on_dismiss(self, sid: int) -> None:
        conn = self._db_connect()
        if conn is None:
            return
        try:
            dismiss_suggestion(conn, sid)
        finally:
            conn.close()
        self._refresh()
