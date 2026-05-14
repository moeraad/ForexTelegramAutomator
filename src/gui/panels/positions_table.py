"""POSITIONS panel: open positions with live unrealized PnL."""
from __future__ import annotations

import sqlite3

from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.positions_model import PositionsModel
from src.gui.services.db_subscriber import DBSubscriber


class PositionsTable(QWidget):
    SUB_POSITIONS = "positions_open"
    SUB_MARKET = "positions_market"
    SQL_POSITIONS = (
        "SELECT mt5_ticket, side, volume, original_volume, entry_price, "
        "sl, tp, opened_at, partial_close_count, sl_moved_at "
        "FROM positions WHERE status='open' ORDER BY opened_at DESC"
    )
    SQL_MARKET = (
        "SELECT key, value FROM settings WHERE key LIKE 'market_XAUUSD_%'"
    )

    def __init__(self, subscriber: DBSubscriber) -> None:
        super().__init__()
        self._subscriber = subscriber
        self._model = PositionsModel()
        self._build_ui()
        subscriber.subscribe(self.SUB_POSITIONS, self.SQL_POSITIONS)
        subscriber.subscribe(self.SUB_MARKET, self.SQL_MARKET)
        subscriber.query_changed.connect(self._on_query_changed)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        self._title = QLabel("POSITIONS  (0 open)")
        header.addWidget(self._title)
        header.addStretch()
        self._market_lbl = QLabel("no market price")
        self._market_lbl.setStyleSheet("color: #586e75;")
        header.addWidget(self._market_lbl)
        layout.addLayout(header)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        layout.addWidget(self._table, 1)

    def _on_query_changed(self, key: str, rows: list[sqlite3.Row]) -> None:
        if key == self.SUB_POSITIONS:
            self._model.set_rows(rows)
            self._title.setText(f"POSITIONS  ({len(rows)} open)")
        elif key == self.SUB_MARKET:
            self._model.set_market(rows)
            self._market_lbl.setText(self._model.market_caption())
            css = (
                "color: #b58900;" if self._model.market_stale() else "color: #586e75;"
            )
            self._market_lbl.setStyleSheet(css)

    def refresh_ages(self) -> None:
        self._model.refresh_ages()
