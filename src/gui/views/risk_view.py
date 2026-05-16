"""RISK view: configure budget caps; live metrics + breach indicator."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QDoubleSpinBox,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.risk_budget import RiskConfig, RiskMetrics, RiskMonitor
from src.gui.services.stack_registry import Stack


def _hex_to_accent(color: str) -> str:
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


class RiskView(QWidget):
    def __init__(self, stack: Stack, monitor: RiskMonitor) -> None:
        super().__init__()
        self._stack = stack
        self._monitor = monitor
        self._stat_row_layout: QHBoxLayout | None = None
        self._stat_boxes: dict[str, QWidget] = {}
        self._build_ui()
        self._populate_from_config(monitor.config)
        monitor.metrics_updated.connect(self._on_metrics)
        monitor.triggered.connect(self._on_triggered)
        monitor.config_changed.connect(self._on_config_changed)

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._populate_from_config(self._monitor.config)
        self._clear_banner()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>RISK BUDGET</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        from src.gui.panels._a11y import mark_heading
        mark_heading(title, "Risk budget")
        top.addWidget(title)
        hint = QLabel(
            "<span style='color:#787b86;'>auto-halts trading when any limit is breached  ·  "
            "must be manually cleared via HALT button</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(hint)
        top.addStretch()
        layout.addLayout(top)

        self._banner = QLabel()
        self._banner.setStyleSheet(
            "QLabel { background: #ef5350; color: white; padding: 10px 14px; "
            "border-radius: 4px; font-weight: 600; }"
        )
        self._banner.setWordWrap(True)
        self._banner.setVisible(False)
        layout.addWidget(self._banner)

        layout.addWidget(QLabel("Current state"))
        from src.gui.panels._stat_card import StatCard
        self._stat_row_layout = QHBoxLayout()
        self._stat_row_layout.setSpacing(8)
        for key in ("today_pnl", "week_pnl", "open_lots", "today_trades"):
            card = StatCard(label=key, value="—")
            self._stat_boxes[key] = card
            self._stat_row_layout.addWidget(card)
        self._stat_row_layout.addStretch()
        layout.addLayout(self._stat_row_layout)

        layout.addWidget(QLabel("Utilization"))
        self._bar_widgets: dict[str, tuple[QLabel, QProgressBar]] = {}
        for key, label in (
            ("daily_dd", "Daily drawdown"),
            ("weekly_dd", "Weekly drawdown"),
            ("open_lots", "Open lots"),
            ("today_trades", "Today's trades"),
        ):
            row = QHBoxLayout()
            l = QLabel(label)
            l.setMinimumWidth(140)
            row.addWidget(l)
            bar = QProgressBar()
            bar.setMaximum(100)
            bar.setMinimumWidth(280)
            row.addWidget(bar)
            cap = QLabel("—")
            cap.setMinimumWidth(220)
            cap.setStyleSheet("color: #787b86; padding-left: 8px;")
            row.addWidget(cap)
            row.addStretch()
            layout.addLayout(row)
            self._bar_widgets[key] = (cap, bar)

        layout.addWidget(QLabel("Limits"))
        form = QFormLayout()
        self._daily_dd = QDoubleSpinBox()
        self._daily_dd.setRange(-1_000_000.0, 0.0)
        self._daily_dd.setDecimals(2)
        self._daily_dd.setSuffix("  USD")
        self._daily_dd.setSingleStep(10.0)
        form.addRow("Daily drawdown limit  (negative)", self._daily_dd)

        self._weekly_dd = QDoubleSpinBox()
        self._weekly_dd.setRange(-1_000_000.0, 0.0)
        self._weekly_dd.setDecimals(2)
        self._weekly_dd.setSuffix("  USD")
        self._weekly_dd.setSingleStep(25.0)
        form.addRow("Weekly drawdown limit  (negative)", self._weekly_dd)

        self._max_lots = QDoubleSpinBox()
        self._max_lots.setRange(0.01, 1000.0)
        self._max_lots.setDecimals(2)
        self._max_lots.setSingleStep(0.01)
        form.addRow("Max total open lots", self._max_lots)

        self._max_trades = QSpinBox()
        self._max_trades.setRange(1, 1000)
        form.addRow("Daily trade count limit", self._max_trades)

        self._enabled = QCheckBox("enforce  ·  auto-halt on breach")
        form.addRow("Enforcement", self._enabled)

        layout.addLayout(form)

        actions = QHBoxLayout()
        try:
            from qfluentwidgets import PrimaryPushButton
            self._save_btn = PrimaryPushButton("Save")
        except Exception:
            self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._save)
        actions.addWidget(self._save_btn)
        revert_btn = QPushButton("Revert")
        revert_btn.clicked.connect(lambda: self._populate_from_config(self._monitor.config))
        actions.addWidget(revert_btn)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()

    def _populate_from_config(self, cfg: RiskConfig) -> None:
        self._daily_dd.setValue(cfg.daily_dd_limit_usd)
        self._weekly_dd.setValue(cfg.weekly_dd_limit_usd)
        self._max_lots.setValue(cfg.max_open_lots)
        self._max_trades.setValue(cfg.daily_trade_count_limit)
        self._enabled.setChecked(cfg.enabled)

    def _read_config(self) -> RiskConfig:
        return RiskConfig(
            enabled=self._enabled.isChecked(),
            daily_dd_limit_usd=self._daily_dd.value(),
            weekly_dd_limit_usd=self._weekly_dd.value(),
            max_open_lots=self._max_lots.value(),
            daily_trade_count_limit=self._max_trades.value(),
        )

    def _save(self) -> None:
        cfg = self._read_config()
        if cfg.daily_dd_limit_usd >= 0:
            QMessageBox.warning(self, "Invalid limit", "Daily drawdown limit must be ≤ 0 (it's a loss cap).")
            return
        if cfg.weekly_dd_limit_usd >= 0:
            QMessageBox.warning(self, "Invalid limit", "Weekly drawdown limit must be ≤ 0.")
            return
        self._monitor.update_config(cfg)
        QMessageBox.information(self, "Saved", "Risk budget saved.\nMonitor is using the new limits.")

    def _on_metrics(self, m: RiskMetrics) -> None:
        cfg = self._monitor.config
        pnl_color_today = "#ef5350" if m.today_pnl < 0 else "#26a69a" if m.today_pnl > 0 else "#d1d4dc"
        pnl_color_week = "#ef5350" if m.week_pnl < 0 else "#26a69a" if m.week_pnl > 0 else "#d1d4dc"
        self._replace_box("today_pnl", "Today realized", f"${m.today_pnl:+.2f}", pnl_color_today)
        self._replace_box("week_pnl", "Week realized", f"${m.week_pnl:+.2f}", pnl_color_week)
        self._replace_box("open_lots", "Open lots", f"{m.open_lots:.2f}")
        self._replace_box("today_trades", "Today opened", str(m.today_trade_count))
        self._update_bars(cfg, m)

    def _update_bars(self, cfg: RiskConfig, m: RiskMetrics) -> None:
        def _pct(used: float, limit: float) -> int:
            if limit == 0:
                return 0
            v = int(round(abs(used / limit) * 100))
            return max(0, min(100, v))

        daily_pct = _pct(m.today_pnl, cfg.daily_dd_limit_usd) if cfg.daily_dd_limit_usd < 0 else 0
        weekly_pct = _pct(m.week_pnl, cfg.weekly_dd_limit_usd) if cfg.weekly_dd_limit_usd < 0 else 0
        lots_pct = _pct(m.open_lots, cfg.max_open_lots) if cfg.max_open_lots > 0 else 0
        trades_pct = _pct(float(m.today_trade_count), float(cfg.daily_trade_count_limit)) if cfg.daily_trade_count_limit > 0 else 0

        captions = {
            "daily_dd": (daily_pct, f"${m.today_pnl:+.2f} / ${cfg.daily_dd_limit_usd:+.2f}"),
            "weekly_dd": (weekly_pct, f"${m.week_pnl:+.2f} / ${cfg.weekly_dd_limit_usd:+.2f}"),
            "open_lots": (lots_pct, f"{m.open_lots:.2f} / {cfg.max_open_lots:.2f}"),
            "today_trades": (trades_pct, f"{m.today_trade_count} / {cfg.daily_trade_count_limit}"),
        }
        for key, (pct, caption) in captions.items():
            cap_lbl, bar = self._bar_widgets[key]
            bar.setValue(pct)
            cap_lbl.setText(caption)
            bar.setStyleSheet(self._bar_css(pct))

    def _bar_css(self, pct: int) -> str:
        chunk = "#26a69a"
        if pct >= 100:
            chunk = "#ef5350"
        elif pct >= 75:
            chunk = "#ff7043"
        elif pct >= 50:
            chunk = "#ff9800"
        from src.gui.theme import current_palette
        p = current_palette()
        return (
            "QProgressBar { "
            f"background: {p.surface}; border: 1px solid {p.border};"
            f" border-radius: 3px; height: 14px; text-align: center;"
            f" color: {p.text}; "
            "} "
            f"QProgressBar::chunk {{ background: {chunk}; border-radius: 2px; }}"
        )

    def _on_triggered(self, breaches: list[str]) -> None:
        self._banner.setText(
            "Risk budget triggered an auto-halt:\n  · " + "\n  · ".join(breaches)
        )
        self._banner.setVisible(True)

    def _on_config_changed(self, _cfg: RiskConfig) -> None:
        pass

    def _clear_banner(self) -> None:
        self._banner.setVisible(False)

    def _replace_box(self, key: str, label: str, value: str, color: str = "#d1d4dc") -> None:
        assert self._stat_row_layout is not None
        card = self._stat_boxes[key]
        card.set_value(value, _hex_to_accent(color))
        if hasattr(card, "_label") and label:
            card._label.setText(label.upper())
