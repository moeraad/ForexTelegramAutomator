"""COST view: token usage + estimated spend from logs/ai_calls.jsonl."""
from __future__ import annotations

from typing import Any

from PySide6.QtCharts import (
    QBarCategoryAxis,
    QBarSeries,
    QBarSet,
    QChart,
    QChartView,
    QValueAxis,
)
from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDoubleSpinBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.ai_costs import (
    CallRecord,
    CostSummary,
    expensive_calls,
    load_records,
    summarize,
    summarize_by_channel,
    summarize_by_route,
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


def _hex_to_accent(color: str) -> str:
    """Map a legacy hex color into a StatCard semantic accent key."""
    if not color:
        return ""
    c = color.lower()
    if c in ("#26a69a", "#859900", "#00e676", "#00897b"):
        return "success"
    if c in ("#ef5350", "#dc322f", "#ff5252", "#d32f2f"):
        return "danger"
    if c in ("#ff9800", "#b58900", "#ffd740", "#f57c00"):
        return "warning"
    if c in ("#2962ff", "#268bd2", "#448aff", "#1976d2"):
        return "accent"
    return ""


def _fmt_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.2f} M"
    if n >= 1_000:
        return f"{n / 1_000:.1f} k"
    return str(n)


class _BreakdownModel(QAbstractTableModel):
    """Two-column model: key (channel id / route id) + per-key CostSummary.

    Used by both the by-channel and by-route panels in cost_view (Step 18).
    Sorted by estimated_cost_usd descending so the chattiest channels /
    routes float to the top.
    """

    HEADERS = ("Key", "Calls", "In tok", "Out tok", "Cost (est)")

    def __init__(self, key_label: str) -> None:
        super().__init__()
        self.HEADERS = (key_label, "Calls", "In tok", "Out tok", "Cost (est)")
        self._rows: list[tuple[str, CostSummary]] = []

    def set_rows(self, summaries: dict[str, CostSummary]) -> None:
        self.beginResetModel()
        self._rows = sorted(
            summaries.items(),
            key=lambda kv: kv[1].estimated_cost_usd,
            reverse=True,
        )
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(
        self, section: int, orientation: Qt.Orientation,
        role: int = Qt.ItemDataRole.DisplayRole,
    ) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        key, summary = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return key
            if col == 1:
                return str(summary.calls)
            if col == 2:
                return _fmt_tokens(summary.input_tokens)
            if col == 3:
                return _fmt_tokens(summary.output_tokens)
            if col == 4:
                return f"${summary.estimated_cost_usd:.4f}"
        if role == Qt.ItemDataRole.TextAlignmentRole and col >= 1:
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        return None


class _TopCallsModel(QAbstractTableModel):
    HEADERS = ("When", "Stage", "Decision", "In tokens", "Out tokens", "Cache R", "Cost (est)")

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[CallRecord] = []

    def set_rows(self, rows: list[CallRecord]) -> None:
        self.beginResetModel()
        self._rows = rows
        self.endResetModel()

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return r.ts.strftime("%Y-%m-%d %H:%M")
            if col == 1:
                return r.stage
            if col == 2:
                return r.decision or ("error" if r.is_error else "—")
            if col == 3:
                return _fmt_tokens(r.input_tokens)
            if col == 4:
                return _fmt_tokens(r.output_tokens)
            if col == 5:
                return _fmt_tokens(r.cache_read_tokens)
            if col == 6:
                return f"${r.estimated_cost():.4f}"
        if role == Qt.ItemDataRole.TextAlignmentRole and col in (3, 4, 5, 6):
            return int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        if role == Qt.ItemDataRole.ForegroundRole and col == 2 and r.is_error:
            return QColor("#ef5350")
        return None


class CostView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._days: int | None = 30
        self._stat_row_layout: QHBoxLayout | None = None
        self._stat_boxes: dict[str, QWidget] = {}
        self._top_model = _TopCallsModel()
        self._channel_model = _BreakdownModel("Channel")
        self._route_model = _BreakdownModel("Route")
        self._build_ui()
        self.refresh()
        from src.gui.theme import bus as theme_bus
        theme_bus.theme_changed.connect(self._on_theme)

    def _on_theme(self, _pal=None) -> None:
        from src.gui.views._chart_theme import apply_dark_theme
        apply_dark_theme(self._chart, self._chart_view)
        self.refresh()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self.refresh()

    def refresh(self) -> None:
        # Listener/bot write logs to Path(DB_PATH).parent / "logs" (see
        # src/config.py:LOGS_DIR). Reading from project_path/logs misses the
        # actual file when project_path is the PyInstaller _internal dir.
        log_path = self._stack.db_path.parent / "logs" / "ai_calls.jsonl"
        records = load_records(log_path, days=self._days)
        s = summarize(records)
        self._update_stats(s, len(records))
        self._top_model.set_rows(expensive_calls(records, top=10))
        # Step 18: per-channel and per-route breakdowns. summarize_by_*
        # buckets falsy keys under "(unattributed)" so pre-Step-18 rows
        # remain visible (not silently dropped from totals).
        self._channel_model.set_rows(summarize_by_channel(records))
        self._route_model.set_rows(summarize_by_route(records))
        self._update_chart(s)
        self._meta_label.setText(f"file: {log_path}  ·  {len(records)} call(s) in range")
        self._refresh_budget(s)

    def _refresh_budget(self, s: CostSummary) -> None:
        from datetime import datetime, timezone
        from src import db_settings
        budget = db_settings.get_float(self._stack.db_path, "cost_daily_budget_usd", 5.0)
        self._budget_spin.blockSignals(True)
        self._budget_spin.setValue(float(budget))
        self._budget_spin.blockSignals(False)
        cap_mult = db_settings.get_float(self._stack.db_path, "cost_cap_multiplier", 1.2)
        self._cap_spin.blockSignals(True)
        self._cap_spin.setValue(float(cap_mult or 1.2))
        self._cap_spin.blockSignals(False)
        # Auto-halt switch: enabled iff budget > 0 (existing cost_guard
        # contract). Label reflects the active multiplier so the operator
        # always sees the effective cap (REVIEW.md §4.4).
        self._auto_halt_label.setText(
            f"Auto-halt when spend exceeds budget × {cap_mult:.1f}"
        )
        if self._auto_halt_switch is not None:
            self._auto_halt_switch.blockSignals(True)
            self._auto_halt_switch.setChecked(budget > 0)
            self._auto_halt_switch.blockSignals(False)
        today = datetime.now(timezone.utc).date().isoformat()
        today_cost = float(s.per_day.get(today, 0.0))
        pct = (today_cost / budget * 100) if budget > 0 else 0
        if budget <= 0:
            self._budget_status.setText(
                "<span style='color:#787b86;'>budget disabled (set > $0 to enable alerts)</span>"
            )
            self._budget_banner.hide()
            return
        color = "#787b86"
        if pct >= 100:
            color = "#ef5350"
        elif pct >= 80:
            color = "#ff9800"
        self._budget_status.setText(
            f"<span style='color:{color};'>"
            f"today: ${today_cost:.2f} / ${budget:.2f}  ({pct:.0f}%)"
            "</span>"
        )
        if pct >= 100:
            over = today_cost - budget
            self._budget_banner.setText(
                f"⚠ Daily budget exceeded — spent ${today_cost:.2f} "
                f"vs ${budget:.2f} budget (over by ${over:.2f})"
            )
            self._budget_banner.show()
        else:
            self._budget_banner.hide()

    def _on_budget_changed(self) -> None:
        from src import db_settings
        value = self._budget_spin.value()
        db_settings.set_str(self._stack.db_path, "cost_daily_budget_usd", f"{value:.2f}")
        self.refresh()

    def _on_auto_halt_toggled(self, checked: bool) -> None:
        """SwitchButton handler — flipping OFF sets budget=0 (the existing
        cost_guard contract for "disabled"); flipping ON restores the
        previously-saved budget if it was zero, otherwise leaves it.
        REVIEW.md §4.4."""
        from src import db_settings
        if checked:
            current = db_settings.get_float(self._stack.db_path, "cost_daily_budget_usd", 0.0)
            if current <= 0:
                # Restore previous non-zero budget if we cached one, else default.
                prev = db_settings.get_float(
                    self._stack.db_path, "cost_daily_budget_usd_last_enabled", 5.0
                )
                db_settings.set_str(
                    self._stack.db_path,
                    "cost_daily_budget_usd",
                    f"{max(prev, 1.0):.2f}",
                )
        else:
            current = db_settings.get_float(self._stack.db_path, "cost_daily_budget_usd", 0.0)
            if current > 0:
                # Cache so toggling back ON restores intent.
                db_settings.set_str(
                    self._stack.db_path,
                    "cost_daily_budget_usd_last_enabled",
                    f"{current:.2f}",
                )
            db_settings.set_str(self._stack.db_path, "cost_daily_budget_usd", "0")
        self.refresh()

    def _on_cap_changed(self) -> None:
        from src import db_settings
        value = self._cap_spin.value()
        db_settings.set_str(self._stack.db_path, "cost_cap_multiplier", f"{value:.1f}")
        self.refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>COST</span>")
        from src.gui.panels._a11y import mark_heading
        mark_heading(title, "Cost")
        title.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(title)
        hint = QLabel(
            "<span style='color:#787b86;'>token usage from logs/ai_calls.jsonl  ·  "
            "cost is an estimate using reference rates</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(hint)
        top.addSpacing(16)
        top.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, days in _RANGES:
            self._range_combo.addItem(label, days)
        self._range_combo.setCurrentIndex(3)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        top.addWidget(self._range_combo)
        top.addStretch()
        from src.gui._button_helpers import make_refresh_button
        refresh = make_refresh_button("Reload cost data")
        refresh.clicked.connect(self.refresh)
        top.addWidget(refresh)
        layout.addLayout(top)

        self._meta_label = QLabel()
        self._meta_label.setStyleSheet("color: #787b86; padding-bottom: 4px;")
        layout.addWidget(self._meta_label)

        # Auto-halt toggle (REVIEW.md §4.4). Previously the cost cap was
        # implicit ("set Daily budget = 0 to disable"). Surfacing an
        # explicit on/off switch with the active multiplier inline makes
        # the policy visible at a glance.
        try:
            from qfluentwidgets import SwitchButton
            self._auto_halt_switch: SwitchButton | None = SwitchButton()
            self._auto_halt_switch.setOnText("On")
            self._auto_halt_switch.setOffText("Off")
            self._auto_halt_switch.checkedChanged.connect(self._on_auto_halt_toggled)
        except Exception:
            self._auto_halt_switch = None
        auto_row = QHBoxLayout()
        self._auto_halt_label = QLabel("Auto-halt when spend exceeds budget x 1.2")
        auto_row.addWidget(self._auto_halt_label)
        if self._auto_halt_switch is not None:
            auto_row.addWidget(self._auto_halt_switch)
        auto_row.addStretch()
        layout.addLayout(auto_row)

        # Daily budget controls + alert banner
        budget_row = QHBoxLayout()
        budget_row.addWidget(QLabel("Daily budget:"))
        self._budget_spin = QDoubleSpinBox()
        self._budget_spin.setRange(0.0, 10000.0)
        self._budget_spin.setDecimals(2)
        self._budget_spin.setSingleStep(1.0)
        self._budget_spin.setPrefix("$ ")
        self._budget_spin.editingFinished.connect(self._on_budget_changed)
        budget_row.addWidget(self._budget_spin)
        budget_row.addSpacing(12)
        budget_row.addWidget(QLabel("Hard cap multiplier:"))
        self._cap_spin = QDoubleSpinBox()
        self._cap_spin.setRange(1.0, 5.0)
        self._cap_spin.setDecimals(1)
        self._cap_spin.setSingleStep(0.1)
        self._cap_spin.setSuffix("×")
        self._cap_spin.setToolTip(
            "When today's spend exceeds budget × this multiplier, the bot "
            "auto-halts trading and DMs the operator. Set 1.0 to halt at "
            "exact budget. Default 1.2 allows a small overshoot."
        )
        self._cap_spin.editingFinished.connect(self._on_cap_changed)
        budget_row.addWidget(self._cap_spin)
        self._budget_status = QLabel("")
        self._budget_status.setTextFormat(Qt.TextFormat.RichText)
        budget_row.addWidget(self._budget_status, 1)
        layout.addLayout(budget_row)

        self._budget_banner = QLabel("")
        self._budget_banner.setTextFormat(Qt.TextFormat.RichText)
        self._budget_banner.setStyleSheet(
            "QLabel { background: #ef5350; color: white; "
            "padding: 8px 12px; border-radius: 4px; font-weight: 600; }"
        )
        self._budget_banner.hide()
        layout.addWidget(self._budget_banner)

        from src.gui.panels._stat_card import StatCard
        self._stat_row_layout = QHBoxLayout()
        self._stat_row_layout.setSpacing(8)
        for key in ("calls", "cost", "in", "out", "cache", "keep_rate", "err_rate"):
            card = StatCard(label=key, value="—")
            self._stat_boxes[key] = card
            self._stat_row_layout.addWidget(card)
        self._stat_row_layout.addStretch()
        layout.addLayout(self._stat_row_layout)

        # Chart and Top-10 sit side-by-side so each takes the full
        # available height instead of fighting for it vertically
        # (REVIEW.md follow-up). 2:1 stretch keeps the chart dominant.
        from src.gui.views._chart_theme import apply_dark_theme
        from PySide6.QtWidgets import QSplitter

        body_split = QSplitter(Qt.Orientation.Horizontal)
        body_split.setChildrenCollapsible(False)

        chart_panel = QWidget()
        chart_panel_layout = QVBoxLayout(chart_panel)
        chart_panel_layout.setContentsMargins(0, 0, 0, 0)
        self._chart = QChart()
        self._chart.setTitle("Estimated daily spend")
        self._chart.legend().hide()
        self._chart_view = QChartView(self._chart)
        self._chart_view.setRenderHint(QPainter.RenderHint.Antialiasing)
        self._chart_view.setMinimumHeight(240)
        apply_dark_theme(self._chart, self._chart_view)
        chart_panel_layout.addWidget(self._chart_view, 1)
        body_split.addWidget(chart_panel)

        right_panel = QWidget()
        right_panel_layout = QVBoxLayout(right_panel)
        right_panel_layout.setContentsMargins(0, 0, 0, 0)
        right_panel_layout.addWidget(QLabel("Top 10 most expensive calls"))
        self._top_table = QTableView()
        self._top_table.setModel(self._top_model)
        self._top_table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._top_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._top_table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        apply_full_width_headers(
            self._top_table,
            content_columns=tuple(range(self._top_model.columnCount() - 1)),
        )
        self._top_table.setAlternatingRowColors(True)
        self._top_table.setShowGrid(False)
        right_panel_layout.addWidget(self._top_table, 1)
        body_split.addWidget(right_panel)

        body_split.setStretchFactor(0, 2)
        body_split.setStretchFactor(1, 1)
        layout.addWidget(body_split, 1)

        # Step 18: per-channel + per-route breakdown tables side-by-side.
        # Useful once mirror (Step 11) or aggregate (Step 12) routing is
        # in play and the single "total per destination" number stops
        # telling the operator which channel / route is the cost driver.
        breakdown_split = QSplitter(Qt.Orientation.Horizontal)
        breakdown_split.setChildrenCollapsible(False)

        ch_panel = QWidget()
        ch_layout = QVBoxLayout(ch_panel)
        ch_layout.setContentsMargins(0, 0, 0, 0)
        ch_label = QLabel("Cost by channel  ·  source_channel_id")
        ch_label.setStyleSheet("color: #787b86; padding-top: 4px;")
        ch_layout.addWidget(ch_label)
        self._channel_table = QTableView()
        self._channel_table.setModel(self._channel_model)
        self._channel_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self._channel_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self._channel_table.verticalHeader().setVisible(False)
        self._channel_table.setAlternatingRowColors(True)
        self._channel_table.setShowGrid(False)
        apply_full_width_headers(
            self._channel_table,
            content_columns=tuple(range(self._channel_model.columnCount() - 1)),
        )
        ch_layout.addWidget(self._channel_table, 1)
        breakdown_split.addWidget(ch_panel)

        rt_panel = QWidget()
        rt_layout = QVBoxLayout(rt_panel)
        rt_layout.setContentsMargins(0, 0, 0, 0)
        rt_label = QLabel("Cost by route  ·  route_id")
        rt_label.setStyleSheet("color: #787b86; padding-top: 4px;")
        rt_layout.addWidget(rt_label)
        self._route_table = QTableView()
        self._route_table.setModel(self._route_model)
        self._route_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows,
        )
        self._route_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers,
        )
        self._route_table.verticalHeader().setVisible(False)
        self._route_table.setAlternatingRowColors(True)
        self._route_table.setShowGrid(False)
        apply_full_width_headers(
            self._route_table,
            content_columns=tuple(range(self._route_model.columnCount() - 1)),
        )
        rt_layout.addWidget(self._route_table, 1)
        breakdown_split.addWidget(rt_panel)

        breakdown_split.setStretchFactor(0, 1)
        breakdown_split.setStretchFactor(1, 1)
        layout.addWidget(breakdown_split, 1)

    def _on_range_changed(self, _idx: int) -> None:
        self._days = self._range_combo.currentData()
        self.refresh()

    def _update_stats(self, s: CostSummary, total_records: int) -> None:
        replacements = (
            ("calls", "Total calls", str(s.calls), "#d1d4dc"),
            ("cost", "Est cost (USD)", f"${s.estimated_cost_usd:.2f}", "#d1d4dc"),
            ("in", "Input tokens", _fmt_tokens(s.input_tokens), "#d1d4dc"),
            ("out", "Output tokens", _fmt_tokens(s.output_tokens), "#d1d4dc"),
            ("cache", "Cache hit", f"{s.cache_hit_rate * 100:.0f}%", "#d1d4dc"),
            ("keep_rate", "Triage keep", f"{s.triage_keep_rate * 100:.0f}%", "#d1d4dc"),
            ("err_rate", "Error rate", f"{s.error_rate * 100:.0f}%",
             "#ef5350" if s.error_rate > 0.05 else "#d1d4dc"),
        )
        for key, label, value, color in replacements:
            self._replace_box(key, label, value, color)
        if total_records == 0:
            # Empty-state copy that's specific instead of leaving the chart
            # blank (REVIEW.md §3). Tells the operator the chart will fill
            # in once the first signal arrives, not that something is broken.
            self._chart.setTitle(
                "Estimated daily spend  ·  no AI calls yet — chart will "
                "appear after the first signal"
            )

    def _replace_box(self, key: str, label: str, value: str, color: str) -> None:
        accent = _hex_to_accent(color)
        card = self._stat_boxes[key]
        card.set_value(value, accent)
        # The label may have changed (e.g. "calls" → "5,210 calls").
        if hasattr(card, "_label") and label:
            card._label.setText(label.upper())

    def _update_chart(self, s: CostSummary) -> None:
        self._chart.removeAllSeries()
        for ax in list(self._chart.axes()):
            self._chart.removeAxis(ax)

        if not s.per_day:
            return

        bar_set = QBarSet("USD")
        bar_set.setColor(QColor("#2962ff"))
        for day in s.per_day:
            bar_set.append(s.per_day[day])
        series = QBarSeries()
        series.append(bar_set)
        series.setLabelsVisible(False)
        self._chart.addSeries(series)

        axis_x = QBarCategoryAxis()
        axis_x.append(list(s.per_day.keys()))
        axis_x.setLabelsAngle(-45)
        self._chart.addAxis(axis_x, Qt.AlignmentFlag.AlignBottom)
        series.attachAxis(axis_x)

        axis_y = QValueAxis()
        axis_y.setTitleText("USD")
        ymax = max(s.per_day.values()) if s.per_day else 1.0
        axis_y.setRange(0, ymax * 1.15)
        self._chart.addAxis(axis_y, Qt.AlignmentFlag.AlignLeft)
        series.attachAxis(axis_y)
        from src.gui.views._chart_theme import theme_axis
        theme_axis(axis_x)
        theme_axis(axis_y)

        days = len(s.per_day)
        last_day_cost = list(s.per_day.values())[-1]
        self._chart.setTitle(
            f"Estimated daily spend  ·  {days} day(s)  ·  most recent ${last_day_cost:.2f}"
        )
