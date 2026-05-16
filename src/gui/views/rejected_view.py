"""REJECTED view: prompt-drift detector.

Surfaces all actions in status='rejected', grouped by ea_response category.
Spikes in 'already_open' / 'no_open_position' / 'cancelled_by_channel' are
strong signals that the AI prompt's idempotency rules are misfiring.
"""
from __future__ import annotations

import json
import sqlite3
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.stack_registry import Stack


_RANGES: list[tuple[str, int | None]] = [
    ("Today", 0),
    ("Last 3 days", 3),
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("All time", None),
]


@dataclass(frozen=True)
class _Rejected:
    id: int
    action_type: str
    reason: str
    reason_category: str
    created_at: str
    source_msg_id: int | None
    source_text: str
    payload_comment: str


def _normalize_reason(raw: str | None) -> tuple[str, str]:
    if not raw:
        return "(unknown)", "(unknown)"
    raw = raw.strip()
    category = raw.split(":", 1)[0].strip() if ":" in raw else raw
    return category or "(unknown)", raw


def _since_iso(days: int | None) -> str | None:
    if days is None:
        return None
    if days == 0:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _load(db_path: Path, days: int | None) -> list[_Rejected]:
    if not db_path.exists():
        return []
    since = _since_iso(days)
    sql = (
        "SELECT a.id, a.action_type, a.ea_response, a.payload_json, "
        "a.created_at, a.source_msg_id, m.text AS source_text "
        "FROM actions a LEFT JOIN messages m ON m.id = a.source_msg_id "
        "WHERE a.status='rejected'"
    )
    params: tuple = ()
    if since is not None:
        sql += " AND a.created_at >= ?"
        params = (since,)
    sql += " ORDER BY a.id DESC"
    out: list[_Rejected] = []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        for r in conn.execute(sql, params).fetchall():
            cat, full = _normalize_reason(r["ea_response"])
            comment = ""
            payload = r["payload_json"] or ""
            if '"comment"' in payload:
                try:
                    p = json.loads(payload)
                    if isinstance(p, dict):
                        comment = str(p.get("comment", ""))
                except json.JSONDecodeError:
                    pass
            out.append(_Rejected(
                id=int(r["id"]),
                action_type=str(r["action_type"]),
                reason=full,
                reason_category=cat,
                created_at=str(r["created_at"] or ""),
                source_msg_id=int(r["source_msg_id"]) if r["source_msg_id"] is not None else None,
                source_text=str(r["source_text"] or ""),
                payload_comment=comment,
            ))
    return out


def _hex_to_accent(color: str) -> str:
    if not color:
        return ""
    c = color.lower()
    if c in ("#26a69a", "#859900", "#00e676", "#00897b"):
        return "success"
    if c in ("#ef5350", "#dc322f", "#ff5252", "#d32f2f"):
        return "danger"
    if c in ("#ff9800", "#b58900", "#ffd740", "#f57c00"):
        return "warning"
    if c in ("#2962ff", "#268bd2", "#448aff", "#1976d2"):
        return "accent"
    return ""


class _ReasonsModel(QAbstractTableModel):
    HEADERS = ("Reason", "Count", "%", "Last seen")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[tuple[str, int, float, str]] = []

    def set_from(self, rejected: list[_Rejected]) -> None:
        self.beginResetModel()
        counter = Counter(r.reason_category for r in rejected)
        last_seen: dict[str, str] = {}
        for r in rejected:
            if r.reason_category not in last_seen:
                last_seen[r.reason_category] = r.created_at
        total = max(1, len(rejected))
        rows: list[tuple[str, int, float, str]] = []
        for cat, count in counter.most_common():
            rows.append((cat, count, count / total * 100.0, last_seen.get(cat, "")))
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        cat, count, pct, last = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return cat
            if col == 1:
                return str(count)
            if col == 2:
                return f"{pct:.0f}%"
            if col == 3:
                return last[:19].replace("T", " ")
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (1, 2):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and col == 0:
            return QColor("#ef5350") if cat != "(unknown)" else QColor("#787b86")
        return None

    def category_at(self, row: int) -> str | None:
        if 0 <= row < len(self._rows):
            return self._rows[row][0]
        return None


class _RejectedListModel(QAbstractTableModel):
    HEADERS = ("ID", "Type", "Reason", "Source / comment", "Created")

    def __init__(self) -> None:
        super().__init__()
        self._all: list[_Rejected] = []
        self._filtered: list[_Rejected] = []
        self._filter_category: str | None = None

    def set_rows(self, rows: list[_Rejected]) -> None:
        self.beginResetModel()
        self._all = rows
        self._apply_filter()
        self.endResetModel()

    def filter_category(self, category: str | None) -> None:
        self.beginResetModel()
        self._filter_category = category
        self._apply_filter()
        self.endResetModel()

    def _apply_filter(self) -> None:
        if self._filter_category is None:
            self._filtered = list(self._all)
        else:
            self._filtered = [r for r in self._all if r.reason_category == self._filter_category]

    def rejected_at(self, row: int) -> _Rejected | None:
        if 0 <= row < len(self._filtered):
            return self._filtered[row]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._filtered)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        row = self._filtered[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return str(row.id)
            if col == 1:
                return row.action_type
            if col == 2:
                return row.reason
            if col == 3:
                src = row.source_text.strip().replace("\n", " ")
                if not src and row.payload_comment:
                    src = f"({row.payload_comment})"
                if len(src) > 80:
                    src = src[:77] + "…"
                return src
            if col == 4:
                return row.created_at[:19].replace("T", " ")
        if role == Qt.ItemDataRole.ToolTipRole and col == 3 and row.source_text:
            return row.source_text
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 0:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        # Arabic source-message column reads correctly only when
        # right-aligned (REVIEW.md §3 RTL).
        if role == Qt.ItemDataRole.TextAlignmentRole and col == 3:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and col == 2:
            return QColor("#ef5350")
        return None


class RejectedView(QWidget):
    # Emitted when the operator clicks "Replay" on a selected rejected
    # row. MainWindow listens and switches to ReplayView + preloads the
    # message (REVIEW.md §4.3).
    from PySide6.QtCore import Signal as _Signal
    replay_message_requested = _Signal(int)

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._days: int | None = 7
        self._stat_row_layout: QHBoxLayout | None = None
        self._stat_boxes: dict[str, QWidget] = {}
        self._reasons_model = _ReasonsModel()
        self._list_model = _RejectedListModel()
        self._build_ui()
        self.refresh()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self.refresh()

    def refresh(self) -> None:
        rows = _load(self._stack.db_path, self._days)
        self._list_model.set_rows(rows)
        self._reasons_model.set_from(rows)
        self._update_stats(rows)
        self._update_spike_banner(rows)

    def _update_spike_banner(self, rows: list[_Rejected]) -> None:
        """Show a banner when today's category counts spike vs yesterday."""
        today = datetime.now(timezone.utc).date()
        yesterday = today - timedelta(days=1)
        today_counts: Counter[str] = Counter()
        yest_counts: Counter[str] = Counter()
        for r in rows:
            try:
                d = datetime.fromisoformat(r.created_at).date()
            except ValueError:
                continue
            if d == today:
                today_counts[r.reason_category] += 1
            elif d == yesterday:
                yest_counts[r.reason_category] += 1
        spikes: list[tuple[str, int, int]] = []
        for cat, n_today in today_counts.items():
            n_yest = yest_counts.get(cat, 0)
            if n_today >= 5 and n_today >= 2 * max(1, n_yest):
                spikes.append((cat, n_today, n_yest))
        if not spikes:
            self._spike_banner.hide()
            return
        spikes.sort(key=lambda x: -x[1])
        parts = []
        for cat, nt, ny in spikes[:3]:
            parts.append(f"<b>{cat}</b> ×{nt} today (was {ny} yesterday)")
        self._spike_banner.setText(
            "⚠ Rejection spike — possible prompt drift: " + " · ".join(parts)
        )
        self._spike_banner.show()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>REJECTED</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        from src.gui.panels._a11y import mark_heading
        mark_heading(title, "Rejected")
        top.addWidget(title)
        hint = QLabel("<span style='color:#787b86;'>prompt-drift detector — investigate spikes</span>")
        hint.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(hint)
        top.addSpacing(16)
        top.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, days in _RANGES:
            self._range_combo.addItem(label, days)
        self._range_combo.setCurrentIndex(2)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        top.addWidget(self._range_combo)
        top.addStretch()
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        from src.gui.panels._stat_card import StatCard
        self._stat_row_layout = QHBoxLayout()
        self._stat_row_layout.setSpacing(8)
        for key in ("total", "categories", "top_reason", "topmost_recent"):
            card = StatCard(label=key, value="—")
            self._stat_boxes[key] = card
            self._stat_row_layout.addWidget(card)
        self._stat_row_layout.addStretch()
        layout.addLayout(self._stat_row_layout)

        self._spike_banner = QLabel("")
        self._spike_banner.setTextFormat(Qt.TextFormat.RichText)
        self._spike_banner.setStyleSheet(
            "QLabel { background: #ef5350; color: white; "
            "padding: 8px 12px; border-radius: 4px; font-weight: 600; }"
        )
        self._spike_banner.hide()
        layout.addWidget(self._spike_banner)

        body = QHBoxLayout()

        left = QVBoxLayout()
        # Mirror the right pane's header row height (QLabel + Replay
        # button) so the two tables start at the same Y. Otherwise the
        # right side's button pushes its table ~10 px down.
        left_header = QHBoxLayout()
        left_header.addWidget(QLabel("Reasons"))
        left_header.addStretch()
        spacer_btn = QPushButton("")
        spacer_btn.setEnabled(False)
        spacer_btn.setFlat(True)
        spacer_btn.setStyleSheet("QPushButton { background: transparent; border: none; }")
        spacer_btn.setFixedSize(0, 28)
        left_header.addWidget(spacer_btn)
        left.addLayout(left_header)
        self._reasons_table = QTableView()
        self._reasons_table.setModel(self._reasons_model)
        self._reasons_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._reasons_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._reasons_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._reasons_table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        apply_full_width_headers(
            self._reasons_table,
            content_columns=tuple(range(self._reasons_model.columnCount() - 1)),
        )
        self._reasons_table.setAlternatingRowColors(True)
        self._reasons_table.setShowGrid(False)
        self._reasons_table.setMinimumWidth(360)
        self._reasons_table.selectionModel().currentRowChanged.connect(self._on_reason_selected)
        left.addWidget(self._reasons_table)
        self._all_btn = QPushButton("Show all categories")
        self._all_btn.clicked.connect(lambda: self._list_model.filter_category(None))
        left.addWidget(self._all_btn)
        body.addLayout(left)

        right = QVBoxLayout()
        right_header = QHBoxLayout()
        right_header.addWidget(QLabel("Rejected actions"))
        right_header.addStretch()
        # Replay button — switches to ReplayView preloaded with the
        # selected row's source message (REVIEW.md §4.3). Enabled only
        # when a row with a source_msg_id is selected.
        self._replay_btn = QPushButton("Replay")
        self._replay_btn.setToolTip(
            "Re-run the selected rejected message through the current AI in "
            "the Replay view (no DB writes)."
        )
        self._replay_btn.setEnabled(False)
        self._replay_btn.clicked.connect(self._on_replay_clicked)
        right_header.addWidget(self._replay_btn)
        right.addLayout(right_header)
        self._list_table = QTableView()
        self._list_table.setModel(self._list_model)
        self._list_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._list_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._list_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._list_table.verticalHeader().setVisible(False)
        apply_full_width_headers(
            self._list_table,
            content_columns=tuple(range(self._list_model.columnCount() - 1)),
        )
        self._list_table.setAlternatingRowColors(True)
        self._list_table.setShowGrid(False)
        self._list_table.selectionModel().currentRowChanged.connect(
            self._on_rejected_row_changed
        )
        right.addWidget(self._list_table)
        body.addLayout(right, 1)

        layout.addLayout(body, 1)

    def _on_range_changed(self, _idx: int) -> None:
        self._days = self._range_combo.currentData()
        self.refresh()

    def _on_reason_selected(self, current, _previous) -> None:
        if not current.isValid():
            return
        category = self._reasons_model.category_at(current.row())
        self._list_model.filter_category(category)

    def _on_rejected_row_changed(self, current, _previous) -> None:
        """Toggle the Replay button based on the selected row's
        source_msg_id — rows without an originating message (e.g.
        EA-side ALERT inserts) can't be replayed."""
        if not current.isValid():
            self._replay_btn.setEnabled(False)
            return
        row = self._list_model.rejected_at(current.row())
        self._replay_btn.setEnabled(bool(row and row.source_msg_id))

    def _on_replay_clicked(self) -> None:
        idx = self._list_table.currentIndex()
        if not idx.isValid():
            return
        row = self._list_model.rejected_at(idx.row())
        if row is None or row.source_msg_id is None:
            return
        self.replay_message_requested.emit(int(row.source_msg_id))

    def _update_stats(self, rows: list[_Rejected]) -> None:
        counter = Counter(r.reason_category for r in rows)
        total = len(rows)
        categories = len(counter)
        top_reason, top_count = counter.most_common(1)[0] if counter else ("—", 0)
        most_recent = rows[0].created_at[:19].replace("T", " ") if rows else "—"

        replacements = (
            ("total", "Total rejected", str(total), "#ef5350" if total else "#d1d4dc"),
            ("categories", "Reason types", str(categories), "#d1d4dc"),
            ("top_reason", "Top reason", f"{top_reason}  ({top_count})", "#d1d4dc"),
            ("topmost_recent", "Most recent", most_recent, "#d1d4dc"),
        )
        for key, label, value, color in replacements:
            self._replace_box(key, label, value, color)

    def _replace_box(self, key: str, label: str, value: str, color: str) -> None:
        assert self._stat_row_layout is not None
        card = self._stat_boxes[key]
        card.set_value(value, _hex_to_accent(color))
        if hasattr(card, "_label") and label:
            card._label.setText(label.upper())
