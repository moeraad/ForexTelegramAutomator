"""JOURNAL view: closed-trade history, summary stats, equity curve, CSV export.

Redesigned 2026-05-20 to use a vertical-scroll layout (so each section
gets the room it needs instead of fighting splitters) and pyqtgraph for
the charts (consistent with the Evaluation tab).
"""
from __future__ import annotations

import csv
from datetime import datetime, timezone
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QTableView,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.journal_model import JournalModel
from src.gui.services.journal_data import (
    EquityPoint,
    JournalSummary,
    closed_trades,
    equity_curve,
    pnl_by_close_reason,
    pnl_by_day,
    summary,
)
from src.gui.services.stack_registry import Stack
from src.gui.views._pg_hover import HoverPoint, HoverTracker


_RANGES: list[tuple[str, int | None]] = [
    ("Today", 0),
    ("Last 3 days", 3),
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("All time", None),
]


# Per-section minimum heights. The outer QScrollArea handles overflow,
# so each section can claim the space it actually needs to be readable
# rather than fighting the others through splitters.
_HEIGHT_EQUITY_CHART = 320
_HEIGHT_DAILY_CHART = 260
_HEIGHT_TRADES_TABLE = 360
_HEIGHT_REASON_TABLE = 220


class JournalView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._days: int | None = 30
        self._model = JournalModel()
        self._summary = JournalSummary(0.0, 0, 0, 0, 0.0, 0.0, 0.0)
        self._stat_boxes: dict[str, QWidget] = {}
        self._build_ui()
        self.refresh()
        from src.gui.theme import bus as theme_bus
        theme_bus.theme_changed.connect(self._on_theme)

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self.refresh()

    def _on_theme(self, _pal=None) -> None:
        """pyqtgraph uses its own config-options for bg/fg colors. We
        set those once at view construction; on theme change we apply
        them again and re-render the charts so the new colors take
        effect."""
        self._apply_chart_theme()
        self.refresh()

    # ---- pyqtgraph theming ---------------------------------------------

    def _apply_chart_theme(self) -> None:
        """Push the current palette into pyqtgraph's module-level
        defaults. Affects all PlotWidgets going forward, including
        ours after they're cleared and repopulated on refresh.

        Falls back to dark defaults when current_palette() can't be
        imported (tests / early init).
        """
        try:
            from src.gui.theme import current_palette
            p = current_palette()
            bg, fg = p.bg, p.text
        except Exception:
            bg, fg = "#0f1115", "#d1d4dc"
        pg.setConfigOption("background", bg)
        pg.setConfigOption("foreground", fg)
        # Re-apply to existing plots so the colors swap immediately,
        # not just on next clear.
        for plot in (
            getattr(self, "_equity_plot", None),
            getattr(self, "_daily_plot", None),
        ):
            if plot is not None:
                plot.setBackground(bg)

    # ---- Build ---------------------------------------------------------

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, 12, 12, 12)
        outer.setSpacing(10)

        # Toolbar — title + range + refresh + export. Lives OUTSIDE the
        # scroll area so it's always visible regardless of how far the
        # operator has scrolled down the body.
        outer.addLayout(self._build_toolbar())

        # Scrollable body. Each section gets a stable minimum height
        # via _HEIGHT_* so charts don't compress into illegibility on
        # short windows; the scrollbar absorbs the rest.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        body_layout.setSpacing(14)

        body_layout.addLayout(self._build_stats_row())
        body_layout.addWidget(self._build_equity_section())
        body_layout.addWidget(self._build_daily_section())
        body_layout.addWidget(self._build_trades_section())
        body_layout.addWidget(self._build_reason_section())
        body_layout.addStretch()

        scroll.setWidget(body)
        outer.addWidget(scroll, 1)

        # pyqtgraph theming uses module-level defaults — apply after the
        # plots exist so the bg color propagates immediately.
        self._apply_chart_theme()

    def _build_toolbar(self) -> QHBoxLayout:
        top = QHBoxLayout()
        title = QLabel(
            "<span style='font-size:16px; font-weight:700;'>JOURNAL</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        try:
            from src.gui.panels._a11y import mark_heading
            mark_heading(title, "Journal")
        except Exception:
            pass
        top.addWidget(title)
        top.addSpacing(16)
        top.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, days in _RANGES:
            self._range_combo.addItem(label, days)
        self._range_combo.setCurrentIndex(3)  # Last 30 days default.
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        top.addWidget(self._range_combo)
        top.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload journal")
        self._refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self._refresh_btn)
        from src.gui._button_helpers import make_icon_button
        self._export_btn = make_icon_button(
            "SHARE", "Export visible rows to CSV",
            variant="primary", fallback_text="Export CSV",
        )
        self._export_btn.clicked.connect(self._export_csv)
        top.addWidget(self._export_btn)
        return top

    def _build_stats_row(self) -> QHBoxLayout:
        from src.gui.panels._stat_card import StatCard
        row = QHBoxLayout()
        row.setSpacing(8)
        for key, label, accent in (
            ("total_pnl", "Total PnL", ""),
            ("trades", "Trades", ""),
            ("win_rate", "Win rate", ""),
            ("avg", "Avg", ""),
            ("best", "Best", "success"),
            ("worst", "Worst", "danger"),
        ):
            card = StatCard(label=label, value="—", accent=accent)
            self._stat_boxes[key] = card
            row.addWidget(card)
        row.addStretch()
        return row

    def _section_header(self, text: str) -> QLabel:
        """Common style for the section captions above each chart/table."""
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "font-size: 12px; font-weight: 700; letter-spacing: 1.5px; "
            "color: #787b86; padding-top: 4px; padding-bottom: 4px;"
        )
        return lbl

    def _build_equity_section(self) -> QWidget:
        """Equity curve — cumulative PnL over closed-at time.

        pyqtgraph DateAxisItem renders Unix-time floats as nice
        timestamps. Single LineGraph in the accent color; pen width 2
        for readability against the dark bg.
        """
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._equity_header = self._section_header(
            "EQUITY CURVE  ·  cumulative PnL over time"
        )
        layout.addWidget(self._equity_header)
        # DateAxisItem on the bottom so x-axis ticks render as dates.
        # autoSIPrefix=False on the y-axis so we get raw dollar values
        # (default '12.5k' is unhelpful at the scale we trade).
        self._equity_plot = pg.PlotWidget(
            axisItems={"bottom": pg.DateAxisItem(orientation="bottom")}
        )
        self._equity_plot.setMinimumHeight(_HEIGHT_EQUITY_CHART)
        self._equity_plot.showGrid(x=False, y=True, alpha=0.2)
        self._equity_plot.setLabel("left", "Cumulative PnL ($)")
        self._equity_plot.setMenuEnabled(False)
        self._equity_plot.getAxis("left").enableAutoSIPrefix(False)
        self._equity_plot.getAxis("bottom").enableAutoSIPrefix(False)
        # Hover tooltip: timestamp + cumulative PnL at the closest
        # trade. The format callback receives the HoverPoint with
        # label set to the trade's closed_at string.
        self._equity_hover = HoverTracker(
            self._equity_plot,
            format_fn=lambda p: (
                f"<b>{p.label}</b><br>"
                f"cumulative PnL: <b style='color:"
                f"{'#26a69a' if p.y >= 0 else '#ef5350'};'>${p.y:+.2f}</b>"
            ),
        )
        layout.addWidget(self._equity_plot)
        return wrap

    def _build_daily_section(self) -> QWidget:
        """PnL per day — diverging bar chart (green wins, red losses).

        Two BarGraphItems on the same plot, offset by zero so they
        stack visually without overlap (each only carries values of one
        sign). x-axis uses categorical labels via setTicks.
        """
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._daily_header = self._section_header(
            "PNL PER DAY  ·  net by trading day"
        )
        layout.addWidget(self._daily_header)
        self._daily_plot = pg.PlotWidget()
        self._daily_plot.setMinimumHeight(_HEIGHT_DAILY_CHART)
        self._daily_plot.showGrid(x=False, y=True, alpha=0.2)
        self._daily_plot.setLabel("left", "PnL ($)")
        self._daily_plot.setMenuEnabled(False)
        self._daily_plot.getAxis("left").enableAutoSIPrefix(False)
        # Hover tooltip: day label + net PnL for that day. Bar charts
        # use x = bar index (0..N-1); the hover snaps to whichever bar
        # x is closest.
        self._daily_hover = HoverTracker(
            self._daily_plot,
            format_fn=lambda p: (
                f"<b>{p.label}</b><br>"
                f"net PnL: <b style='color:"
                f"{'#26a69a' if p.y >= 0 else '#ef5350'};'>${p.y:+.2f}</b>"
            ),
        )
        layout.addWidget(self._daily_plot)
        return wrap

    def _build_trades_section(self) -> QWidget:
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._trades_header = self._section_header("CLOSED TRADES")
        layout.addWidget(self._trades_header)
        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        try:
            from src.gui.panels._table_utils import apply_full_width_headers
            apply_full_width_headers(
                self._table,
                content_columns=tuple(range(self._model.columnCount() - 1)),
            )
        except Exception:
            pass
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setMinimumHeight(_HEIGHT_TRADES_TABLE)
        layout.addWidget(self._table)
        return wrap

    def _build_reason_section(self) -> QWidget:
        """Close-reason breakdown as a real QTableWidget — was a
        word-wrapped HTML <table> inside a QLabel previously, which
        rendered cramped and couldn't be sorted."""
        wrap = QWidget()
        layout = QVBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self._reason_header = self._section_header(
            "BY CLOSE REASON  ·  count and PnL per reason"
        )
        layout.addWidget(self._reason_header)
        self._reason_table = QTableWidget(0, 3)
        self._reason_table.setHorizontalHeaderLabels(("Reason", "Count", "PnL"))
        self._reason_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._reason_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._reason_table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._reason_table.verticalHeader().setVisible(False)
        self._reason_table.setAlternatingRowColors(True)
        self._reason_table.setMinimumHeight(_HEIGHT_REASON_TABLE)
        hdr = self._reason_table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        hdr.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hdr.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._reason_table)
        return wrap

    # ---- Refresh -------------------------------------------------------

    def refresh(self) -> None:
        trades = closed_trades(self._stack.db_path, days=self._days)
        self._model.set_rows(trades)
        self._summary = summary(trades)
        self._update_stats()
        self._update_equity(equity_curve(trades))
        self._update_daily(pnl_by_day(trades))
        self._update_reason_table(pnl_by_close_reason(trades))

    def _on_range_changed(self, _idx: int) -> None:
        self._days = self._range_combo.currentData()
        self.refresh()

    def _update_stats(self) -> None:
        s = self._summary
        pnl_accent = "success" if s.total_pnl >= 0 else "danger"
        self._stat_boxes["total_pnl"].set_value(f"${s.total_pnl:+.2f}", pnl_accent)
        self._stat_boxes["trades"].set_value(str(s.total_trades))
        self._stat_boxes["win_rate"].set_value(
            f"{s.win_rate * 100:.0f}%  ({s.win_count}/{s.total_trades})"
        )
        self._stat_boxes["avg"].set_value(
            f"${s.avg_pnl:+.2f}" if s.total_trades else "—"
        )
        self._stat_boxes["best"].set_value(
            f"${s.best:+.2f}" if s.total_trades else "—", "success",
        )
        self._stat_boxes["worst"].set_value(
            f"${s.worst:+.2f}" if s.total_trades else "—", "danger",
        )

    # ---- Charts --------------------------------------------------------

    def _update_equity(self, points: list[EquityPoint]) -> None:
        # PlotWidget.clear() removes the crosshair items too — reattach
        # via the tracker's public method so subsequent mouse events
        # still find live widgets to update.
        self._equity_plot.clear()
        self._equity_hover.reattach()
        if not points:
            self._equity_header.setText(
                "EQUITY CURVE  ·  no closed trades in range"
            )
            self._equity_hover.set_points([])
            return
        # x in Unix seconds (UTC) for DateAxisItem; y in dollars.
        xs = [p.closed_at.replace(tzinfo=timezone.utc).timestamp()
              if p.closed_at.tzinfo is None else p.closed_at.timestamp()
              for p in points]
        ys = [p.cumulative_pnl for p in points]
        accent = QColor("#5b8def")
        self._equity_plot.plot(
            xs, ys, pen=pg.mkPen(accent, width=2),
            symbol="o", symbolSize=4,
            symbolBrush=accent, symbolPen=accent,
        )
        # Y range with 10% padding so the line doesn't kiss the borders.
        ymin, ymax = min(ys), max(ys)
        pad = max(1.0, (ymax - ymin) * 0.1)
        self._equity_plot.setYRange(ymin - pad, ymax + pad, padding=0)
        self._equity_hover.set_points([
            HoverPoint(
                x=xs[i],
                y=ys[i],
                label=points[i].closed_at.strftime("%Y-%m-%d %H:%M"),
            )
            for i in range(len(points))
        ])
        self._equity_header.setText(
            f"EQUITY CURVE  ·  {len(points)} trades  ·  end ${ys[-1]:+.2f}"
        )

    def _update_daily(self, days) -> None:
        """Diverging bar chart. Wins green, losses red, color-coded
        per bar. x ticks are MM-DD strings (categorical)."""
        self._daily_plot.clear()
        self._daily_hover.reattach()
        if not days:
            self._daily_header.setText("PNL PER DAY  ·  no data")
            self._daily_hover.set_points([])
            return
        win_color = QColor("#26a69a")
        loss_color = QColor("#ef5350")
        xs = list(range(len(days)))
        wins_y = [d.pnl if d.pnl >= 0 else 0.0 for d in days]
        loss_y = [d.pnl if d.pnl < 0 else 0.0 for d in days]
        self._daily_plot.addItem(pg.BarGraphItem(
            x=xs, height=wins_y, width=0.7,
            brush=win_color, pen=win_color,
        ))
        self._daily_plot.addItem(pg.BarGraphItem(
            x=xs, height=loss_y, width=0.7,
            brush=loss_color, pen=loss_color,
        ))
        x_axis = self._daily_plot.getAxis("bottom")
        x_axis.setTicks([
            [(i, d.day[5:]) for i, d in enumerate(days)],
        ])
        # Hover points: bar index as x, total pnl as y, full date as
        # label. The crosshair will snap to the bar nearest the cursor.
        self._daily_hover.set_points([
            HoverPoint(x=float(i), y=d.pnl, label=d.day)
            for i, d in enumerate(days)
        ])
        total = sum(d.pnl for d in days)
        self._daily_header.setText(f"PNL PER DAY  ·  net ${total:+.2f}")

    def _update_reason_table(self, by_reason: dict[str, tuple[float, int]]) -> None:
        self._reason_table.setRowCount(0)
        if not by_reason:
            return
        # Sort by descending count (most-frequent reason first), then by
        # absolute PnL so big losers/winners surface above one-offs.
        items = sorted(
            by_reason.items(),
            key=lambda kv: (-kv[1][1], -abs(kv[1][0])),
        )
        for reason, (pnl, n) in items:
            row = self._reason_table.rowCount()
            self._reason_table.insertRow(row)
            reason_item = QTableWidgetItem(str(reason))
            count_item = QTableWidgetItem(str(n))
            pnl_item = QTableWidgetItem(f"${pnl:+.2f}")
            if pnl > 0:
                pnl_item.setForeground(QColor("#26a69a"))
            elif pnl < 0:
                pnl_item.setForeground(QColor("#ef5350"))
            self._reason_table.setItem(row, 0, reason_item)
            self._reason_table.setItem(row, 1, count_item)
            self._reason_table.setItem(row, 2, pnl_item)

    # ---- Export --------------------------------------------------------

    def _export_csv(self) -> None:
        rows = self._model.rows()
        if not rows:
            QMessageBox.information(
                self, "Export CSV", "No trades to export in this range."
            )
            return
        suggested = f"copytrades_journal_{self._stack.name}.csv"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export Journal CSV", suggested, "CSV (*.csv)"
        )
        if not path_str:
            return
        path = Path(path_str)
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow([
                "closed_at", "ticket", "action_id", "side", "volume",
                "original_volume", "entry_price", "exit_price",
                "realized_pnl", "close_reason", "opened_at",
            ])
            for r in rows:
                w.writerow([
                    r.closed_at, r.ticket, r.action_id or "", r.side, r.volume,
                    r.original_volume, r.entry_price, r.exit_price,
                    r.realized_pnl, r.close_reason, r.opened_at,
                ])
        QMessageBox.information(
            self, "Export CSV", f"Exported {len(rows)} trades to {path}"
        )
