"""Reusable hover tooltip + crosshair for pyqtgraph plots.

pyqtgraph ships without a built-in "value under cursor" widget; the
standard solution is to subscribe to `scene().sigMouseMoved`, snap to
the nearest data point, and overlay a `TextItem` plus two
`InfiniteLine`s as a crosshair. We codify that here so each plot in
the app gets the same UX without duplicating the boilerplate.

API:

    tracker = HoverTracker(plot_widget, format_fn=lambda p: f"{p.label}: {p.y:+.2f}")
    tracker.set_points([HoverPoint(x=..., y=..., label="...", color=...), ...])

`format_fn` returns the HTML/plain string that goes into the tooltip
label; it's per-chart so each caller controls the format. The tracker
hides the crosshair when the cursor leaves the plot area.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

import pyqtgraph as pg
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor


@dataclass(frozen=True)
class HoverPoint:
    """One data point the tracker can snap to.

    `x`, `y` are the data coordinates the crosshair locks to.
    `label` and `extra` are passed verbatim to the format callback —
    use them for labels (e.g. date strings) or auxiliary metadata
    (e.g. claimed p_profit, sample count) that should appear in the
    tooltip alongside the bare value.
    """
    x: float
    y: float
    label: str = ""
    extra: dict | None = None


class HoverTracker:
    """Attach a crosshair-plus-tooltip to a PlotWidget.

    Reads from `self._points` updated via `set_points`. Snaps by
    x-distance because every chart we have uses x as the independent
    axis. The crosshair vertical line shows what x the cursor is over;
    the tooltip floats next to the snapped data point.

    Failure modes are silent — a missing scene or empty points list
    just hides the crosshair. The tracker must never raise into Qt's
    event loop.
    """

    def __init__(
        self,
        plot_widget: pg.PlotWidget,
        format_fn: Callable[[HoverPoint], str],
    ) -> None:
        self._plot = plot_widget
        self._format = format_fn
        self._points: list[HoverPoint] = []

        # Style: dashed neutral-color crosshair + small dark tooltip box
        # offset from the snapped point so it doesn't overlap the symbol.
        pen = pg.mkPen("#787b86", width=1, style=Qt.PenStyle.DashLine)
        self._vline = pg.InfiniteLine(angle=90, movable=False, pen=pen)
        self._hline = pg.InfiniteLine(angle=0, movable=False, pen=pen)
        # The tooltip's anchor=(0, 1) puts its top-left corner at the
        # data point; we shift it via offset so it sits above and to
        # the right rather than on top of the symbol.
        self._text = pg.TextItem(
            anchor=(0, 1),
            color="#d1d4dc",
            fill=pg.mkBrush(QColor(30, 33, 38, 230)),
            border=pg.mkPen(QColor("#2a2e39")),
        )
        # The marker dot highlights the snapped point. Bigger than the
        # series' own symbol so it reads as "selected" rather than
        # blending in.
        self._marker = pg.ScatterPlotItem(
            size=10, brush=pg.mkBrush("#5b8def"),
            pen=pg.mkPen(QColor("#ffffff"), width=1),
        )
        for item in (self._vline, self._hline, self._text, self._marker):
            self._plot.addItem(item, ignoreBounds=True)
            item.setVisible(False)
            # ZValue above bar/line plots so the crosshair doesn't get
            # painted behind the data.
            item.setZValue(100)

        # SignalProxy throttles mouse events to ~60Hz; without it the
        # snap-find loop runs every pixel of movement which can lag on
        # plots with thousands of points.
        self._proxy_move = pg.SignalProxy(
            self._plot.scene().sigMouseMoved,
            rateLimit=60,
            slot=self._on_move,
        )
        # Hide crosshair when the cursor leaves the plot. pyqtgraph's
        # scene fires `sceneRectChanged` reliably but not a
        # mouse-leave; we listen to mouse moves landing outside the
        # plot's scene bounding rect and hide there.

    def set_points(self, points: list[HoverPoint]) -> None:
        """Update the snap targets. Sorted by x so the binary-ish
        nearest lookup in `_on_move` is short."""
        self._points = sorted(points, key=lambda p: p.x)
        # Hide any stale snap from the previous data set.
        self._hide()

    def reattach(self) -> None:
        """Re-add the overlay items to the plot. Call this after a
        `PlotWidget.clear()` because clear() also removes everything
        the tracker attached on construction. Without this the
        crosshair silently disappears after the first refresh."""
        for item in (self._vline, self._hline, self._text, self._marker):
            self._plot.addItem(item, ignoreBounds=True)
            item.setVisible(False)
            item.setZValue(100)

    def _hide(self) -> None:
        for item in (self._vline, self._hline, self._text, self._marker):
            item.setVisible(False)

    def _on_move(self, evt) -> None:
        try:
            pos = evt[0]
            scene_rect = self._plot.sceneBoundingRect()
            if not scene_rect.contains(pos):
                self._hide()
                return
            if not self._points:
                self._hide()
                return
            vb = self._plot.plotItem.vb
            view_pt = vb.mapSceneToView(pos)
            target_x = float(view_pt.x())
            # Nearest-by-x. O(n) is fine — our plots have <500 points
            # and the search runs throttled at 60Hz.
            nearest = min(
                self._points, key=lambda p: abs(p.x - target_x)
            )
            label = self._format(nearest)
            self._text.setHtml(
                f"<div style='padding: 4px 8px; font-size: 11px; "
                f"font-family: -apple-system, Segoe UI, sans-serif;'>"
                f"{label}</div>"
            )
            self._text.setPos(nearest.x, nearest.y)
            self._vline.setPos(nearest.x)
            self._hline.setPos(nearest.y)
            self._marker.setData([nearest.x], [nearest.y])
            for item in (self._vline, self._hline, self._text, self._marker):
                item.setVisible(True)
        except Exception:
            # Mouse-tracking errors must never crash the GUI's event
            # loop. Hide and move on.
            self._hide()
