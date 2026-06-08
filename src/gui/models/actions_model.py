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


def manual_badge_text(payload_json: str | None) -> str:
    """Return 'MANUAL' when the action payload is a GUI-placed manual trade."""
    if not payload_json:
        return ""
    try:
        p = json.loads(payload_json)
    except (ValueError, TypeError):
        return ""
    return "MANUAL" if isinstance(p, dict) and p.get("manual") else ""


COL_ID = 0
COL_AGE = 1
COL_TYPE = 2
COL_SIDE = 3
COL_STATUS = 4
COL_CHANNEL = 5
COL_SCORE = 6
COL_MULT = 7
COL_PRICE = 8
COL_PNL = 9
COL_REASON = 10
HEADERS = (
    "#", "Age", "Type", "Side", "Status", "Channel",
    "Score", "Mult", "Price", "PnL", "Reason",
)

# Kept for backward compatibility with any caller still importing
# COL_QUALITY — points at the new Score column.
COL_QUALITY = COL_SCORE


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

def _palette():
    """Cached read of the current palette. Returns None when the theme
    module is unavailable (tests, early init)."""
    try:
        from src.gui.theme import current_palette
        return current_palette()
    except Exception:
        return None


def _score_color(score: int) -> QColor | None:
    """Map an evaluator 0-100 score to the verdict palette tokens.
    Same color logic as the Evaluation tab's score badge, sourced from
    the active palette so the visual language stays unified across
    tabs."""
    pal = _palette()
    if pal is None:
        return None
    if score >= 80:
        return QColor(pal.success)
    if score >= 60:
        return QColor(pal.warning)
    if score >= 40:
        return QColor(pal.warning).darker(120) if pal.warning else None
    return QColor(pal.danger)


def _status_color(status: str) -> QColor | None:
    """Resolve the status pill foreground colour against the active
    palette so light / dark mode both render at proper contrast. The
    static hex map this replaces was tuned for dark mode only and
    washed out on light surfaces.
    """
    try:
        from src.gui.theme import current_palette
        pal = current_palette()
    except Exception:
        return None
    mapping = {
        "executed": QColor(pal.success),
        "watching": QColor(pal.warning),
        "rejected": QColor(pal.danger),
        "pending":  QColor(pal.text_muted),
        "sent":     QColor(pal.accent),
        "claimed":  QColor(pal.accent),
        "failed":   QColor(pal.danger),
    }
    return mapping.get(status)


def _rejected_bg() -> QColor:
    """Faint tint for fully-rejected rows. Pulled from the palette's
    danger token so it stays sympathetic in both themes."""
    try:
        from src.gui.theme import current_palette
        c = QColor(current_palette().danger)
    except Exception:
        c = QColor("#ef5350")
    c.setAlpha(28)
    return c


@dataclass(frozen=True)
class ActionRow:
    """One row backing the Actions table.

    Many fields are nullable because they depend on the action's type
    and lifecycle. ALERT actions have no side/score/price; pending
    actions have no PnL; non-OPEN actions have no evaluator data. The
    model's data() method handles None by rendering "—".
    """
    id: int
    action_type: str
    side: str
    status: str
    created_at: str
    payload: dict
    quality_score: int | None
    quality_verdict: str | None
    multiplier: float | None
    entry_price: float | None
    exit_price: float | None
    realized_pnl: float | None
    close_reason: str
    ea_response: str
    # Post-v2 dashboard fix: which v2 Channel produced this action.
    # Empty string on legacy rows (pre-Step-11) or single-stack installs
    # that never wrote source_channel_id. The model renders "—" then.
    source_channel_id: str = ""

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

    @property
    def reason_display(self) -> str:
        """The 'why it ended' text used in the Reason column. Closed
        positions surface their broker close_reason (tp1 / mt5_sl /
        etc.); actions that never produced a position surface the EA
        response or status. Pending rows return ""."""
        if self.close_reason:
            return self.close_reason
        if self.ea_response:
            return self.ea_response
        return ""

    def search_fields(self) -> dict:
        """Flat dict the search parser's `apply` reads. Keys must match
        the parser's known field names."""
        return {
            "id": self.id,
            "type": self.action_type,
            "status": self.status,
            "side": self.side,
            "reason": self.reason_display,
            "verdict": self.quality_verdict,
            "score": self.quality_score,
            "mult": self.multiplier,
            "pnl": self.realized_pnl,
        }


def _f(value) -> float | None:
    """Tolerant float-cast for sqlite Row values that may be None or
    string-typed (legacy migrations)."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse(row: sqlite3.Row) -> ActionRow:
    try:
        payload = json.loads(row["payload_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    evaluation = payload.get("evaluation") if isinstance(payload, dict) else None
    score = None
    verdict = None
    multiplier = None
    if isinstance(evaluation, dict):
        raw_score = evaluation.get("score")
        if isinstance(raw_score, (int, float)):
            score = int(raw_score)
        v = evaluation.get("verdict")
        if isinstance(v, str):
            verdict = v
        sizing = evaluation.get("sizing")
        if isinstance(sizing, dict):
            m = sizing.get("multiplier")
            if isinstance(m, (int, float)):
                multiplier = float(m)
    # Position-joined columns. Present only when the query LEFT-JOINs
    # positions; older callers using the 5-col subscription still
    # produce a row, just with all of these as None.
    keys = row.keys()
    return ActionRow(
        id=int(row["id"]),
        action_type=str(row["action_type"]),
        side=str(payload.get("side", "")) if isinstance(payload, dict) else "",
        status=str(row["status"]),
        created_at=str(row["created_at"]),
        payload=payload if isinstance(payload, dict) else {},
        quality_score=score,
        quality_verdict=verdict,
        multiplier=multiplier,
        entry_price=_f(row["entry_price"]) if "entry_price" in keys else None,
        exit_price=_f(row["exit_price"]) if "exit_price" in keys else None,
        realized_pnl=_f(row["realized_pnl"]) if "realized_pnl" in keys else None,
        close_reason=str(row["close_reason"]) if (
            "close_reason" in keys and row["close_reason"] is not None
        ) else "",
        ea_response=str(row["ea_response"]) if (
            "ea_response" in keys and row["ea_response"] is not None
        ) else "",
        source_channel_id=str(row["source_channel_id"]) if (
            "source_channel_id" in keys and row["source_channel_id"] is not None
        ) else "",
    )


def _created_at_epoch(created_at: str) -> float:
    """Best-effort parse of an action's created_at string into a Unix
    timestamp. Used by the sort key for the Age column so click-to-sort
    orders by actual recency, not by the rendered short-form string.
    Returns -inf on parse failure so malformed rows cluster at one end.
    """
    if not created_at:
        return float("-inf")
    try:
        s = created_at.replace("Z", "+00:00").replace(" ", "T")
        if not (s.endswith("Z") or "+" in s[10:] or "-" in s[10:]):
            s = s + "+00:00"
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return float("-inf")


def _sort_value(row: "ActionRow", col: int) -> object:
    """Return the raw value the proxy should sort the row by for the
    given column. Numeric columns return floats (with -inf for None);
    string columns return strings; the Age column returns the negative
    timestamp so ascending sort puts NEWEST first (lowest age) — which
    matches what an operator expects when clicking "Age" once.
    """
    NEG_INF = float("-inf")
    if col == COL_ID:
        return row.id
    if col == COL_AGE:
        # Negative timestamp -> ascending sort puts smallest age (newest)
        # first. Descending puts oldest first. Both directions are useful.
        return -_created_at_epoch(row.created_at)
    if col == COL_TYPE:
        return row.action_type or ""
    if col == COL_SIDE:
        return row.side or ""
    if col == COL_STATUS:
        return row.status or ""
    if col == COL_CHANNEL:
        return row.source_channel_id or ""
    if col == COL_SCORE:
        return row.quality_score if row.quality_score is not None else NEG_INF
    if col == COL_MULT:
        return row.multiplier if row.multiplier is not None else NEG_INF
    if col == COL_PRICE:
        # Sort by entry price; exit price falls back when entry missing.
        if row.entry_price is not None:
            return row.entry_price
        if row.exit_price is not None:
            return row.exit_price
        return NEG_INF
    if col == COL_PNL:
        return row.realized_pnl if row.realized_pnl is not None else NEG_INF
    if col == COL_REASON:
        return row.reason_display or ""
    return ""


_CHANNEL_NAME_CACHE: dict[str, str] = {}


def _channel_display_name(channel_id: str) -> str:
    """v2 Channel.id → display name, cached per process.

    Resolves via ``config_v2.load_v2()`` once per (id, cache miss).
    Fall-through to the raw id keeps non-v2 / orphan-id rows readable.
    The cache is process-global because v2 config changes infrequently
    and a stale name is preferable to a per-row file read.
    """
    if not channel_id:
        return ""
    cached = _CHANNEL_NAME_CACHE.get(channel_id)
    if cached is not None:
        return cached
    try:
        from src import config_v2
        cfg = config_v2.load_v2(config_v2.config_path())
        if cfg is not None:
            ch = cfg.channel(channel_id)
            name = ch.name if ch is not None else channel_id
        else:
            name = channel_id
    except Exception:
        name = channel_id
    _CHANNEL_NAME_CACHE[channel_id] = name
    return name


def clear_channel_name_cache() -> None:
    """Reset the channel-name cache. Called when stacks_config.json changes
    so renamed channels show their new names on the next paint."""
    _CHANNEL_NAME_CACHE.clear()


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
        self._selected_rows: set[int] = set()
        # Theme swap → re-emit all status-foreground roles so cells
        # repaint with the new palette's tokens instead of staying on
        # the previously cached colour.
        try:
            from src.gui.theme import bus as _bus
            _bus.theme_changed.connect(lambda _p: self._on_theme_changed())
        except Exception:
            pass

    def _on_theme_changed(self) -> None:
        if not self._rows:
            return
        top = self.index(0, COL_STATUS)
        bottom = self.index(len(self._rows) - 1, COL_STATUS)
        self.dataChanged.emit(
            top, bottom,
            [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.BackgroundRole],
        )

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
            if col == COL_AGE:
                return _age_text(row.created_at)
            if col == COL_TYPE:
                badge = manual_badge_text(
                    json.dumps(row.payload) if row.payload else None
                )
                type_str = row.action_type
                return f"{type_str}  [{badge}]" if badge else type_str
            if col == COL_SIDE:
                if row.side in ("BUY", "SELL"):
                    return "↑ BUY" if row.side == "BUY" else "↓ SELL"
                return ""
            if col == COL_STATUS:
                if row.status == "pending":
                    return f"  {row.status}"
                glyph = _STATUS_DOT.get(row.status, "·")
                return f"{glyph}  {row.status}"
            if col == COL_CHANNEL:
                # Resolve the v2 Channel.name from id when v2 cfg is
                # loadable; fall back to the raw id, then "—" for legacy.
                if not row.source_channel_id:
                    return "—"
                return _channel_display_name(row.source_channel_id)
            if col == COL_SCORE:
                if row.is_open and row.quality_score is not None:
                    return f"{row.quality_score}"
                return ""
            if col == COL_MULT:
                if row.multiplier is None:
                    return ""
                return f"{row.multiplier:.2f}×"
            if col == COL_PRICE:
                if row.entry_price is None and row.exit_price is None:
                    return ""
                if row.exit_price is None:
                    return f"{row.entry_price:.2f}"
                if row.entry_price is None:
                    return f"→ {row.exit_price:.2f}"
                return f"{row.entry_price:.2f} → {row.exit_price:.2f}"
            if col == COL_PNL:
                if row.realized_pnl is None:
                    return ""
                return f"${row.realized_pnl:+.2f}"
            if col == COL_REASON:
                return row.reason_display
        if role == Qt.ItemDataRole.DecorationRole and col == COL_STATUS:
            if row.status == "pending":
                return _hourglass_icon()
        if role == Qt.ItemDataRole.ForegroundRole:
            # Selection-aware foreground: while the row is selected, the
            # accent tint is rendered behind it. Returning None lets the
            # palette's selection foreground take over so the colored
            # status / pnl text doesn't fight that tint.
            selected = bool(
                self._selected_rows and index.row() in self._selected_rows
            )
            if selected:
                return None
            if col == COL_STATUS:
                return _status_color(row.status)
            if col == COL_SIDE and row.side in ("BUY", "SELL"):
                pal = _palette()
                if pal is None:
                    return None
                return QColor(pal.success) if row.side == "BUY" else QColor(pal.danger)
            if col == COL_PNL and row.realized_pnl is not None:
                pal = _palette()
                if pal is None:
                    return None
                if row.realized_pnl > 0:
                    return QColor(pal.success)
                if row.realized_pnl < 0:
                    return QColor(pal.danger)
            if col == COL_SCORE and row.quality_score is not None:
                return _score_color(row.quality_score)
        if role == Qt.ItemDataRole.BackgroundRole and row.status == "rejected":
            return _rejected_bg()
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (
            COL_ID, COL_SCORE, COL_MULT, COL_PNL,
        ):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ToolTipRole and col == COL_SCORE and row.quality_verdict:
            return f"{row.quality_score}/100  ·  {row.quality_verdict}"
        # UserRole returns the raw sortable value per column. The proxy
        # is configured with `setSortRole(UserRole)` so clicking a
        # header sorts numerically/temporally instead of
        # lexicographically on the rendered display string (which would
        # rank "100" before "20" alphabetically, treat "3m" and "1h" as
        # adjacent text, etc.). None / unset values become -inf so they
        # cluster at one end regardless of direction.
        if role == Qt.ItemDataRole.UserRole:
            return _sort_value(row, col)
        return None

    def set_selected_rows(self, rows: set[int]) -> None:
        """Called by ActionsTable whenever the selection changes. Drives
        the ForegroundRole suppression that prevents bright status text
        from clashing with the soft selection tint."""
        if rows == self._selected_rows:
            return
        affected = self._selected_rows | rows
        self._selected_rows = set(rows)
        if not affected:
            return
        first = min(affected)
        last = max(affected)
        top = self.index(first, COL_STATUS)
        bottom = self.index(last, COL_STATUS)
        self.dataChanged.emit(
            top, bottom, [Qt.ItemDataRole.ForegroundRole]
        )
