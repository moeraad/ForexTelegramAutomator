"""JOURNAL view: closed-trade history, summary stats, equity curve, CSV export."""
from __future__ import annotations

import csv
from pathlib import Path

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QDateTimeAxis,
    QLineSeries,
    QValueAxis,
)
from PySide6.QtCore import QDateTime, Qt
from PySide6.QtGui import QColor, QPainter, QPen
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
    QTableView,
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


_RANGES: list[tuple[str, int | None]] = [
    ("Today", 0),
    ("Last 3 days", 3),
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("All time", None),
]


def _stat_box(label: str, value: str, color: str = "#073642") -> QWidget:
    box = QFrame()
    box.setFrameShape(QFrame.Shape.StyledPanel)
    box.setStyleSheet("QFrame { background: #fdf6e3; border-radius: 4px; }")
    box.setFixedSize(120, 80)
    layout = QVBoxLayout(box)
    layout.setContentsMargins(10, 8, 10, 8)
    layout.setSpacing(2)
    k = QLabel(label.upper())
    k.setStyleSheet("color: #93a1a1; font-size: 9px; letter-spacing: 1px;")
    v = QLabel(value)
    v.setStyleSheet(f"color: {color}; font-size: 16px; font-weight: 700;")
    v.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
    v.setWordWrap(True)
    layout.addWidget(k)
    layout.addWidget(v)
    layout.addStretch()
    return box


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

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self.refresh()

    def refresh(self) -> None:
        trades = closed_trades(self._stack.db_path, days=self._days)
        self._model.set_rows(trades)
        self._summary = summary(trades)
        self._update_stats()
        self._update_chart(equity_curve(trades))
        self._update_daily_chart(pnl_by_day(trades))
        self._update_reason_table(pnl_by_close_reason(trades))

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>JOURNAL</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(title)
        top.addSpacing(16)
        top.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, days in _RANGES:
            self._range_combo.addItem(label, days)
        self._range_combo.setCurrentIndex(3)  # default: Last 30 days
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        top.addWidget(self._range_combo)
        top.addStretch()

        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self.refresh)
        top.addWidget(self._refresh_btn)
        self._export_btn = QPushButton("Export CSV")
        self._export_btn.clicked.connect(self._export_csv)
        top.addWidget(self._export_btn)
        layout.addLayout(top)

        self._stats_row = QHBoxLayout()
        self._stats_row.setSpacing(8)
        for key, label in (
            ("total_pnl", "Total PnL"),
            ("trades", "Trades"),
            ("win_rate", "Win rate"),
            ("avg", "Avg"),
            ("best", "Best"),
            ("worst", "Worst"),
        ):
            box = _stat_box(label, "—")
            self._stat_boxes[key] = box
            self._stats_row.addWidget(box)
        self._stats_row.addStretch()
        layout.addLayout(self._stats_row)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        layout.addWidget(self._table, 1)

        charts_row = QHBoxLayout()
        charts_row.setSpacing(8)

        self._chart = QChart()
        self._chart.setTitle("Equity curve")
        self._chart.legend().hide()
        self._chart_view = QChartView(self._chart)
        self._chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        charts_row.addWidget(self._chart_view, 2)

        self._daily_chart = QChart()
        self._daily_chart.setTitle("PnL per day")
        self._daily_chart.legend().hide()
        self._daily_view = QChartView(self._daily_chart)
        self._daily_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        charts_row.addWidget(self._daily_view, 1)

        layout.addLayout(charts_row, 1)

        self._reason_label = QLabel("<b>By close reason</b>")
        self._reason_label.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._reason_label)
        self._reason_table = QLabel("(no closed trades in range)")
        self._reason_table.setStyleSheet(
            "QLabel { background: #fdf6e3; border: 1px solid #d6cfb8; "
            "padding: 6px; font-family: Consolas, monospace; }"
        )
        self._reason_table.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(self._reason_table)

    def _on_range_changed(self, _idx: int) -> None:
        days = self._range_combo.currentData()
        self._days = days
        self.refresh()

    def _update_stats(self) -> None:
        s = self._summary
        pnl_color = "#859900" if s.total_pnl >= 0 else "#dc322f"

        def _replace(key: str, label: str, value: str, color: str = "#073642") -> None:
            old = self._stat_boxes[key]
            new = _stat_box(label, value, color)
            idx = self._stats_row.indexOf(old)
            self._stats_row.removeWidget(old)
            old.deleteLater()
            self._stats_row.insertWidget(idx, new)
            self._stat_boxes[key] = new

        _replace("total_pnl", "Total PnL", f"${s.total_pnl:+.2f}", pnl_color)
        _replace("trades", "Trades", str(s.total_trades))
        _replace("win_rate", "Win rate", f"{s.win_rate * 100:.0f}%  ({s.win_count}/{s.total_trades})")
        _replace("avg", "Avg / trade", f"${s.avg_pnl:+.2f}" if s.total_trades else "—")
        _replace("best", "Best", f"${s.best:+.2f}" if s.total_trades else "—", "#859900")
        _replace("worst", "Worst", f"${s.worst:+.2f}" if s.total_trades else "—", "#dc322f")

    def _update_chart(self, points: list[EquityPoint]) -> None:
        self._chart.removeAllSeries()
        for ax in list(self._chart.axes()):
            self._chart.removeAxis(ax)

        if not points:
            self._chart.setTitle("Equity curve  ·  no closed trades in range")
            return

        series = QLineSeries()
        pen = QPen(QColor("#268bd2"))
        pen.setWidth(2)
        series.setPen(pen)
        for p in points:
            series.append(QDateTime(p.closed_at).toMSecsSinceEpoch(), p.cumulative_pnl)
        self._chart.addSeries(series)

        axis_x = QDateTimeAxis()
        axis_x.setFormat("MM-dd HH:mm")
        axis_x.setTitleText("closed at")
        axis_x.setLabelsAngle(-30)
        self._chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setTitleText("cumulative PnL ($)")
        ymin = min(p.cumulative_pnl for p in points)
        ymax = max(p.cumulative_pnl for p in points)
        pad = max(1.0, (ymax - ymin) * 0.1)
        axis_y.setRange(ymin - pad, ymax + pad)
        self._chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)

        self._chart.setTitle(
            f"Equity curve  ·  {len(points)} trades  ·  "
            f"end ${points[-1].cumulative_pnl:+.2f}"
        )

    def _update_daily_chart(self, days) -> None:
        self._daily_chart.removeAllSeries()
        for ax in list(self._daily_chart.axes()):
            self._daily_chart.removeAxis(ax)
        if not days:
            self._daily_chart.setTitle("PnL per day  ·  no data")
            return
        win_set = QBarSet("Wins")
        loss_set = QBarSet("Losses")
        win_set.setColor(QColor("#859900"))
        loss_set.setColor(QColor("#dc322f"))
        labels: list[str] = []
        for d in days:
            labels.append(d.day[5:])  # "MM-DD"
            if d.pnl >= 0:
                win_set.append(d.pnl)
                loss_set.append(0)
            else:
                win_set.append(0)
                loss_set.append(d.pnl)
        series = QBarSeries()
        series.append(win_set)
        series.append(loss_set)
        self._daily_chart.addSeries(series)
        axis_x = QBarCategoryAxis()
        axis_x.append(labels)
        self._daily_chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)
        axis_y = QValueAxis()
        axis_y.setTitleText("PnL ($)")
        self._daily_chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)
        total = sum(d.pnl for d in days)
        self._daily_chart.setTitle(f"PnL per day  ·  net ${total:+.2f}")

    def _update_reason_table(self, by_reason: dict[str, tuple[float, int]]) -> None:
        if not by_reason:
            self._reason_table.setText("(no closed trades in range)")
            return
        items = sorted(by_reason.items(), key=lambda kv: -kv[1][1])
        rows = []
        for reason, (pnl, n) in items:
            color = "#859900" if pnl >= 0 else "#dc322f"
            rows.append(
                f"<tr><td style='padding:2px 12px;'>{reason}</td>"
                f"<td style='padding:2px 12px;'>{n}</td>"
                f"<td style='padding:2px 12px; color:{color};'>${pnl:+.2f}</td></tr>"
            )
        self._reason_table.setText(
            "<table>"
            "<tr><th style='text-align:left; padding:2px 12px;'>reason</th>"
            "<th style='text-align:left; padding:2px 12px;'>#</th>"
            "<th style='text-align:left; padding:2px 12px;'>PnL</th></tr>"
            + "".join(rows)
            + "</table>"
        )

    def _export_csv(self) -> None:
        rows = self._model.rows()
        if not rows:
            QMessageBox.information(self, "Export CSV", "No trades to export in this range.")
            return
        suggested = f"copytrades_journal_{self._stack.name}.csv"
        path_str, _ = QFileDialog.getSaveFileName(self, "Export Journal CSV", suggested, "CSV (*.csv)")
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
        QMessageBox.information(self, "Export CSV", f"Exported {len(rows)} trades to {path}")
