# src/gui/views/manual_trade_chart.py
"""pyqtgraph candlestick chart for the Manual Trade tab.

Draws OHLC candles, a live price line, and three draggable horizontal lines
(entry / TP-or-SL / TP-or-SL). The view above reads line_values() to size and
submit the trade; the lines emit `lines_changed` while dragging.
"""
from __future__ import annotations

from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QPicture, QPainter
from PySide6.QtWidgets import QWidget, QVBoxLayout


class _CandleItem(pg.GraphicsObject):
    """Minimal candlestick item. bars: list of (x, open, high, low, close)."""

    def __init__(self) -> None:
        super().__init__()
        self._picture = QPicture()
        self._bars: list[tuple[float, float, float, float, float]] = []

    def set_bars(self, bars: list[tuple[float, float, float, float, float]]) -> None:
        self._bars = bars
        self._rebuild()

    def _rebuild(self) -> None:
        self._picture = QPicture()
        p = QPainter(self._picture)
        up = pg.mkBrush("#26a69a")
        down = pg.mkBrush("#ef5350")
        up_pen = pg.mkPen("#26a69a")
        down_pen = pg.mkPen("#ef5350")
        width = 0.6
        for (x, o, h, l, c) in self._bars:
            bullish = c >= o
            p.setPen(up_pen if bullish else down_pen)
            p.setBrush(up if bullish else down)
            p.drawLine(pg.QtCore.QPointF(x, l), pg.QtCore.QPointF(x, h))
            top, bot = (c, o) if bullish else (o, c)
            p.drawRect(pg.QtCore.QRectF(x - width / 2, bot, width, max(top - bot, 1e-6)))
        p.end()
        self.prepareGeometryChange()

    def paint(self, painter, *args) -> None:
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        return pg.QtCore.QRectF(self._picture.boundingRect())


class ChartPanel(QWidget):
    lines_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._candles = _CandleItem()
        self._plot.addItem(self._candles)
        self._live_line = pg.InfiniteLine(angle=0, movable=False,
                                          pen=pg.mkPen("#888", style=Qt.PenStyle.DashLine))
        self._plot.addItem(self._live_line)
        self._entry_line: pg.InfiniteLine | None = None
        self._line_a: pg.InfiniteLine | None = None
        self._line_b: pg.InfiniteLine | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._plot)

    def set_candles(self, bars: list[dict]) -> None:
        parsed: list[tuple[float, float, float, float, float]] = []
        for i, b in enumerate(bars):
            parsed.append((float(i), float(b["o"]), float(b["h"]),
                           float(b["l"]), float(b["c"])))
        self._candles.set_bars(parsed)

    def set_live_price(self, price: float) -> None:
        self._live_line.setValue(price)

    def arm_order_lines(self, *, entry: float, line_a: float, line_b: float) -> None:
        for ln in (self._entry_line, self._line_a, self._line_b):
            if ln is not None:
                self._plot.removeItem(ln)
        self._entry_line = pg.InfiniteLine(pos=entry, angle=0, movable=True,
                                           pen=pg.mkPen("#42a5f5", width=2),
                                           label="ENTRY {value:.2f}")
        self._line_a = pg.InfiniteLine(pos=line_a, angle=0, movable=True,
                                       pen=pg.mkPen("#66bb6a", width=2),
                                       label="{value:.2f}")
        self._line_b = pg.InfiniteLine(pos=line_b, angle=0, movable=True,
                                       pen=pg.mkPen("#ef5350", width=2),
                                       label="{value:.2f}")
        for ln in (self._entry_line, self._line_a, self._line_b):
            ln.sigPositionChanged.connect(lambda: self.lines_changed.emit())
            self._plot.addItem(ln)

    def is_armed(self) -> bool:
        return self._entry_line is not None

    def line_values(self) -> tuple[float, float, float]:
        if not self.is_armed():
            raise RuntimeError("order lines not armed")
        return (float(self._entry_line.value()),
                float(self._line_a.value()),
                float(self._line_b.value()))
