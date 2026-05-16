"""QAbstractTableModel for the POSITIONS panel.

Holds positions + the latest market_XAUUSD_{bid,ask,at} from settings, and
computes unrealized PnL locally per row. PnL recomputes on either input change.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor


COL_TICKET = 0
COL_SIDE = 1
COL_LOTS = 2
COL_ENTRY = 3
COL_SL = 4
COL_TP = 5
COL_CURRENT = 6
COL_PNL = 7
COL_AGE = 8
HEADERS = ("Ticket", "Side", "Lots", "Entry", "SL", "TP", "Current", "PnL", "Age")

# XAUUSD: 1 lot = 100 oz; price quoted in USD/oz. PnL = delta * volume * 100.
_CONTRACT_SIZE = 100.0

_GREEN = QColor("#26a69a")
_RED = QColor("#ef5350")
_MUTED = QColor("#787b86")


@dataclass(frozen=True)
class PositionRow:
    ticket: int
    side: str
    volume: float
    original_volume: float
    entry_price: float
    sl: float | None
    tp: float | None
    opened_at: str
    partial_close_count: int
    sl_moved: bool


def _parse(row: sqlite3.Row) -> PositionRow:
    return PositionRow(
        ticket=int(row["mt5_ticket"]),
        side=str(row["side"]).upper(),
        volume=float(row["volume"] or 0),
        original_volume=float(row["original_volume"] or row["volume"] or 0),
        entry_price=float(row["entry_price"] or 0),
        sl=float(row["sl"]) if row["sl"] is not None else None,
        tp=float(row["tp"]) if row["tp"] is not None else None,
        opened_at=str(row["opened_at"]),
        partial_close_count=int(row["partial_close_count"] or 0),
        sl_moved=row["sl_moved_at"] is not None,
    )


def _age(opened_at: str) -> str:
    try:
        s = opened_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    secs = int((datetime.now(timezone.utc) - dt).total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


def _pnl(row: PositionRow, bid: float | None, ask: float | None) -> float | None:
    if row.side == "BUY":
        if bid is None:
            return None
        return (bid - row.entry_price) * row.volume * _CONTRACT_SIZE
    if row.side == "SELL":
        if ask is None:
            return None
        return (row.entry_price - ask) * row.volume * _CONTRACT_SIZE
    return None


def _exit_price(row: PositionRow, bid: float | None, ask: float | None) -> float | None:
    return bid if row.side == "BUY" else ask if row.side == "SELL" else None


class PositionsModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[PositionRow] = []
        self._bid: float | None = None
        self._ask: float | None = None
        self._market_at: str | None = None
        self._market_stale = True

    def set_rows(self, raw_rows: list[sqlite3.Row]) -> None:
        self.beginResetModel()
        self._rows = [_parse(r) for r in raw_rows]
        self.endResetModel()

    def set_market(self, settings_rows: list[sqlite3.Row]) -> None:
        bid = ask = None
        at = None
        for r in settings_rows:
            k = r["key"]
            v = r["value"]
            if k == "market_XAUUSD_bid":
                bid = float(v)
            elif k == "market_XAUUSD_ask":
                ask = float(v)
            elif k == "market_XAUUSD_at":
                at = v
        self._bid = bid
        self._ask = ask
        self._market_at = at
        self._market_stale = self._is_stale(at)
        if self._rows:
            top = self.index(0, COL_CURRENT)
            bottom = self.index(len(self._rows) - 1, COL_PNL)
            self.dataChanged.emit(
                top, bottom, [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole]
            )

    def refresh_ages(self) -> None:
        if not self._rows:
            return
        top = self.index(0, COL_AGE)
        bottom = self.index(len(self._rows) - 1, COL_AGE)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole])

    def market_stale(self) -> bool:
        return self._market_stale

    def market_caption(self) -> str:
        if self._bid is None or self._ask is None:
            return "no market price"
        tag = "  STALE" if self._market_stale else ""
        return f"bid {self._bid:.2f}  /  ask {self._ask:.2f}{tag}"

    def _is_stale(self, at: str | None, max_age_sec: int = 60) -> bool:
        if not at:
            return True
        try:
            s = at.replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return True
        return (datetime.now(timezone.utc) - dt).total_seconds() > max_age_sec

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
            if col == COL_TICKET:
                return str(row.ticket)
            if col == COL_SIDE:
                return row.side
            if col == COL_LOTS:
                if row.volume == row.original_volume:
                    return f"{row.volume:.2f}"
                return f"{row.volume:.2f}  /  {row.original_volume:.2f}"
            if col == COL_ENTRY:
                return f"{row.entry_price:.2f}"
            if col == COL_SL:
                return f"{row.sl:.2f}" if row.sl is not None else "—"
            if col == COL_TP:
                return f"{row.tp:.2f}" if row.tp is not None else "—"
            if col == COL_CURRENT:
                px = _exit_price(row, self._bid, self._ask)
                return f"{px:.2f}" if px is not None else "—"
            if col == COL_PNL:
                pnl = _pnl(row, self._bid, self._ask)
                return f"${pnl:+.2f}" if pnl is not None else "—"
            if col == COL_AGE:
                return _age(row.opened_at)
        if role == Qt.ItemDataRole.ForegroundRole and col == COL_PNL:
            pnl = _pnl(row, self._bid, self._ask)
            if pnl is None or self._market_stale:
                return _MUTED
            return _GREEN if pnl >= 0 else _RED
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (
            COL_TICKET, COL_LOTS, COL_ENTRY, COL_SL, COL_TP, COL_CURRENT, COL_PNL
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == COL_LOTS and row.partial_close_count:
            return f"partial closes: {row.partial_close_count}"
        return None
