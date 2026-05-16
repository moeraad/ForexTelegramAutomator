"""ACTIONS panel: filterable, searchable, keyboard-navigable live table."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.actions_model import ActionRow, ActionsModel, COL_TYPE
from src.gui.services.db_subscriber import DBSubscriber
from src.gui.services.stack_registry import Stack


_FILTERS: list[tuple[str, str]] = [
    ("all", "All"),
    ("open", "OPEN only"),
    ("management", "Management"),
    ("rejected", "Rejected"),
    ("watching", "Watching"),
]

_MANAGEMENT_TYPES = {
    "MOVE_SL_BE",
    "MOVE_SL",
    "CLOSE_PARTIAL",
    "CLOSE_FULL",
    "REOPEN_LAST",
    "REINFORCE",
    "TIGHTEN_SL",
    "MODIFY_TPS",
    "CANCEL_PENDING",
}


class _ActionsProxyModel(QSortFilterProxyModel):
    def __init__(self) -> None:
        super().__init__()
        self._filter_key = "all"
        self._search_text = ""

    def set_filter(self, key: str) -> None:
        self._filter_key = key
        self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._search_text = text.lower().strip()
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, _parent) -> bool:  # type: ignore[override]
        model = self.sourceModel()
        if not isinstance(model, ActionsModel):
            return True
        action = model.action_at(source_row)
        if action is None:
            return False
        if not self._matches_filter(action):
            return False
        if self._search_text:
            hay = (action.action_type + " " + action.type_display + " " + action.status).lower()
            return self._search_text in hay
        return True

    def _matches_filter(self, action: ActionRow) -> bool:
        if self._filter_key == "all":
            return True
        if self._filter_key == "open":
            return action.action_type == "OPEN"
        if self._filter_key == "management":
            return action.action_type in _MANAGEMENT_TYPES
        if self._filter_key == "rejected":
            return action.status == "rejected"
        if self._filter_key == "watching":
            return action.status == "watching"
        return True


class ActionsTable(QWidget):
    selection_changed = Signal(object)

    SUBSCRIPTION_KEY = "actions_recent"
    SQL = (
        "SELECT id, action_type, status, created_at, payload_json "
        "FROM actions ORDER BY id DESC LIMIT 50"
    )

    def __init__(self, subscriber: DBSubscriber, stack: Stack | None = None) -> None:
        super().__init__()
        self._subscriber = subscriber
        self._stack = stack
        self._model = ActionsModel()
        self._proxy = _ActionsProxyModel()
        self._proxy.setSourceModel(self._model)
        self._current_action: ActionRow | None = None
        self._build_ui()
        subscriber.subscribe(self.SUBSCRIPTION_KEY, self.SQL)
        subscriber.query_changed.connect(self._on_query_changed)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel("ACTIONS"))
        header.addStretch()
        self._filter = QComboBox()
        for key, label in _FILTERS:
            self._filter.addItem(label, key)
        self._filter.currentIndexChanged.connect(
            lambda _i: self._proxy.set_filter(self._filter.currentData())
        )
        header.addWidget(QLabel("Filter:"))
        header.addWidget(self._filter)

        self._search = QLineEdit()
        self._search.setPlaceholderText("search type / status")
        self._search.setMaximumWidth(220)
        self._search.textChanged.connect(self._proxy.set_search)
        header.addWidget(self._search)
        # Per-row operator controls (REVIEW.md Q4). Promote-now flips a
        # pending row straight to 'sent' so the EA picks it up on its
        # next poll instead of waiting for the configured grace delay.
        # Cancel marks a pending row 'cancelled'. Both are disabled
        # until a 'pending' row is selected.
        self._promote_btn = QPushButton("Promote now")
        self._promote_btn.setToolTip(
            "Force-trigger the selected pending action (skips the auto-execute "
            "delay; EA picks it up on the next poll)."
        )
        self._promote_btn.setEnabled(False)
        self._promote_btn.clicked.connect(self._on_promote_clicked)
        header.addWidget(self._promote_btn)
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setToolTip(
            "Cancel the selected pending action before it auto-promotes."
        )
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        header.addWidget(self._cancel_btn)
        layout.addLayout(header)

        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        ncols = self._model.columnCount()
        content_cols = tuple(c for c in range(ncols) if c != COL_TYPE)
        apply_full_width_headers(
            self._table, content_columns=content_cols, stretch_column=COL_TYPE,
        )
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.selectionModel().currentRowChanged.connect(self._on_selection)
        layout.addWidget(self._table, 1)

    def _on_query_changed(self, key: str, rows: list[sqlite3.Row]) -> None:
        if key != self.SUBSCRIPTION_KEY:
            return
        self._model.set_rows(rows)
        if self._proxy.rowCount() > 0 and not self._table.selectionModel().hasSelection():
            self._table.selectRow(0)

    def _on_selection(self, current, _previous) -> None:
        if not current.isValid():
            self._current_action = None
            self._update_button_state()
            self.selection_changed.emit(None)
            return
        source_index = self._proxy.mapToSource(current)
        action = self._model.action_at(source_index.row())
        self._current_action = action
        self._update_button_state()
        self.selection_changed.emit(action)

    def _update_button_state(self) -> None:
        # Only pending rows can be promoted or cancelled. Sent/claimed are
        # mid-flight with the EA; watching is owned by the broker pending
        # order (see CLAUDE.md "Synthetic pending"). Disabling here mirrors
        # what _do_cancel / _do_execute enforce server-side in bot.py.
        action = self._current_action
        is_pending = (
            self._stack is not None
            and action is not None
            and action.status == "pending"
        )
        self._promote_btn.setEnabled(bool(is_pending))
        self._cancel_btn.setEnabled(bool(is_pending))

    def _on_promote_clicked(self) -> None:
        action = self._current_action
        if action is None or self._stack is None:
            return
        if action.status != "pending":
            QMessageBox.information(
                self, "Promote",
                f"Action #{action.id} is {action.status}; only pending "
                "actions can be promoted.",
            )
            return
        self._apply_action_status_change(
            action.id,
            "UPDATE actions SET status='sent' WHERE id=? AND status='pending'",
            success_msg=f"Promoted action #{action.id} to sent.",
            noop_msg=f"Action #{action.id} is no longer pending.",
        )

    def _on_cancel_clicked(self) -> None:
        action = self._current_action
        if action is None or self._stack is None:
            return
        if action.status != "pending":
            QMessageBox.information(
                self, "Cancel",
                f"Action #{action.id} is {action.status}; only pending "
                "actions can be cancelled.",
            )
            return
        self._apply_action_status_change(
            action.id,
            "UPDATE actions SET status='cancelled' WHERE id=? AND status='pending'",
            success_msg=f"Cancelled action #{action.id}.",
            noop_msg=f"Action #{action.id} is no longer pending.",
        )

    def _apply_action_status_change(
        self, action_id: int, sql: str, *, success_msg: str, noop_msg: str,
    ) -> None:
        assert self._stack is not None
        try:
            conn = sqlite3.connect(str(self._stack.db_path))
            try:
                cur = conn.execute(sql, (action_id,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            QMessageBox.warning(self, "DB error", f"Action #{action_id}: {e}")
            return
        QMessageBox.information(
            self, "OK", success_msg if cur.rowcount else noop_msg,
        )

    def refresh_ages(self) -> None:
        self._model.refresh_ages()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_J:
            self._move_selection(+1)
            return
        if key == Qt.Key.Key_K:
            self._move_selection(-1)
            return
        super().keyPressEvent(event)

    def _move_selection(self, delta: int) -> None:
        if self._proxy.rowCount() == 0:
            return
        current_idx = self._table.currentIndex()
        new_row = (current_idx.row() if current_idx.isValid() else -1) + delta
        new_row = max(0, min(self._proxy.rowCount() - 1, new_row))
        self._table.selectRow(new_row)
