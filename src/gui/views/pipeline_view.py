"""PIPELINE view: per-message stage decisions + radial histogram.

Shows every message the listener has seen in the selected time window
plus a donut chart of how many landed at each of the 6 pipeline stages:
prefilter drop, trigger text, trigger embedding, triage ignored,
interpreter signal, interpreter ignore. NULL stages (pre-migration /
in-flight) surface as a 7th "unknown" bucket so orphans are visible.

Data path: SQLite directly via src.gui.services.pipeline_data. No HTTP
roundtrip — matches journal_data.py / evaluation_data.py patterns.

Filtering: clicking a chart slice or a stage chip filters the table to
that bucket. Clicking the same slice again clears the filter.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.gui.services import pipeline_data
from src.gui.services.pipeline_data import (
    BUCKET_COLORS,
    BUCKET_LABELS,
    PIPELINE_BUCKETS,
    PipelineMessage,
)
from src.gui.services.stack_registry import Stack


_WINDOWS: list[tuple[str, int | None]] = [
    ("Last hour",   1),
    ("Last 24h",    24),
    ("Last 7 days", 24 * 7),
    ("Last 30 days", 24 * 30),
    ("All time",    None),
]

# Auto-refresh cadence. Pipeline activity is bursty on a chatty channel
# so 10s keeps the view feeling live without hammering SQLite.
_REFRESH_MS = 10_000


@dataclass
class _Slice:
    bucket: str
    count: int
    start_deg: float   # Qt uses 16ths of a degree; we store plain degrees.
    span_deg: float
    color: QColor


class _DonutChart(QWidget):
    """Custom donut chart: paints one arc per bucket + click-to-filter.

    Why custom instead of QtCharts: QtCharts is an optional Qt module
    not guaranteed to be present in PySide6 wheels (frozen builds have
    failed on this before). A 60-line QPainter draw is more portable
    and lets us match the existing dashboard palette exactly.
    """

    slice_clicked = Signal(str)  # bucket name, or "" for the center
    slice_hovered = Signal(str)  # bucket name under cursor, or ""

    HOLE_RATIO = 0.55     # inner hole / outer radius — donut, not pie.
    MIN_SLICE_DEG = 1.0   # tiny slices still get a sliver so 0-count is visible from <1%

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._slices: list[_Slice] = []
        self._total: int = 0
        self._selected: str = ""
        self._hovered: str = ""
        self.setMinimumSize(280, 280)
        self.setMouseTracking(True)

    def set_counts(self, counts: dict[str, int]) -> None:
        total = sum(counts.values())
        self._total = total
        self._slices = []
        if total == 0:
            self.update()
            return
        # Stable bucket order so colors don't shuffle between refreshes.
        order = list(PIPELINE_BUCKETS) + ["unknown"]
        # Qt arcs start at 3 o'clock and go counter-clockwise. Convert
        # to "clock face" — start at 12, go clockwise — by negating the
        # span and offsetting the start.
        start = 90.0  # 12 o'clock
        for bucket in order:
            n = counts.get(bucket, 0)
            if n <= 0:
                continue
            span = (n / total) * 360.0
            if span < self.MIN_SLICE_DEG:
                span = self.MIN_SLICE_DEG
            self._slices.append(_Slice(
                bucket=bucket,
                count=n,
                start_deg=start,
                span_deg=span,
                color=QColor(BUCKET_COLORS.get(bucket, "#888888")),
            ))
            start -= span
        self.update()

    def set_selected(self, bucket: str) -> None:
        self._selected = bucket
        self.update()

    # ---- mouse ----
    def _hit_test(self, x: float, y: float) -> str:
        if self._total == 0:
            return ""
        cx, cy, r_out, r_in = self._geom()
        dx, dy = x - cx, y - cy
        dist = math.hypot(dx, dy)
        if dist < r_in or dist > r_out:
            return ""
        # atan2 returns -pi..pi with 0 at 3 o'clock, counter-clockwise.
        # Convert to "12 o'clock = 0, clockwise increasing degrees".
        ang = math.degrees(math.atan2(-dy, dx))  # 0 at 3 o'clock, up = +
        clock_deg = (90.0 - ang) % 360.0
        # Find the slice whose [start..start+span] contains clock_deg
        # (slices are laid out clockwise with start at 12 = 0deg-on-clock).
        cursor_from_12 = clock_deg  # 0 = 12 o'clock, increases clockwise
        running = 0.0
        for s in self._slices:
            end = running + s.span_deg
            if running <= cursor_from_12 < end:
                return s.bucket
            running = end
        return ""

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            bucket = self._hit_test(ev.position().x(), ev.position().y())
            self.slice_clicked.emit(bucket)
        super().mousePressEvent(ev)

    def mouseMoveEvent(self, ev) -> None:
        b = self._hit_test(ev.position().x(), ev.position().y())
        if b != self._hovered:
            self._hovered = b
            self.slice_hovered.emit(b)
            self.update()
        super().mouseMoveEvent(ev)

    def leaveEvent(self, ev) -> None:
        if self._hovered:
            self._hovered = ""
            self.slice_hovered.emit("")
            self.update()
        super().leaveEvent(ev)

    # ---- paint ----
    def _geom(self) -> tuple[float, float, float, float]:
        side = min(self.width(), self.height())
        cx = self.width() / 2.0
        cy = self.height() / 2.0
        r_out = (side / 2.0) - 12.0
        r_in = r_out * self.HOLE_RATIO
        return cx, cy, r_out, r_in

    def paintEvent(self, ev) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        cx, cy, r_out, r_in = self._geom()

        if self._total == 0:
            p.setPen(QColor("#5A7090"))
            p.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                       "No messages in this window")
            p.end()
            return

        rect = QRectF(cx - r_out, cy - r_out, 2 * r_out, 2 * r_out)
        # Qt arc angles are in 16ths of a degree; 0 is at 3 o'clock,
        # positive = counter-clockwise. We have "clockwise from 12"; the
        # _Slice.start_deg already encodes the Qt-native start (90 - run).
        p.setPen(Qt.PenStyle.NoPen)
        for s in self._slices:
            col = QColor(s.color)
            if self._selected and self._selected != s.bucket:
                col.setAlpha(80)
            elif self._hovered == s.bucket:
                # Brighten on hover.
                col = col.lighter(120)
            p.setBrush(QBrush(col))
            # Qt drawPie spans counter-clockwise from start_deg.
            # We laid slices out clockwise, so use negative span.
            p.drawPie(rect, int(round(s.start_deg * 16)),
                      int(round(-s.span_deg * 16)))

        # Knock out the center to make it a donut.
        p.setBrush(QBrush(QColor(self.palette().window().color())))
        p.drawEllipse(QRectF(cx - r_in, cy - r_in, 2 * r_in, 2 * r_in))

        # Center label: hovered bucket count + % if any, otherwise total.
        p.setPen(QColor("#E6FBFF"))
        font_big = self.font()
        font_big.setPointSize(max(14, font_big.pointSize() + 6))
        font_big.setBold(True)
        p.setFont(font_big)
        if self._hovered:
            n = next((s.count for s in self._slices if s.bucket == self._hovered), 0)
            pct = 100.0 * n / max(1, self._total)
            p.drawText(QRectF(cx - r_in, cy - 14, 2 * r_in, 28),
                       Qt.AlignmentFlag.AlignCenter,
                       f"{n}  ({pct:.0f}%)")
            font_small = self.font()
            p.setFont(font_small)
            p.setPen(QColor("#5A7090"))
            p.drawText(QRectF(cx - r_in, cy + 14, 2 * r_in, 18),
                       Qt.AlignmentFlag.AlignCenter,
                       BUCKET_LABELS.get(self._hovered, self._hovered))
        else:
            p.drawText(QRectF(cx - r_in, cy - 14, 2 * r_in, 28),
                       Qt.AlignmentFlag.AlignCenter,
                       str(self._total))
            font_small = self.font()
            p.setFont(font_small)
            p.setPen(QColor("#5A7090"))
            p.drawText(QRectF(cx - r_in, cy + 14, 2 * r_in, 18),
                       Qt.AlignmentFlag.AlignCenter,
                       "messages")
        p.end()


def _bucket_chip(bucket: str) -> QLabel:
    """Coloured pill rendering BUCKET_LABELS[bucket]."""
    lbl = QLabel(BUCKET_LABELS.get(bucket, bucket))
    color = BUCKET_COLORS.get(bucket, "#888888")
    lbl.setStyleSheet(
        f"QLabel {{ background-color: {color}; color: #07091A; "
        f"padding: 2px 8px; border-radius: 8px; "
        f"font-weight: 600; font-size: 11px; }}"
    )
    lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
    return lbl


class _LegendRow(QWidget):
    """One row in the chart legend: color square + label + count.

    Clicking the row filters the table the same as clicking the slice.
    """
    clicked = Signal(str)

    def __init__(self, bucket: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._bucket = bucket
        layout = QHBoxLayout(self)
        layout.setContentsMargins(2, 1, 2, 1)
        swatch = QLabel()
        swatch.setFixedSize(12, 12)
        swatch.setStyleSheet(
            f"background-color: {BUCKET_COLORS.get(bucket, '#888')}; "
            f"border-radius: 2px;"
        )
        layout.addWidget(swatch)
        self._label = QLabel(BUCKET_LABELS.get(bucket, bucket))
        self._label.setStyleSheet("font-size: 11px;")
        layout.addWidget(self._label, 1)
        self._count = QLabel("0")
        self._count.setStyleSheet("font-size: 11px; font-weight: 600;")
        self._count.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(self._count)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_count(self, n: int) -> None:
        self._count.setText(str(n))

    def mousePressEvent(self, ev) -> None:
        if ev.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self._bucket)
        super().mousePressEvent(ev)


class PipelineView(QWidget):
    """Top-level view registered under nav key 'pipeline'."""

    _TABLE_COLS = ("Time", "Channel", "Sender", "Stage", "Outcome", "Snippet", "Actions")

    def __init__(self, stack: Stack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._stack = stack
        self._window_hours: int | None = 24
        self._filter_stage: str | None = None
        self._messages: list[PipelineMessage] = []
        # Snapshot of (id, decided_stage) for every row currently rendered.
        # Used by _refresh_table to skip the (expensive) rebuild when the
        # data hasn't actually changed — auto-refresh runs every 10s on
        # the UI thread, so a no-op refresh on a quiet channel costs us
        # roughly the GROUP-BY query plus a tuple compare, instead of
        # tearing down 500 QTableWidgetItems.
        self._row_snapshot: tuple[tuple[int, str | None], ...] = ()
        # Selected message id, preserved across refreshes so the user's
        # selection survives the auto-refresh tick. None when nothing is
        # selected. Drives the post-rebuild re-selection logic.
        self._selected_msg_id: int | None = None

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 8, 8, 8)
        root.setSpacing(8)

        # ---- header bar: window selector + filter + refresh ----
        header = QHBoxLayout()
        header.setSpacing(8)
        title = QLabel("Pipeline")
        title.setStyleSheet("font-size: 18px; font-weight: 700;")
        header.addWidget(title)
        header.addSpacing(16)

        self._window_combo = QComboBox()
        for label, _ in _WINDOWS:
            self._window_combo.addItem(label)
        self._window_combo.setCurrentIndex(1)  # Last 24h
        self._window_combo.currentIndexChanged.connect(self._on_window_changed)
        header.addWidget(self._window_combo)

        self._filter_label = QLabel("All stages")
        self._filter_label.setStyleSheet("color: #5A7090;")
        header.addWidget(self._filter_label)

        self._clear_btn = QPushButton("Clear filter")
        self._clear_btn.setVisible(False)
        self._clear_btn.clicked.connect(lambda: self._set_filter(None))
        header.addWidget(self._clear_btn)

        header.addStretch(1)
        refresh = QPushButton("Refresh")
        refresh.clicked.connect(self._refresh)
        header.addWidget(refresh)
        root.addLayout(header)

        # ---- chart + legend ----
        chart_row = QHBoxLayout()
        chart_row.setSpacing(12)
        self._chart = _DonutChart()
        self._chart.slice_clicked.connect(self._on_slice_clicked)
        chart_row.addWidget(self._chart, 2)

        legend = QFrame()
        legend.setFrameShape(QFrame.Shape.NoFrame)
        legend_layout = QVBoxLayout(legend)
        legend_layout.setContentsMargins(8, 8, 8, 8)
        legend_layout.setSpacing(4)
        legend_title = QLabel("Stages")
        legend_title.setStyleSheet("font-weight: 600; font-size: 12px;")
        legend_layout.addWidget(legend_title)
        self._legend_rows: dict[str, _LegendRow] = {}
        for b in list(PIPELINE_BUCKETS) + ["unknown"]:
            row = _LegendRow(b)
            row.clicked.connect(self._on_slice_clicked)
            self._legend_rows[b] = row
            legend_layout.addWidget(row)
        legend_layout.addStretch(1)
        chart_row.addWidget(legend, 1)
        root.addLayout(chart_row)

        # ---- split: table on top, detail panel on bottom ----
        splitter = QSplitter(Qt.Orientation.Vertical)
        splitter.setChildrenCollapsible(False)

        self._table = QTableWidget(0, len(self._TABLE_COLS))
        self._table.setHorizontalHeaderLabels(self._TABLE_COLS)
        self._table.verticalHeader().setVisible(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        hh = self._table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(3, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        hh.setSectionResizeMode(5, QHeaderView.ResizeMode.Stretch)
        hh.setSectionResizeMode(6, QHeaderView.ResizeMode.ResizeToContents)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        splitter.addWidget(self._table)

        self._detail = QPlainTextEdit()
        self._detail.setReadOnly(True)
        self._detail.setPlaceholderText(
            "Select a message to see its full text + pipeline metadata "
            "(triage latency, embedding score, raw LLM response)."
        )
        splitter.addWidget(self._detail)
        splitter.setSizes([520, 220])
        root.addWidget(splitter, 1)

        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(self._refresh)
        self._refresh_timer.start(_REFRESH_MS)
        self._refresh()

    # ---- stack switching -------------------------------------------

    def rebind(self, stack: Stack) -> None:
        """Called by MainWindow when the header switcher changes stack.

        Each stack is a separate SQLite install with its own messages
        table — flipping the switcher must repoint our reads at the new
        db_path. Clear the in-memory filter + selection so the operator
        doesn't see stale rows from the previous stack flashing through.
        """
        self._stack = stack
        self._filter_stage = None
        self._filter_label.setText("All stages")
        self._clear_btn.setVisible(False)
        self._chart.set_selected("")
        self._selected_msg_id = None
        self._row_snapshot = ()
        self._refresh()

    # ---- handlers --------------------------------------------------

    def _on_window_changed(self, idx: int) -> None:
        _, hours = _WINDOWS[idx]
        self._window_hours = hours
        self._selected_msg_id = None
        self._row_snapshot = ()  # force a rebuild
        self._refresh()

    def _on_slice_clicked(self, bucket: str) -> None:
        # Toggle: clicking the same slice clears the filter.
        if not bucket:
            return
        if self._filter_stage == bucket:
            self._set_filter(None)
        else:
            self._set_filter(bucket)

    def _set_filter(self, stage: str | None) -> None:
        self._filter_stage = stage
        self._selected_msg_id = None
        self._row_snapshot = ()  # force a rebuild
        if stage is None:
            self._filter_label.setText("All stages")
            self._clear_btn.setVisible(False)
        else:
            self._filter_label.setText(
                "Filtered: " + BUCKET_LABELS.get(stage, stage)
            )
            self._clear_btn.setVisible(True)
        self._chart.set_selected(stage or "")
        self._refresh_table()

    def _on_row_selected(self) -> None:
        rows = self._table.selectionModel().selectedRows()
        if not rows:
            self._selected_msg_id = None
            self._detail.clear()
            return
        idx = rows[0].row()
        if idx < 0 or idx >= len(self._messages):
            return
        m = self._messages[idx]
        self._selected_msg_id = m.id
        lines = [
            f"Message #{m.id}  ({m.received_at})",
            f"From: {m.sender or '-'}    Channel: {m.source_channel_id or '-'}",
            "",
            "TEXT",
            "-" * 60,
            m.text,
            "",
            "DECISION",
            "-" * 60,
            f"stage:     {m.decided_stage or '(none — still in flight or pre-migration)'}",
            f"outcome:   {m.decided_outcome or '-'}",
            f"decided:   {m.decided_at or '-'}",
            f"actions:   {', '.join(str(a) for a in m.action_ids) or '-'}",
        ]
        if m.pipeline_meta:
            import json as _json
            lines.append("")
            lines.append("META")
            lines.append("-" * 60)
            lines.append(_json.dumps(m.pipeline_meta, indent=2, ensure_ascii=False))
        self._detail.setPlainText("\n".join(lines))

    # ---- refresh ---------------------------------------------------

    def _refresh(self) -> None:
        db = self._stack.db_path
        try:
            counts = pipeline_data.bucket_counts(db, self._window_hours)
        except Exception as e:
            counts = {b: 0 for b in PIPELINE_BUCKETS}
            counts["unknown"] = 0
            self._detail.setPlainText(f"pipeline stats failed: {e!r}")
        self._chart.set_counts(counts)
        for bucket, row in self._legend_rows.items():
            row.set_count(counts.get(bucket, 0))
        self._refresh_table()

    def _refresh_table(self) -> None:
        db = self._stack.db_path
        try:
            new_messages = pipeline_data.messages(
                db,
                since_hours=self._window_hours,
                stage=self._filter_stage,
                limit=500,
            )
        except Exception as e:
            self._messages = []
            self._row_snapshot = ()
            self._detail.setPlainText(f"pipeline messages failed: {e!r}")
            self._table.setRowCount(0)
            return

        # Quick equality check: if the (id, decided_stage) tuples haven't
        # changed since the last render, skip the rebuild entirely. This
        # is the common case on a quiet channel — auto-refresh fires
        # every 10s and the rows are identical to last tick, so we'd
        # otherwise clobber the user's selection and burn ~20ms per tick
        # for nothing. decided_stage is included because an in-flight
        # row's stage can flip from NULL → "interpreted_signal" without
        # the id changing.
        snapshot = tuple((m.id, m.decided_stage) for m in new_messages)
        if snapshot == self._row_snapshot and len(self._messages) == len(new_messages):
            self._messages = new_messages
            return

        self._messages = new_messages
        self._row_snapshot = snapshot

        # Block selection signals during rebuild so we don't emit a
        # spurious "selection cleared" while populating.
        self._table.blockSignals(True)
        try:
            self._table.setRowCount(len(self._messages))
            for r, m in enumerate(self._messages):
                time_short = (m.received_at or "")[:19].replace("T", " ")
                self._table.setItem(r, 0, QTableWidgetItem(time_short))
                self._table.setItem(r, 1, QTableWidgetItem(m.source_channel_id or "-"))
                self._table.setItem(r, 2, QTableWidgetItem(m.sender or "-"))

                stage_key = m.decided_stage or "unknown"
                stage_item = QTableWidgetItem(BUCKET_LABELS.get(stage_key, stage_key))
                stage_item.setForeground(QColor(BUCKET_COLORS.get(stage_key, "#888")))
                self._table.setItem(r, 3, stage_item)

                self._table.setItem(r, 4, QTableWidgetItem(m.decided_outcome or "-"))

                snippet = (m.text or "").replace("\n", " ")
                if len(snippet) > 120:
                    snippet = snippet[:117] + "…"
                self._table.setItem(r, 5, QTableWidgetItem(snippet))

                self._table.setItem(
                    r, 6,
                    QTableWidgetItem(str(len(m.action_ids)) if m.action_ids else "0"),
                )
        finally:
            self._table.blockSignals(False)

        # Restore selection by message id when possible. Falls back to
        # clearing if the previously selected message scrolled off the
        # current window/filter.
        if self._selected_msg_id is not None:
            target_row = next(
                (i for i, m in enumerate(self._messages)
                 if m.id == self._selected_msg_id),
                -1,
            )
            if target_row >= 0:
                self._table.selectRow(target_row)
            else:
                self._selected_msg_id = None
                self._detail.clear()
