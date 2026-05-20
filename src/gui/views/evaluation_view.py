"""EVALUATION view: three sub-tabs comparing evaluator output vs realized outcomes.

  - Per-Trade — table of closed trades with their evaluation; click a
    row to see the full evaluation dict (axes, probabilities, edge
    thesis, key risks, context excerpt).
  - Calibration — pyqtgraph bar chart: claimed score band vs ACTUAL
    win rate. The slope tells you whether the evaluator is calibrated
    (45-degree line = perfect), miscalibrated high (overconfident), or
    miscalibrated low. Needs ~50 closed trades to mean anything.
  - Regime — table grouped by regime tag, showing per-regime win rate +
    avg score. Highest-leverage diagnostic: which conditions the
    evaluator nails and which it misreads.
"""
from __future__ import annotations

import json
from pathlib import Path

import pyqtgraph as pg
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
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
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.evaluation_data import (
    CalibrationBucket,
    CalibrationStatus,
    EvaluatedTrade,
    RegimeBucket,
    calibration_buckets,
    calibration_status,
    evaluated_trades,
    regime_buckets,
)
from src.gui.services.stack_registry import Stack


_RANGES: list[tuple[str, int | None]] = [
    ("Today", 0),
    ("Last 7 days", 7),
    ("Last 30 days", 30),
    ("Last 90 days", 90),
    ("All time", None),
]


# Auto-refresh cadence. The view is forensic so we don't need sub-second
# updates; 30s keeps the table fresh as new trades close without
# thrashing the DB.
_REFRESH_MS = 30_000


class EvaluationView(QWidget):
    """Top-level Evaluation tab. Three sub-tabs share one trade list."""

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._days: int | None = 30
        self._trades: list[EvaluatedTrade] = []
        self._build_ui()
        self.refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(_REFRESH_MS)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()

    def rebind(self, stack: Stack) -> None:
        """MainWindow calls this on stack switch — same pattern as the
        other views (LiveView, JournalView, etc.)."""
        self._stack = stack
        self.refresh()

    def refresh(self) -> None:
        """Pull fresh data + re-render all three sub-tabs.

        Called on a timer (30s), on stack switch, on the range combo
        change, and on the Refresh button. Single query per refresh —
        the three sub-tabs read the same `self._trades` list.
        """
        self._trades = evaluated_trades(self._stack.db_path, days=self._days)
        self._calibration_banner.populate(calibration_status(self._trades))
        self._per_trade.populate(self._trades)
        self._calibration.populate(calibration_buckets(self._trades))
        self._regime.populate(regime_buckets(self._trades))
        self._update_stats_label()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel(
            "<span style='font-size:16px; font-weight:700;'>EVALUATION</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        try:
            from src.gui.panels._a11y import mark_heading
            mark_heading(title, "Evaluation")
        except Exception:
            pass
        top.addWidget(title)
        top.addSpacing(16)
        top.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, days in _RANGES:
            self._range_combo.addItem(label, days)
        self._range_combo.setCurrentIndex(2)  # Last 30 days
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        top.addWidget(self._range_combo)
        top.addStretch()
        self._stats_label = QLabel("")
        self._stats_label.setStyleSheet("color: #787b86;")
        top.addWidget(self._stats_label)
        top.addSpacing(8)
        refresh_btn = QPushButton("Refresh")
        refresh_btn.clicked.connect(self.refresh)
        top.addWidget(refresh_btn)
        layout.addLayout(top)

        # Calibration banner — colored by band, populated on every
        # refresh. Replaces the static hint: same purpose (tell the
        # operator what calibration state we're in) but reactive,
        # quantitative, and color-coded.
        self._calibration_banner = _CalibrationBanner()
        layout.addWidget(self._calibration_banner)

        self._tabs = QTabWidget()
        self._per_trade = _PerTradeTab()
        self._calibration = _CalibrationTab()
        self._regime = _RegimeTab()
        self._tabs.addTab(self._per_trade, "Per-trade")
        self._tabs.addTab(self._calibration, "Calibration")
        self._tabs.addTab(self._regime, "Regime breakdown")
        layout.addWidget(self._tabs, 1)

    def _on_range_changed(self, index: int) -> None:
        self._days = self._range_combo.itemData(index)
        self.refresh()

    def _update_stats_label(self) -> None:
        n = len(self._trades)
        scored = sum(1 for t in self._trades if t.evaluation is not None)
        wins = sum(1 for t in self._trades if t.realized_pnl > 0)
        wr = (wins / n * 100) if n > 0 else 0.0
        self._stats_label.setText(
            f"{n} trades · {scored} scored · win rate {wr:.0f}%"
        )


# ---- Calibration banner ---------------------------------------------


# Per-band styling for the calibration banner. Each tuple is
# (border_color, background_rgba, icon_glyph, headline_label).
# `headline_label` shows in big caps; the dynamic message from
# `CalibrationStatus.message` is the prose underneath.
_BANNER_STYLES: dict[str, tuple[str, str, str, str]] = {
    "no_data": (
        "#787b86", "rgba(120,123,134,0.10)", "ℹ",
        "NO CALIBRATION DATA YET",
    ),
    "uncalibrated": (
        "#ef5350", "rgba(239,83,80,0.10)", "⚠",
        "UNCALIBRATED — TREAT AS PRIORS",
    ),
    "partial": (
        "#f6c453", "rgba(246,196,83,0.10)", "△",
        "PARTIAL CALIBRATION",
    ),
    "calibrating": (
        "#26a69a", "rgba(38,166,154,0.10)", "✓",
        "CALIBRATION DATA READY",
    ),
}


class _CalibrationBanner(QFrame):
    """Colored status bar above the sub-tabs. Tells the operator
    whether the probabilities they're about to read are statistically
    meaningful or still LLM priors.

    Hidden when the view has no DB path yet (e.g. early startup with
    no stack discovered); shown otherwise even at zero trades — the
    "NO DATA" state is itself useful information.
    """

    def __init__(self) -> None:
        super().__init__()
        self.setFrameShape(QFrame.Shape.NoFrame)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(10)
        self._icon = QLabel()
        self._icon.setStyleSheet("font-size: 18px; font-weight: 700;")
        layout.addWidget(self._icon, 0, Qt.AlignmentFlag.AlignTop)
        text_wrap = QVBoxLayout()
        text_wrap.setSpacing(2)
        self._headline = QLabel()
        self._headline.setStyleSheet(
            "font-size: 11px; font-weight: 700; letter-spacing: 1.5px;"
        )
        text_wrap.addWidget(self._headline)
        self._message = QLabel()
        self._message.setWordWrap(True)
        self._message.setStyleSheet("font-size: 12px;")
        text_wrap.addWidget(self._message)
        layout.addLayout(text_wrap, 1)

    def populate(self, status: CalibrationStatus) -> None:
        border, bg, icon, headline = _BANNER_STYLES.get(
            status.band, _BANNER_STYLES["no_data"]
        )
        # The frame's background only applies to the QFrame itself —
        # child QLabels inherit the global theme's opaque bg unless we
        # explicitly mark them transparent. Without the QLabel rule
        # below, icon/text show on a black rectangle even when the
        # banner is red.
        self.setStyleSheet(
            f"_CalibrationBanner {{ "
            f"background: {bg}; "
            f"border-left: 4px solid {border}; "
            f"border-radius: 4px; "
            f"}}"
            f"_CalibrationBanner QLabel {{ background: transparent; }}"
        )
        # Icon: color matches the border. Background must stay
        # transparent — the global rule above handles it, but we keep
        # the explicit one here so a future per-icon style edit
        # doesn't accidentally reintroduce the opaque bg.
        self._icon.setStyleSheet(
            f"font-size: 22px; font-weight: 700; color: {border}; "
            f"background: transparent;"
        )
        self._icon.setText(icon)
        self._headline.setStyleSheet(
            f"font-size: 11px; font-weight: 700; letter-spacing: 1.5px; "
            f"color: {border}; background: transparent;"
        )
        self._headline.setText(headline)
        self._message.setStyleSheet("font-size: 12px; background: transparent;")
        self._message.setText(status.message)


# ---- Per-trade ------------------------------------------------------


class _PerTradeTab(QWidget):
    """Two-pane: table on left, full evaluation JSON on the right."""

    _HEADERS = (
        "When", "Type", "Side", "Score", "Verdict", "Multiplier",
        "P&L", "Close reason", "Hold (min)",
    )

    # Per-column weights summing to 100. Distribution loosely matches
    # the typical content size each cell holds — "Close reason" gets
    # the most room because reasons like "sl_too_wide_for_max_risk_pct"
    # are long; "Side" gets the least because it's BUY/SELL.
    # Sum verified at startup; if you tweak these, the constraint check
    # in `_apply_column_weights` will warn at runtime.
    _COLUMN_WEIGHTS: tuple[int, ...] = (
        14,  # When
        11,  # Type
         6,  # Side
         7,  # Score
        10,  # Verdict
        10,  # Multiplier
         9,  # P&L
        23,  # Close reason
        10,  # Hold (min)
    )

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[EvaluatedTrade] = []
        # Tracks which action_id the detail pane is currently rendering.
        # Used to skip a redundant setHtml when the selection survives
        # a refresh — without this, the detail pane's scroll position
        # jumps to the top every refresh tick.
        self._current_action_id: int | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(0)
        splitter = QSplitter(Qt.Orientation.Horizontal)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        # Columns stretch to fill the available width, with per-column
        # weights proportional to the content size each one typically
        # holds. Implemented via Interactive resize mode + a
        # resize-handler that redistributes width on every layout pass.
        # `Stretch` mode would equalize widths instead of weighting them.
        hdr = self._table.horizontalHeader()
        hdr.setStretchLastSection(False)
        for i in range(len(self._HEADERS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.Interactive)
        self._table.itemSelectionChanged.connect(self._on_selection_changed)
        # Wrap resizeEvent so the proportional layout updates whenever
        # the splitter or window resizes. Defined inline because the
        # only state it needs is `self` and the column weights below.
        original_resize = self._table.resizeEvent

        def _on_table_resize(ev) -> None:
            original_resize(ev)
            self._apply_column_weights()

        self._table.resizeEvent = _on_table_resize  # type: ignore[assignment]
        splitter.addWidget(self._table)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(8, 0, 0, 0)
        right_layout.setSpacing(6)

        header_bar = QHBoxLayout()
        header = QLabel("<b>Evaluation detail</b>")
        header.setTextFormat(Qt.TextFormat.RichText)
        header_bar.addWidget(header)
        header_bar.addStretch()
        # Toggle between the readable HTML view and the raw JSON dump.
        # Default readable; the toggle persists per-tab session, not
        # globally — the operator can pop into JSON for one trade
        # without committing to it for everything.
        self._raw_toggle = QPushButton("Raw JSON")
        self._raw_toggle.setCheckable(True)
        self._raw_toggle.toggled.connect(self._on_raw_toggled)
        self._raw_toggle.setToolTip(
            "Toggle between the readable summary and the raw evaluation JSON."
        )
        header_bar.addWidget(self._raw_toggle)
        right_layout.addLayout(header_bar)

        # Two stacked widgets — only one visible at a time. QTextBrowser
        # for the readable HTML view (renders <h*>, <table>, <ul>, css);
        # QPlainTextEdit for the raw JSON (monospace, no rendering).
        self._detail_html = QTextBrowser()
        self._detail_html.setOpenExternalLinks(False)
        self._detail_html.setPlaceholderText(
            "Select a row to see its evaluation."
        )
        right_layout.addWidget(self._detail_html, 1)

        self._detail_raw = QPlainTextEdit()
        self._detail_raw.setReadOnly(True)
        self._detail_raw.setStyleSheet("font-family: Consolas, monospace;")
        self._detail_raw.hide()
        right_layout.addWidget(self._detail_raw, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        layout.addWidget(splitter, 1)

    def _apply_column_weights(self) -> None:
        """Distribute table width across columns by `_COLUMN_WEIGHTS`.
        Called on every resizeEvent so columns track the splitter.

        Why Interactive + manual widths instead of `Stretch`: stretch
        equalizes every column, which makes short columns ("Side")
        identical in width to long ones ("Close reason"). Weighted
        widths look like a real product, not a 9-equal-cells grid.
        """
        # Account for the vertical scroll bar's width so the rightmost
        # column doesn't get cropped when the table has more rows than
        # fit. viewport().width() handles this automatically.
        total = self._table.viewport().width()
        weight_sum = sum(self._COLUMN_WEIGHTS)
        if total <= 0 or weight_sum <= 0:
            return
        for col, w in enumerate(self._COLUMN_WEIGHTS):
            self._table.setColumnWidth(col, max(40, total * w // weight_sum))

    def populate(self, trades: list[EvaluatedTrade]) -> None:
        # Preserve selection AND scroll position AND detail-pane
        # content across refreshes. blockSignals wraps the ENTIRE
        # rebuild because setRowCount(0) fires a selection-cleared
        # event before we get a chance to re-select; the unblocked
        # event would run _on_selection_changed and wipe the detail
        # panes. Earlier code only blocked around selectRow, which was
        # too late.
        prev_action_id: int | None = None
        sel = self._table.selectedItems()
        if sel:
            prev_row = sel[0].row()
            if 0 <= prev_row < len(self._rows):
                prev_action_id = self._rows[prev_row].action_id

        prev_scroll = self._table.verticalScrollBar().value()

        self._table.blockSignals(True)
        try:
            self._rows = trades
            self._table.setRowCount(0)
            for t in trades:
                row_idx = self._table.rowCount()
                self._table.insertRow(row_idx)
                self._fill_row(row_idx, t)
            self._table.resizeRowsToContents()
            self._apply_column_weights()

            still_present = False
            if prev_action_id is not None:
                for i, t in enumerate(trades):
                    if t.action_id == prev_action_id:
                        self._table.selectRow(i)
                        still_present = True
                        break

            if not still_present:
                # Selection didn't survive (either nothing was selected
                # to begin with, or the row's gone). Clear the detail
                # panes explicitly because signals are blocked and
                # _on_selection_changed wouldn't fire on its own.
                self._current_action_id = None
                self._detail_html.clear()
                self._detail_raw.clear()
        finally:
            self._table.blockSignals(False)

        # Restore scroll bar after the table has been repopulated. Done
        # last because setRowCount(0) + repopulate can momentarily
        # change the scroll range; setting the value too early gets
        # clamped to the new range.
        self._table.verticalScrollBar().setValue(prev_scroll)

    def _fill_row(self, row_idx: int, t: EvaluatedTrade) -> None:
        # Closed_at, truncated to "MM-DD HH:MM" — full ISO too wide for
        # the column.
        when = t.closed_at[5:16].replace("T", " ") if t.closed_at else ""
        eval_data = t.evaluation or {}
        score = eval_data.get("score", "—")
        verdict = eval_data.get("verdict", "—")
        sizing = eval_data.get("sizing") or {}
        mult = sizing.get("multiplier")
        mult_str = f"{mult:.2f}x" if isinstance(mult, (int, float)) else "—"
        pnl_str = f"{t.realized_pnl:+.2f}"
        hold_str = str(t.hold_minutes) if t.hold_minutes is not None else "—"

        cells = (
            when, t.action_type, t.side, str(score), str(verdict), mult_str,
            pnl_str, t.close_reason, hold_str,
        )
        for col, val in enumerate(cells):
            item = QTableWidgetItem(val)
            if col == 6:  # P&L column — color by sign for quick scan
                if t.realized_pnl > 0:
                    item.setForeground(QColor("#26a69a"))
                elif t.realized_pnl < 0:
                    item.setForeground(QColor("#ef5350"))
            self._table.setItem(row_idx, col, item)

    def _on_selection_changed(self) -> None:
        sel = self._table.selectedItems()
        if not sel:
            self._current_action_id = None
            self._detail_html.clear()
            self._detail_raw.clear()
            return
        row = sel[0].row()
        if not (0 <= row < len(self._rows)):
            return
        t = self._rows[row]
        # Skip the re-render when the selected action_id is already the
        # one the detail pane is showing. setHtml on a QTextBrowser
        # resets the scroll bar to 0 and discards selection, which made
        # the detail pane appear to "jump to top" on every refresh tick
        # even though the operator hadn't moved.
        if t.action_id == self._current_action_id:
            return
        self._current_action_id = t.action_id
        self._detail_html.setHtml(_render_trade_html(t))
        try:
            pretty = json.dumps(t.evaluation or {}, indent=2, ensure_ascii=False)
        except (TypeError, ValueError):
            pretty = repr(t.evaluation)
        self._detail_raw.setPlainText(_raw_header(t) + pretty)

    def _on_raw_toggled(self, checked: bool) -> None:
        """Swap which detail widget is visible. Selection state on the
        table is unchanged, so a fresh `_on_selection_changed` would
        re-populate both panes anyway — but we don't need to redraw
        here; just show/hide."""
        if checked:
            self._detail_html.hide()
            self._detail_raw.show()
            self._raw_toggle.setText("Readable")
        else:
            self._detail_raw.hide()
            self._detail_html.show()
            self._raw_toggle.setText("Raw JSON")


# ---- Detail rendering -----------------------------------------------
#
# The right pane of the Per-trade tab. Renders the evaluation dict as
# structured HTML rather than raw JSON so a human can read it at a
# glance. The Raw JSON toggle in the same pane swaps to the canonical
# json.dumps for when forensics needs the literal bytes.


def _raw_header(t: EvaluatedTrade) -> str:
    """Single text block summarizing the trade — same content used in
    both the readable and the raw-JSON views. Kept above the JSON dump
    in the raw pane so the operator can correlate the JSON with the
    position without scrolling up."""
    return (
        f"ticket={t.ticket}  action_id={t.action_id}  {t.action_type}\n"
        f"opened: {t.opened_at}\n"
        f"closed: {t.closed_at}  ({t.close_reason})\n"
        f"realized_pnl: {t.realized_pnl:+.2f}\n"
        f"hold: {t.hold_minutes} min\n"
        + ("─" * 60) + "\n"
    )


def _html_escape(s: object) -> str:
    """Minimal HTML escaping. QTextBrowser is forgiving but we don't
    want a `<script>` tag from a malicious LLM response to render."""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


# Verdict -> color (matches the existing dashboard banding).
_VERDICT_COLORS = {
    "strong":      "#26a69a",
    "moderate":    "#f6c453",
    "weak":        "#ef9a3e",
    "avoid":       "#ef5350",
    "unavailable": "#787b86",
}


def _verdict_color(verdict: str) -> str:
    return _VERDICT_COLORS.get(str(verdict).lower(), "#787b86")


def _format_pct(v: object, places: int = 0) -> str:
    """Format a 0..1 probability as a percentage string."""
    try:
        return f"{float(v) * 100:.{places}f}%"
    except (TypeError, ValueError):
        return "—"


def _render_trade_html(t: EvaluatedTrade) -> str:
    """Build the readable HTML for a single evaluated trade.

    Sections (in display order):
      1. Header line with ticket / action / P&L color
      2. Score + verdict prominent badge
      3. Direction, style, style source
      4. Regime tag broken into its 5 components
      5. Axis scores table with rationales
      6. Vetoes (if any) — red banner
      7. Probability by horizon (when synthesizer ran)
      8. Expected MAE / MFE / R:R
      9. Edge thesis blockquote
     10. Key risks list
     11. Sizing decision
     12. Data quality / missing features
     13. Context excerpt (collapsed look — small font)

    The renderer is defensive: every nested .get() tolerates missing
    keys so a partial or legacy evaluation still produces sensible
    output rather than blowing up.
    """
    if t.evaluation is None:
        return _render_no_eval_html(t)

    e = t.evaluation
    score = e.get("score", "—")
    verdict = e.get("verdict", "—")
    score_color = _verdict_color(verdict)

    pnl_color = "#26a69a" if t.realized_pnl > 0 else (
        "#ef5350" if t.realized_pnl < 0 else "#787b86"
    )

    parts: list[str] = []

    # 1. Header
    parts.append(
        f"<div style='font-family: -apple-system, Segoe UI, sans-serif;'>"
        f"<div style='color:#787b86; font-size:11px;'>"
        f"ticket {_html_escape(t.ticket)} · action #{_html_escape(t.action_id)} "
        f"· {_html_escape(t.action_type)} {_html_escape(t.side)}"
        f"</div>"
        f"<div style='color:#787b86; font-size:11px; margin-top:2px;'>"
        f"opened {_html_escape(t.opened_at[:19].replace('T',' '))} · "
        f"closed {_html_escape(t.closed_at[:19].replace('T',' '))} · "
        f"hold {t.hold_minutes if t.hold_minutes is not None else '—'} min · "
        f"close: {_html_escape(t.close_reason or '—')}"
        f"</div>"
        f"<div style='font-size:18px; font-weight:700; margin-top:8px; "
        f"color:{pnl_color};'>"
        f"P&amp;L {t.realized_pnl:+.2f}"
        f"</div>"
    )

    # 2. Score & verdict card. Also surfaces `key_factor` and `summary`
    # — these are the operator-facing tl;dr that the EA dashboard reads
    # as the headline. The score card inherits the panel bg (no rgba
    # fill) and uses a colored left border for visual emphasis —
    # tinted backgrounds rendered as opaque rectangles against the
    # QTextBrowser's bg color on dark themes, which the operator
    # flagged as discontinuous.
    key_factor = e.get("key_factor") or ""
    summary = e.get("summary") or ""
    parts.append(
        f"<div style='margin-top:14px; padding:8px 0 8px 14px; "
        f"border-left:4px solid {score_color};'>"
        f"<span style='font-size:11px; color:#787b86; letter-spacing:1px;'>SCORE</span>"
        f"<div style='font-size:28px; font-weight:700; color:{score_color}; "
        f"line-height:1.1;'>"
        f"{_html_escape(score)} <span style='font-size:13px; font-weight:500;'>"
        f"({_html_escape(verdict)})</span></div>"
    )
    if key_factor:
        parts.append(
            f"<div style='margin-top:8px; font-size:13px; color:#d1d4dc;'>"
            f"<span style='color:#787b86; font-size:11px; letter-spacing:1px;'>"
            f"KEY FACTOR</span><br>"
            f"{_html_escape(key_factor)}"
            f"</div>"
        )
    if summary:
        parts.append(
            f"<div style='margin-top:8px; font-size:12px; color:#787b86; "
            f"font-family: Consolas, monospace; line-height:1.5;'>"
            f"{_html_escape(summary)}"
            f"</div>"
        )
    parts.append("</div>")

    # 3. Direction & style. Surfaces both `effective_style` (what the
    # evaluator actually used) and the bare `style` (what was emitted
    # from the composer) so AI-override events are visible — the two
    # diverge only when the per-signal override fires.
    effective_style = e.get("effective_style") or e.get("style") or "—"
    bare_style = e.get("style")
    style_src = e.get("style_source") or "—"
    direction = e.get("direction") or t.side
    ver = e.get("evaluator_version", "v1")
    style_rows = [
        f"<tr><td style='color:#787b86;'>Direction</td><td>{_html_escape(direction)}</td></tr>",
        f"<tr><td style='color:#787b86;'>Effective style</td><td>{_html_escape(effective_style)}</td></tr>",
    ]
    # Only print the bare style row when it differs from effective_style
    # — typically only when style_source starts with `ai_override:`.
    if bare_style and bare_style != effective_style:
        style_rows.append(
            f"<tr><td style='color:#787b86;'>Composer style</td>"
            f"<td>{_html_escape(bare_style)}</td></tr>"
        )
    style_rows.append(
        f"<tr><td style='color:#787b86;'>Style source</td><td>{_html_escape(style_src)}</td></tr>"
    )
    style_rows.append(
        f"<tr><td style='color:#787b86;'>Evaluator</td><td>{_html_escape(ver)}</td></tr>"
    )
    parts.append(
        "<h4 style='margin-top:18px; margin-bottom:6px;'>Direction &amp; style</h4>"
        "<table cellspacing='0' cellpadding='4'>"
        + "".join(style_rows)
        + "</table>"
    )

    # 4. Regime
    regime = e.get("regime") or {}
    if isinstance(regime, dict) and regime:
        parts.append("<h4 style='margin-top:14px; margin-bottom:6px;'>Regime</h4>")
        rows = []
        for key, label in (
            ("macro", "Macro"),
            ("vol", "Volatility"),
            ("trend", "Trend"),
            ("session", "Session"),
            ("catalyst", "Catalyst"),
        ):
            v = regime.get(key, "—")
            rows.append(
                f"<tr><td style='color:#787b86;'>{label}</td>"
                f"<td>{_html_escape(v)}</td></tr>"
            )
        parts.append("<table cellspacing='0' cellpadding='4'>" + "".join(rows) + "</table>")

    # 5. Axis scores. Iterates whatever keys are present in `factors`
    # — v2 emits the 5 family keys (TREND/LEVELS/REGIME/MACRO/CATALYST);
    # v1 emits 15 sub-axes (T1..T4, L1..L3, M1..M3, G1..G3, C1..C2).
    # Both render correctly by reading the shape rather than hardcoding
    # one taxonomy. Preserves preferred ordering when present, falls
    # back to dict-insertion order otherwise.
    factors = e.get("factors") or {}
    if isinstance(factors, dict) and factors:
        parts.append("<h4 style='margin-top:14px; margin-bottom:6px;'>Axis scores</h4>")
        preferred_order = (
            "TREND", "LEVELS", "REGIME", "MACRO", "CATALYST",  # v2
            "T1", "T2", "T3", "T4",                            # v1 trend
            "L1", "L2", "L3",                                  # v1 levels
            "M1", "M2", "M3",                                  # v1 market state
            "G1", "G2", "G3",                                  # v1 macro
            "C1", "C2",                                        # v1 context
        )
        sorted_keys = [k for k in preferred_order if k in factors]
        # Tail: any unknown keys (forward-compat with future axes) in
        # insertion order.
        sorted_keys += [k for k in factors.keys() if k not in preferred_order]
        rows = []
        for key in sorted_keys:
            raw = str(factors[key])
            # Expected shape: "+0.50  rationale text". The leading
            # number may be 4-6 chars depending on sign + magnitude;
            # split at the first run of whitespace after a digit/sign
            # so "+0.50  rationale" and "-0.04 reason" both parse.
            score_str = raw[:6].strip()
            rationale = raw[6:].strip() if len(raw) > 6 else ""
            try:
                sv = float(score_str)
                col = (
                    "#26a69a" if sv > 0.1
                    else "#ef5350" if sv < -0.1
                    else "#787b86"
                )
            except ValueError:
                # v1 sometimes uses "data_quality_limited: ..." as the
                # whole string — no numeric score to render.
                col = "#787b86"
                score_str = "—"
                rationale = raw
            rows.append(
                f"<tr>"
                f"<td style='color:#787b86; padding-right:10px; "
                f"font-family: Consolas, monospace;'>"
                f"{_html_escape(key)}</td>"
                f"<td style='color:{col}; font-weight:600;'>{_html_escape(score_str)}</td>"
                f"<td style='color:#d1d4dc; padding-left:10px;'>{_html_escape(rationale)}</td>"
                f"</tr>"
            )
        parts.append(
            "<table cellspacing='0' cellpadding='4'>" + "".join(rows) + "</table>"
        )

    # 6. Vetoes
    vetoes = e.get("vetoes") or []
    if vetoes:
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>Vetoes</h4>"
            "<div style='padding:6px 0 6px 12px; "
            "border-left:3px solid #ef5350; color:#ef5350;'>"
            + "<br>".join(f"⚠ {_html_escape(v)}" for v in vetoes)
            + "</div>"
        )

    # 6b. Synthesizer breadcrumb. Always rendered (even when probabilities
    # are present) so the operator can see WHY a probability table looks
    # the way it does — full = synthesizer ran cleanly; failed = LLM
    # tried but didn't parse; unavailable = no AI client was passed.
    syn_status = e.get("synthesizer_status")
    syn_parse_errors = e.get("synthesizer_parse_errors") or []
    horizons_minutes = e.get("horizons_minutes") or []
    if syn_status or horizons_minutes or syn_parse_errors:
        status_colors = {
            "ok": "#26a69a",
            "failed": "#ef5350",
            "unavailable": "#787b86",
        }
        status_col = status_colors.get(str(syn_status).lower(), "#787b86")
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>Synthesizer</h4>"
            "<table cellspacing='0' cellpadding='4'>"
        )
        if syn_status:
            parts.append(
                f"<tr><td style='color:#787b86;'>Status</td>"
                f"<td style='color:{status_col}; font-weight:600;'>"
                f"{_html_escape(syn_status)}</td></tr>"
            )
        if horizons_minutes:
            hz = ", ".join(str(int(m)) for m in horizons_minutes if isinstance(m, (int, float)))
            parts.append(
                f"<tr><td style='color:#787b86;'>Requested horizons (min)</td>"
                f"<td>{_html_escape(hz)}</td></tr>"
            )
        parts.append("</table>")
        if syn_parse_errors:
            parts.append(
                "<div style='margin-top:6px; padding:4px 0 4px 10px; "
                "border-left:3px solid #f6c453; "
                "color:#f6c453; font-size:11px;'>"
                "<b>Parse errors</b> (synthesizer ran but some fields dropped):<br>"
                + "<br>".join(f"• {_html_escape(err)}" for err in syn_parse_errors)
                + "</div>"
            )

    # 7. Probabilities
    probs = e.get("probabilities") or []
    if probs:
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>"
            "Probabilities by horizon</h4>"
        )
        rows = [
            "<tr style='color:#787b86;'>"
            "<th align='left'>Horizon</th>"
            "<th align='left'>P(profit)</th>"
            "<th align='left'>P(drawdown first)</th>"
            "</tr>"
        ]
        for p in probs:
            mins = p.get("minutes")
            mins_str = (
                f"{int(mins)} min" if isinstance(mins, (int, float)) and mins < 1440
                else f"{int(mins)//60} h" if isinstance(mins, (int, float)) and mins < 4320
                else f"{int(mins)//1440} d" if isinstance(mins, (int, float))
                else "—"
            )
            pp = p.get("p_profit")
            pd = p.get("p_drawdown_first")
            pp_col = (
                "#26a69a" if isinstance(pp, (int, float)) and pp >= 0.55
                else "#ef5350" if isinstance(pp, (int, float)) and pp < 0.45
                else "#d1d4dc"
            )
            rows.append(
                f"<tr>"
                f"<td>{_html_escape(mins_str)}</td>"
                f"<td style='color:{pp_col}; font-weight:600;'>{_format_pct(pp, 1)}</td>"
                f"<td>{_format_pct(pd, 0)}</td>"
                f"</tr>"
            )
        parts.append("<table cellspacing='0' cellpadding='5'>" + "".join(rows) + "</table>")

    # 8. Excursion
    excursion = e.get("expected_excursion")
    if isinstance(excursion, dict):
        mae = excursion.get("expected_mae_atr")
        mfe = excursion.get("expected_mfe_atr")
        rr = excursion.get("implied_rr")
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>"
            "Expected excursion (in ATR units)</h4>"
            f"<div>Adverse (MAE): <b>{_html_escape(mae)}</b> · "
            f"Favorable (MFE): <b>{_html_escape(mfe)}</b> · "
            f"Implied R:R: <b>{_html_escape(rr)}</b></div>"
        )

    # 9. Edge thesis
    thesis = e.get("edge_thesis")
    if thesis:
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>Edge thesis</h4>"
            f"<blockquote style='margin:0; padding:4px 0 4px 12px; "
            f"border-left:3px solid #5b8def; color:#d1d4dc;'>"
            f"{_html_escape(thesis)}</blockquote>"
        )

    # 10. Key risks
    risks = e.get("key_risks") or []
    if risks:
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>Key risks</h4>"
            "<ul style='margin-top:0;'>"
            + "".join(f"<li>{_html_escape(r)}</li>" for r in risks)
            + "</ul>"
        )

    # 11. Sizing decision
    sizing = e.get("sizing") or {}
    if isinstance(sizing, dict) and sizing:
        mult = sizing.get("multiplier")
        skip = sizing.get("skip")
        p_used = sizing.get("p_profit_used")
        horizon = sizing.get("horizon_minutes")
        reason = sizing.get("reason") or "—"
        if skip:
            badge = (
                "<span style='color:#ef5350; font-weight:600;'>SKIP</span>"
            )
        else:
            mult_col = (
                "#26a69a" if isinstance(mult, (int, float)) and mult > 1.0
                else "#ef5350" if isinstance(mult, (int, float)) and mult < 1.0
                else "#d1d4dc"
            )
            badge = (
                f"<span style='color:{mult_col}; font-weight:600;'>"
                f"{_html_escape(mult)}×</span>"
            )
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>Sizing decision</h4>"
            f"<div>Multiplier: {badge}"
            f" · p_profit used: <b>{_format_pct(p_used, 1)}</b>"
            f" · horizon: <b>{_html_escape(horizon)} min</b></div>"
            f"<div style='color:#787b86; font-size:11px; margin-top:4px;'>"
            f"{_html_escape(reason)}</div>"
        )

    # 12. Data quality
    dq = e.get("data_quality", "—")
    missing = e.get("missing") or []
    if missing or dq != "full":
        dq_col = "#26a69a" if dq == "full" else "#f6c453"
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>Data quality</h4>"
            f"<div>Status: <span style='color:{dq_col}; font-weight:600;'>"
            f"{_html_escape(dq)}</span></div>"
        )
        if missing:
            parts.append(
                "<div style='color:#787b86; font-size:11px; margin-top:6px;'>"
                "Missing inputs: "
                + ", ".join(_html_escape(m) for m in missing[:15])
                + ("…" if len(missing) > 15 else "")
                + "</div>"
            )

    # 13. Context excerpt (smaller; the synthesizer input).
    ctx = e.get("context_excerpt")
    if ctx:
        parts.append(
            "<h4 style='margin-top:14px; margin-bottom:6px;'>"
            "Synthesizer input (excerpt)</h4>"
            f"<pre style='font-family: Consolas, monospace; font-size:11px; "
            f"color:#787b86; white-space:pre-wrap; "
            f"margin:0; padding:4px 0 4px 10px; "
            f"border-left:2px solid #2a2e39;'>"
            f"{_html_escape(ctx)}</pre>"
        )

    parts.append(
        f"<div style='color:#787b86; font-size:10px; margin-top:14px;'>"
        f"evaluated_at: {_html_escape(e.get('evaluated_at', '—'))}"
        f"</div></div>"
    )
    return "".join(parts)


def _render_no_eval_html(t: EvaluatedTrade) -> str:
    """Friendly empty-state when a trade has no evaluation block."""
    return (
        "<div style='font-family: -apple-system, Segoe UI, sans-serif;'>"
        f"<div style='color:#787b86; font-size:11px;'>"
        f"ticket {t.ticket} · action #{t.action_id} · "
        f"{_html_escape(t.action_type)} {_html_escape(t.side)}"
        "</div>"
        "<div style='margin-top:14px; padding:10px 12px; "
        "background:rgba(120,123,134,0.08); border-radius:6px; "
        "color:#787b86;'>"
        "No evaluation recorded for this trade.<br><br>"
        "This usually means the trade fired before the v2 evaluator "
        "was wired, or the evaluator worker crashed.<br>"
        f"Check <code>logs/orchestrator.log</code> around "
        f"<b>{_html_escape(t.opened_at)}</b>."
        "</div>"
        "</div>"
    )


# ---- Calibration ----------------------------------------------------


def _calibration_hover_format(point) -> str:
    """Tooltip body for the calibration bar chart's hover crosshair.

    Lives at module scope (not as a closure inside _CalibrationTab)
    so unit tests can exercise the formatting without spinning up Qt.
    Reads from `point.extra` keys filled in by populate().
    """
    extra = point.extra or {}
    n = extra.get("n", 0)
    wins = extra.get("wins", 0)
    wr = extra.get("win_rate", 0.0)
    p_avg = extra.get("p_profit_avg")
    avg_pnl = extra.get("avg_pnl", 0.0)
    p_str = (
        f"{p_avg * 100:.1f}%" if isinstance(p_avg, (int, float)) else "—"
    )
    delta_str = ""
    if isinstance(p_avg, (int, float)):
        delta = wr - p_avg
        col = (
            "#26a69a" if delta > 0.05
            else "#ef5350" if delta < -0.05
            else "#d1d4dc"
        )
        delta_str = (
            f"<br>delta: <b style='color:{col};'>{delta:+.2f}</b>"
        )
    return (
        f"<b>band {point.label}</b><br>"
        f"trades: {n}  ·  wins: {wins}<br>"
        f"realized: <b style='color:#26a69a;'>{wr * 100:.1f}%</b><br>"
        f"claimed: <b style='color:#5b8def;'>{p_str}</b>"
        f"{delta_str}<br>"
        f"avg PnL: ${avg_pnl:+.2f}"
    )


class _CalibrationTab(QWidget):
    """pyqtgraph bar chart of score-band vs realized win rate.

    Two overlaid bar groups:
      - Realized win rate per band (the truth).
      - Claimed p_profit_avg per band (what the synthesizer said).

    A diagonal "y = score/100" reference line lets you eyeball whether
    the evaluator is calibrated: bars should track the line. Bars above
    the line = miscalibrated low (pessimistic); below = overconfident.
    """

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel(
            "<span style='color:#787b86;'>Bars: actual win rate per "
            "evaluator score band. Solid = realized, hatched = "
            "synthesizer's claimed p_profit (when available). Sample "
            "counts shown below each band — under ~10 trades per "
            "band, the numbers are noise.</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        # pyqtgraph PlotWidget. We theme the background and axis pens
        # manually because pyqtgraph doesn't inherit Qt palette.
        pg.setConfigOption("background", "#0f1115")
        pg.setConfigOption("foreground", "#d1d4dc")
        self._plot = pg.PlotWidget()
        self._plot.setMouseEnabled(x=False, y=False)
        self._plot.setMenuEnabled(False)
        self._plot.hideButtons()
        self._plot.setLabel("left", "Win rate / probability")
        self._plot.setLabel("bottom", "Evaluator score band")
        self._plot.setYRange(0, 1.0)
        self._plot.showGrid(x=False, y=True, alpha=0.2)
        # Hover tooltip — shows band label, sample count, realized
        # win-rate, claimed p_profit. `extra` carries the bucket so
        # the formatter can present everything without snapping
        # ambiguity (one entry per band, snapped by x).
        from src.gui.views._pg_hover import HoverPoint, HoverTracker
        self._hover = HoverTracker(
            self._plot,
            format_fn=_calibration_hover_format,
        )
        layout.addWidget(self._plot, 1)

        self._summary = QLabel("")
        self._summary.setStyleSheet("color: #787b86; font-family: Consolas, monospace;")
        layout.addWidget(self._summary)

    def populate(self, buckets: list[CalibrationBucket]) -> None:
        self._plot.clear()
        self._hover.reattach()
        if not buckets:
            self._summary.setText("No data yet. Calibration appears after closed evaluated trades.")
            self._hover.set_points([])
            return

        x_indices = list(range(len(buckets)))
        labels = [b.band_label for b in buckets]

        # Realized win rate bars.
        win_rates = [b.win_rate for b in buckets]
        win_bars = pg.BarGraphItem(
            x=[i - 0.18 for i in x_indices], height=win_rates,
            width=0.35, brush=QColor("#26a69a"),
        )
        self._plot.addItem(win_bars)

        # Claimed p_profit_avg bars (when present).
        claimed = [b.p_profit_avg if b.p_profit_avg is not None else 0.0
                   for b in buckets]
        if any(b.p_profit_avg is not None for b in buckets):
            claim_bars = pg.BarGraphItem(
                x=[i + 0.18 for i in x_indices], height=claimed,
                width=0.35, brush=QColor("#5b8def"),
            )
            self._plot.addItem(claim_bars)

        # Diagonal reference: y = midpoint of band / 100.
        ref_x = list(range(len(buckets)))
        ref_y = [((b.band_lo + b.band_hi - 1) / 2.0) / 100.0 for b in buckets]
        ref_line = pg.PlotDataItem(
            ref_x, ref_y, pen=pg.mkPen(QColor("#787b86"), width=1, style=Qt.PenStyle.DashLine),
        )
        self._plot.addItem(ref_line)

        # X-axis ticks.
        x_axis = self._plot.getAxis("bottom")
        x_axis.setTicks([
            [(i, f"{labels[i]}\nn={buckets[i].n}") for i in x_indices],
        ])

        # Hover points: one per band, snapped to the realized
        # win-rate bar so the marker lands on the green bar (the
        # operator's primary number). The full bucket dict travels
        # along in `extra` so the formatter can render all four
        # numbers at once.
        from src.gui.views._pg_hover import HoverPoint
        self._hover.set_points([
            HoverPoint(
                x=float(i),
                y=b.win_rate,
                label=b.band_label,
                extra={
                    "n": b.n,
                    "wins": b.wins,
                    "win_rate": b.win_rate,
                    "p_profit_avg": b.p_profit_avg,
                    "avg_pnl": b.avg_pnl,
                },
            )
            for i, b in enumerate(buckets)
        ])

        # Summary table below.
        lines = ["band     n   wins  win_rate  claimed_p   delta"]
        for b in buckets:
            claimed_str = f"{b.p_profit_avg:.2f}" if b.p_profit_avg is not None else "  — "
            delta = (
                f"{(b.win_rate - b.p_profit_avg):+.2f}"
                if b.p_profit_avg is not None else "   — "
            )
            lines.append(
                f"{b.band_label:<7} {b.n:>3}  {b.wins:>4}   "
                f"{b.win_rate:>5.2f}     {claimed_str:>5}    {delta}"
            )
        self._summary.setText("\n".join(lines))


# ---- Regime breakdown ----------------------------------------------


class _RegimeTab(QWidget):
    """Per-regime aggregates. Highest-leverage diagnostic: spot which
    macro/vol/session combinations the evaluator nails vs misreads."""

    _HEADERS = ("Regime tag", "Trades", "Win rate", "Avg P&L", "Avg score")

    def __init__(self) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        hint = QLabel(
            "<span style='color:#787b86;'>Regimes with fewer than 3 "
            "trades are hidden — too small a sample to interpret. "
            "Look for high win-rate regimes (lean into) and low ones "
            "(downgrade / skip).</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._table = QTableWidget(0, len(self._HEADERS))
        self._table.setHorizontalHeaderLabels(self._HEADERS)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.verticalHeader().setVisible(False)
        hdr = self._table.horizontalHeader()
        hdr.setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        for i in range(1, len(self._HEADERS)):
            hdr.setSectionResizeMode(i, QHeaderView.ResizeMode.ResizeToContents)
        layout.addWidget(self._table, 1)

    def populate(self, buckets: list[RegimeBucket]) -> None:
        self._table.setRowCount(0)
        for b in buckets:
            row = self._table.rowCount()
            self._table.insertRow(row)
            cells = (
                b.regime_summary,
                str(b.n),
                f"{b.win_rate*100:.0f}%",
                f"{b.avg_pnl:+.2f}",
                f"{b.avg_score:.0f}",
            )
            for col, val in enumerate(cells):
                item = QTableWidgetItem(val)
                if col == 2:  # win-rate color
                    if b.win_rate >= 0.55:
                        item.setForeground(QColor("#26a69a"))
                    elif b.win_rate < 0.45:
                        item.setForeground(QColor("#ef5350"))
                self._table.setItem(row, col, item)
        self._table.resizeRowsToContents()
