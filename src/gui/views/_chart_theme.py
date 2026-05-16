"""Apply the active theme to QChart instances.

Reads colors from src.gui.theme.current_palette() so the same chart
re-themes correctly when the user toggles between light and dark.
"""
from __future__ import annotations

from PySide6.QtCharts import QChart, QAbstractAxis
from PySide6.QtGui import QBrush, QColor, QPen


def _pal():
    from src.gui.theme import current_palette
    return current_palette()


def apply_dark_theme(chart: QChart, view=None) -> None:
    """Pulls colors from the active palette. Optionally also styles the
    QChartView so the area outside the plot matches the active bg."""
    pal = _pal()
    bg = QColor(pal.bg)
    chart.setBackgroundBrush(QBrush(bg))
    chart.setBackgroundPen(QPen(bg))                     # kill the default thin frame
    chart.setPlotAreaBackgroundBrush(QBrush(bg))
    chart.setPlotAreaBackgroundVisible(True)
    chart.setTitleBrush(QBrush(QColor(pal.text)))
    chart.legend().setLabelBrush(QBrush(QColor(pal.text)))
    chart.legend().setBorderColor(QColor(pal.grid))
    for axis in chart.axes():
        _theme_axis(axis, pal)
    if view is not None:
        # The QChartView is a QGraphicsView under the hood. Its background
        # is *not* covered by the chart's backgroundBrush, which is why
        # the plot/chart edges look "different" without this.
        view.setBackgroundBrush(QBrush(bg))


def theme_axis(axis: QAbstractAxis) -> None:
    _theme_axis(axis, _pal())


def _theme_axis(axis: QAbstractAxis, pal) -> None:
    axis.setLabelsBrush(QBrush(QColor(pal.text)))
    axis.setTitleBrush(QBrush(QColor(pal.text)))
    grid_pen = QPen(QColor(pal.grid))
    grid_pen.setWidth(1)
    axis.setGridLinePen(grid_pen)
    axis_pen = QPen(QColor(pal.text_muted))
    axis_pen.setWidth(1)
    axis.setLinePen(axis_pen)
