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
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.actions_model import ActionRow, ActionsModel, COL_TYPE
from src.gui.services.db_subscriber import DBSubscriber


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

    def __init__(self, subscriber: DBSubscriber) -> None:
        super().__init__()
        self._subscriber = subscriber
        self._model = ActionsModel()
        self._proxy = _ActionsProxyModel()
        self._proxy.setSourceModel(self._model)
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
            self.selection_changed.emit(None)
            return
        source_index = self._proxy.mapToSource(current)
        action = self._model.action_at(source_index.row())
        self.selection_changed.emit(action)

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
