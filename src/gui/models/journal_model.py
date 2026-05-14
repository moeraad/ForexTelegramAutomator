"""QAbstractTableModel for closed-trade journal rows."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor

from src.gui.services.journal_data import TradeRow


COL_CLOSED = 0
COL_TICKET = 1
COL_SIDE = 2
COL_LOTS = 3
COL_ENTRY = 4
COL_EXIT = 5
COL_PNL = 6
COL_DURATION = 7
COL_REASON = 8
HEADERS = ("Closed", "Ticket", "Side", "Lots", "Entry", "Exit", "PnL", "Duration", "Reason")

_GREEN = QColor("#859900")
_RED = QColor("#dc322f")


def _duration(opened_at: str, closed_at: str) -> str:
    try:
        o = datetime.fromisoformat(opened_at.replace("Z", "+00:00"))
        c = datetime.fromisoformat(closed_at.replace("Z", "+00:00"))
        if o.tzinfo is None:
            o = o.replace(tzinfo=timezone.utc)
        if c.tzinfo is None:
            c = c.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    secs = int((c - o).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


class JournalModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[TradeRow] = []

    def set_rows(self, rows: list[TradeRow]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rows(self) -> list[TradeRow]:
        return list(self._rows)

    def trade_at(self, row: int) -> TradeRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(HEADERS)

    def headerData(
        self,
        section: int,
        orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        if orientation == Qt.Orientation.Horizontal:
            return HEADERS[section]
        return section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == COL_CLOSED:
                return row.closed_at[:19].replace("T", " ")
            if col == COL_TICKET:
                return str(row.ticket)
            if col == COL_SIDE:
                return row.side
            if col == COL_LOTS:
                if row.volume == row.original_volume:
                    return f"{row.volume:.2f}"
                return f"{row.volume:.2f}/{row.original_volume:.2f}"
            if col == COL_ENTRY:
                return f"{row.entry_price:.2f}"
            if col == COL_EXIT:
                return f"{row.exit_price:.2f}"
            if col == COL_PNL:
                return f"${row.realized_pnl:+.2f}"
            if col == COL_DURATION:
                return _duration(row.opened_at, row.closed_at)
            if col == COL_REASON:
                return row.close_reason or "—"
        if role == Qt.ItemDataRole.ForegroundRole and col == COL_PNL:
            return _GREEN if row.realized_pnl >= 0 else _RED
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (
            COL_TICKET, COL_LOTS, COL_ENTRY, COL_EXIT, COL_PNL
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None
