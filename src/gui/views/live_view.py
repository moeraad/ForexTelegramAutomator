"""Live view: ACTIONS + DETAIL + POSITIONS + LOG STREAM."""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSplitter, QVBoxLayout, QWidget

from src.gui.panels.actions_table import ActionsTable
from src.gui.panels.detail_panel import DetailPanel
from src.gui.panels.log_stream import LogStream
from src.gui.panels.positions_table import PositionsTable
from src.gui.services.db_subscriber import DBSubscriber
from src.gui.services.stack_registry import Stack


class LiveView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._subscriber = DBSubscriber(stack.db_path, parent=self)
        self._build_ui()
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(1000)
        self._age_timer.timeout.connect(self._tick_ages)
        self._subscriber.start()
        self._age_timer.start()

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._age_timer.stop()
        self._subscriber.stop()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        self._actions_table = ActionsTable(self._subscriber, stack=self._stack)
        self._detail_panel = DetailPanel(self._subscriber, self._stack.db_path)
        self._actions_table.selection_changed.connect(self._detail_panel.set_action)

        upper = QSplitter(Qt.Orientation.Horizontal)
        upper.addWidget(self._actions_table)
        upper.addWidget(self._detail_panel)
        upper.setStretchFactor(0, 3)
        upper.setStretchFactor(1, 2)

        self._positions_table = PositionsTable(self._subscriber)
        self._positions_table.setMinimumHeight(140)

        self._log_stream = LogStream(self._stack)
        self._log_stream.setMinimumHeight(160)

        outer = QSplitter(Qt.Orientation.Vertical)
        outer.addWidget(upper)
        outer.addWidget(self._positions_table)
        outer.addWidget(self._log_stream)
        outer.setStretchFactor(0, 5)
        outer.setStretchFactor(1, 1)
        outer.setStretchFactor(2, 2)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # EA heartbeat + claim staleness — render as pill-shaped tiles
        # so the operator's eye finds them in a fixed location even when
        # the value is "—". Previously rendered as flat inline text which
        # is easy to miss when not scanning (REVIEW.md §4.1).
        self._indicators = QWidget()
        self._indicators.setFixedHeight(28)
        ind_layout = QHBoxLayout(self._indicators)
        ind_layout.setContentsMargins(8, 0, 8, 0)
        ind_layout.setSpacing(6)
        self._ea_lbl = QLabel("EA: —")
        self._ea_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._ea_lbl.setProperty("role", "indicator-tile")
        self._ea_lbl.setAccessibleName("EA heartbeat age")
        self._claim_lbl = QLabel("Oldest claim: —")
        self._claim_lbl.setTextFormat(Qt.TextFormat.RichText)
        self._claim_lbl.setProperty("role", "indicator-tile")
        self._claim_lbl.setAccessibleName("Oldest claim age")
        ind_layout.addWidget(self._ea_lbl)
        ind_layout.addWidget(self._claim_lbl)
        ind_layout.addStretch()

        layout.addWidget(outer, 1)
        layout.addWidget(self._indicators)

        # Theme-aware styling: paint once now, then re-apply whenever
        # the user toggles light/dark. Without the bus subscription the
        # pill surface/border/text colours were baked in at construction
        # and stayed frozen on theme swap, looking wrong in the other
        # mode.
        self._apply_indicator_theme()
        try:
            from src.gui.theme import bus as _theme_bus
            _theme_bus.theme_changed.connect(
                lambda _pal: (self._apply_indicator_theme(), self._refresh_indicators())
            )
        except Exception:
            pass

    def _tick_ages(self) -> None:
        self._actions_table.refresh_ages()
        self._positions_table.refresh_ages()
        self._refresh_indicators()

    def _apply_indicator_theme(self) -> None:
        """Re-skin the indicator pills against the active palette.

        Re-invoked on every `theme_changed` so toggling light/dark mode
        immediately repaints the EA / Oldest claim chips instead of
        leaving them stuck in the colours captured at construction.
        """
        from src.gui.theme import current_palette
        pal = current_palette()
        self._indicators.setStyleSheet(
            "QLabel[role='indicator-tile'] {"
            f"  background: {pal.surface};"
            f"  border: 1px solid {pal.border};"
            f"  color: {pal.text_muted};"
            "  border-radius: 12px;"
            "  padding: 2px 12px;"
            "  font-size: 11px;"
            "  margin: 2px 4px;"
            "}"
        )

    def _refresh_indicators(self) -> None:
        try:
            conn = sqlite3.connect(str(self._stack.db_path))
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='market_XAUUSD_at'"
                ).fetchone()
                ea_iso = row[0] if row else ""
                claim = conn.execute(
                    "SELECT MIN(claimed_at) FROM actions WHERE status='claimed'"
                ).fetchone()[0]
            finally:
                conn.close()
        except sqlite3.Error:
            return
        from src.gui.theme import current_palette
        pal = current_palette()
        now = datetime.now(timezone.utc)
        self._ea_lbl.setText(self._format_age("EA", ea_iso, now, warn=30, bad=60, pal=pal))
        self._claim_lbl.setText(
            self._format_age("Oldest claim", claim, now, warn=15, bad=45, pal=pal)
        )

    @staticmethod
    def _format_age(
        label: str,
        iso: str | None,
        now: datetime,
        warn: int,
        bad: int,
        pal,
    ) -> str:
        if not iso:
            return f"{label}: —"
        try:
            ts = datetime.fromisoformat(iso)
        except ValueError:
            return f"{label}: —"
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        age = int((now - ts).total_seconds())
        if age < 0:
            age = 0
        if age < 60:
            text = f"{age}s ago"
        elif age < 3600:
            text = f"{age // 60}m ago"
        else:
            text = f"{age // 3600}h ago"
        # Pull state colours from the active palette so the warning /
        # danger hues read correctly in both light and dark themes
        # instead of being baked to a single hex value.
        color = pal.text_muted
        if age >= bad:
            color = pal.danger
        elif age >= warn:
            color = pal.warning
        return f"<span style='color:{color};'>{label}: {text}</span>"

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._subscriber.rebind(stack.db_path)
        self._detail_panel.rebind(stack.db_path)
        self._log_stream.rebind(stack)
