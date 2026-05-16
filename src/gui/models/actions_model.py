"""QAbstractTableModel for the ACTIONS panel."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from pathlib import Path

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


COL_ID = 0
COL_TYPE = 1
COL_STATUS = 2
COL_AGE = 3
COL_QUALITY = 4
HEADERS = ("#", "Type", "Status", "Age", "Quality")


_STATUS_DOT = {
    "executed": "●",
    "watching": "◐",
    "rejected": "✕",
    # "pending" intentionally uses an SVG icon instead of a glyph — see
    # _hourglass_icon(). Display path skips the prefix for "pending".
    "sent": "▷",
    "claimed": "⇩",
    "failed": "✕",
}


_HOURGLASS_SVG = (
    Path(__file__).resolve().parent.parent / "resources" / "icons" / "hourglass.svg"
)
_hourglass_icon_cache: QIcon | None = None


def _hourglass_icon() -> QIcon:
    """Render the hourglass SVG once, recolored to the muted-text token
    of the active palette, and cache the QIcon."""
    global _hourglass_icon_cache
    if _hourglass_icon_cache is not None:
        return _hourglass_icon_cache
    try:
        from src.gui.theme import current_palette
        tint = QColor(current_palette().text_muted)
    except Exception:
        tint = QColor("#787b86")
    renderer = QSvgRenderer(str(_HOURGLASS_SVG))
    size = 16
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pix)
    renderer.render(painter)
    # Re-color: composite the tint over the painted alpha so the SVG's
    # currentColor (which falls back to black) becomes our muted text.
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
    painter.fillRect(pix.rect(), tint)
    painter.end()
    _hourglass_icon_cache = QIcon(pix)
    return _hourglass_icon_cache


def _invalidate_icon_cache() -> None:
    """Clear the icon cache so it re-renders against the new palette
    on theme toggle."""
    global _hourglass_icon_cache
    _hourglass_icon_cache = None


# Re-build the cached icon whenever the theme flips.
try:
    from src.gui.theme import bus as _theme_bus
    _theme_bus.theme_changed.connect(lambda _p: _invalidate_icon_cache())
except Exception:
    pass

_STATUS_COLOR = {
    "executed": QColor("#26a69a"),
    "watching": QColor("#ff9800"),
    "rejected": QColor("#ef5350"),
    "pending": QColor("#787b86"),
    "sent": QColor("#2962ff"),
    "claimed": QColor("#2962ff"),
    "failed": QColor("#ef5350"),
}

_REJECTED_BG = QColor(220, 50, 47, 28)


@dataclass(frozen=True)
class ActionRow:
    id: int
    action_type: str
    side: str
    status: str
    created_at: str
    payload: dict
    quality_score: int | None
    quality_verdict: str | None

    @property
    def is_open(self) -> bool:
        # Both OPEN (structured) and OPEN_INSTANT (bare directional) carry a
        # side and get the evaluator scored against them, so both qualify
        # for the "signal-quality" rendering path.
        return self.action_type in ("OPEN", "OPEN_INSTANT")

    @property
    def type_display(self) -> str:
        if self.action_type == "OPEN" and self.side:
            return f"OPEN  {self.side.lower()}"
        if self.action_type == "OPEN_INSTANT" and self.side:
            return f"OPEN_INSTANT  {self.side.lower()}"
        if self.action_type == "ALERT":
            lvl = self.payload.get("level", "info") if self.payload else "info"
            return f"ALERT  {lvl}"
        return self.action_type


def _parse(row: sqlite3.Row) -> ActionRow:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    evaluation = payload.get("evaluation") if isinstance(payload, dict) else None
    score = None
    verdict = None
    if isinstance(evaluation, dict):
        raw_score = evaluation.get("score")
        if isinstance(raw_score, (int, float)):
            score = int(raw_score)
        v = evaluation.get("verdict")
        if isinstance(v, str):
            verdict = v
    return ActionRow(
        id=int(row["id"]),
        action_type=str(row["action_type"]),
        side=str(payload.get("side", "")) if isinstance(payload, dict) else "",
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        payload=payload if isinstance(payload, dict) else {},
        quality_score=score,
        quality_verdict=verdict,
    )


def _age_text(created_at: str) -> str:
    try:
        s = created_at.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    delta = datetime.now(timezone.utc) - dt
    secs = int(delta.total_seconds())
    if secs < 0:
        secs = 0
    if secs < 60:
        return f"{secs}s"
    if secs < 3600:
        return f"{secs // 60}m {secs % 60}s"
    if secs < 86400:
        return f"{secs // 3600}h {(secs % 3600) // 60}m"
    return f"{secs // 86400}d {(secs % 86400) // 3600}h"


class ActionsModel(QAbstractTableModel):
    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ActionRow] = []

    def set_rows(self, raw_rows: list[sqlite3.Row]) -> None:
        self.beginResetModel()
        self._rows = [_parse(r) for r in raw_rows]
        self.endResetModel()

    def action_at(self, row: int) -> ActionRow | None:
        if 0 <= row < len(self._rows):
            return self._rows[row]
        return None

    def refresh_ages(self) -> None:
        if not self._rows:
            return
        top = self.index(0, COL_AGE)
        bottom = self.index(len(self._rows) - 1, COL_AGE)
        self.dataChanged.emit(top, bottom, [Qt.ItemDataRole.DisplayRole])

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
            if col == COL_ID:
                return str(row.id)
            if col == COL_TYPE:
                return row.type_display
            if col == COL_STATUS:
                if row.status == "pending":
                    # Icon comes from DecorationRole; show only the
                    # text here so the cell renders "icon + 'pending'".
                    return f"  {row.status}"
                glyph = _STATUS_DOT.get(row.status, "·")
                return f"{glyph}  {row.status}"
        if role == Qt.ItemDataRole.DecorationRole and col == COL_STATUS:
            if row.status == "pending":
                return _hourglass_icon()
            if col == COL_AGE:
                return _age_text(row.created_at)
            if col == COL_QUALITY:
                if row.is_open and row.quality_score is not None:
                    return f"{row.quality_score}"
                return ""
        if role == Qt.ItemDataRole.ForegroundRole and col == COL_STATUS:
            return _STATUS_COLOR.get(row.status)
        if role == Qt.ItemDataRole.BackgroundRole and row.status == "rejected":
            return _REJECTED_BG
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (COL_ID, COL_QUALITY):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == COL_QUALITY and row.quality_verdict:
            return f"{row.quality_score}/100  ·  {row.quality_verdict}"
        return None
