# src/gui/views/manual_trade_view.py
"""Manual Trade tab: candlestick chart + order form. Places entry/TP/SL
lines, sizes the lot from balance + risk cap, and POSTs a manual OPEN."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from src.gui.services.manual_trade_sizing import compute_lot, SizingResult
from src.gui.services.manual_trade_submit import (
    assign_sl_tp, build_manual_open_body, infer_pending, submit_manual_open,
)
from src.gui.views.manual_trade_chart import ChartPanel

_CONTRACT_SIZE = 100.0     # XAUUSD: $100 per 1.0 price move per 1.0 lot
_LOT_STEP = 0.01
_LOT_MIN = 0.01
_LOT_MAX = 100.0
_ENTRY_TOL = 0.5           # within this of live price -> market order


class ManualTradeView(QWidget):
    def __init__(self, stack) -> None:
        super().__init__()
        self._stack = stack
        self._side = "BUY"
        self._last_sizing: SizingResult | None = None
        self._live_price: float | None = None
        self._timeframe = "M15"
        self._build_ui()
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._refresh_market)
        self._poll.start()
        self._refresh_market()

    # ---- UI ----------------------------------------------------------
    def _build_ui(self) -> None:
        self._chart = ChartPanel()
        self._chart.lines_changed.connect(self._recompute)

        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["M15", "H1", "H4"])
        self._tf_combo.currentTextChanged.connect(self._on_tf_changed)

        self._side_combo = QComboBox()
        self._side_combo.addItems(["BUY", "SELL"])
        self._side_combo.currentTextChanged.connect(self._on_side_changed)

        self._lot_per_100 = QDoubleSpinBox()
        self._lot_per_100.setDecimals(4)
        self._lot_per_100.setRange(0.0001, 100.0)
        self._lot_per_100.setValue(0.01)
        self._lot_per_100.valueChanged.connect(self._recompute)

        self._risk_cap = QDoubleSpinBox()
        self._risk_cap.setDecimals(2)
        self._risk_cap.setRange(0.01, 100.0)
        self._risk_cap.setValue(1.0)
        self._risk_cap.setSuffix(" %")
        self._risk_cap.valueChanged.connect(self._recompute)

        self._arm_btn = QPushButton("Place order (arm lines)")
        self._arm_btn.clicked.connect(self._arm)
        self._summary = QLabel("Arm the lines to size a trade.")
        self._summary.setWordWrap(True)
        self._exec_btn = QPushButton("Execute")
        self._exec_btn.setEnabled(False)
        self._exec_btn.clicked.connect(self._execute)

        form = QFormLayout()
        form.addRow("Timeframe", self._tf_combo)
        form.addRow("Direction", self._side_combo)
        form.addRow("Lot per $100", self._lot_per_100)
        form.addRow("Risk cap", self._risk_cap)
        form.addRow(self._arm_btn)
        form.addRow(self._summary)
        form.addRow(self._exec_btn)

        form_box = QWidget()
        form_box.setLayout(form)
        form_box.setMaximumWidth(360)

        root = QHBoxLayout(self)
        root.addWidget(self._chart, stretch=1)
        root.addWidget(form_box)

    # ---- data --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._stack.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _balance(self) -> float:
        try:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='account_balance'"
                ).fetchone()
            finally:
                conn.close()
            return float(row[0]) if row and row[0] is not None else 0.0
        except (sqlite3.Error, ValueError, TypeError):
            return 0.0

    def _refresh_market(self) -> None:
        import json
        import urllib.request
        base = self._stack.api_url.rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{base}/market/candles?symbol=XAUUSD&timeframe={self._timeframe}", timeout=3
            ) as r:
                data = json.loads(r.read().decode("utf-8"))
            self._chart.set_candles(data.get("bars", []))
        except Exception:
            pass
        try:
            with urllib.request.urlopen(
                f"{base}/market/price?symbol=XAUUSD", timeout=3
            ) as r:
                p = json.loads(r.read().decode("utf-8"))
            bid = p.get("bid")
            ask = p.get("ask")
            if bid is not None and ask is not None:
                self._live_price = (float(bid) + float(ask)) / 2.0
                self._chart.set_live_price(self._live_price)
        except Exception:
            pass

    # ---- events ------------------------------------------------------
    def _on_tf_changed(self, tf: str) -> None:
        self._timeframe = tf
        self._refresh_market()

    def _on_side_changed(self, side: str) -> None:
        self._side = side
        self._recompute()

    def _arm(self) -> None:
        ref = self._live_price if self._live_price is not None else 4500.0
        self._chart.arm_order_lines(entry=ref, line_a=ref + 10.0, line_b=ref - 10.0)
        self._recompute()

    def _recompute(self) -> None:
        if not self._chart.is_armed():
            self._exec_btn.setEnabled(False)
            return
        entry, a, b = self._chart.line_values()
        try:
            sl, tp = assign_sl_tp(self._side, entry=entry, line_a=a, line_b=b)
        except ValueError as e:
            self._summary.setText(f"⚠ {e}")
            self._exec_btn.setEnabled(False)
            self._last_sizing = None
            return
        balance = self._balance()
        sizing = compute_lot(
            balance=balance, lot_per_100=self._lot_per_100.value(),
            risk_cap_pct=self._risk_cap.value(), entry=entry, sl=sl,
            contract_size=_CONTRACT_SIZE, lot_step=_LOT_STEP,
            lot_min=_LOT_MIN, lot_max=_LOT_MAX,
        )
        self._last_sizing = sizing
        pending = infer_pending(entry=entry,
                                live_price=self._live_price or entry, tol=_ENTRY_TOL)
        otype = "LIMIT" if pending else "MARKET"
        if sizing.blocked:
            self._summary.setText(f"⚠ {sizing.blocked}")
            self._exec_btn.setEnabled(False)
            return
        self._summary.setText(
            f"{self._side} {otype}  lot={sizing.final_lot}\n"
            f"entry={entry:.2f}  SL={sl:.2f}  TP={tp:.2f}\n"
            f"risk=${sizing.risk_at_final:.2f} ({sizing.risk_pct_of_balance}% of bal)"
        )
        self._exec_btn.setEnabled(True)

    def _execute(self) -> None:
        if not self._chart.is_armed() or self._last_sizing is None:
            return
        entry, a, b = self._chart.line_values()
        sl, tp = assign_sl_tp(self._side, entry=entry, line_a=a, line_b=b)
        pending = infer_pending(entry=entry,
                                live_price=self._live_price or entry, tol=_ENTRY_TOL)
        lot = self._last_sizing.final_lot
        confirm = QMessageBox.question(
            self, "Confirm manual trade",
            f"{self._side} {'LIMIT' if pending else 'MARKET'} XAUUSD\n"
            f"lot={lot}  entry={entry:.2f}  SL={sl:.2f}  TP={tp:.2f}\n"
            f"risk=${self._last_sizing.risk_at_final:.2f}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        body = build_manual_open_body(
            side=self._side, entry=entry, sl=sl, tp=tp, lot=lot, pending=pending,
        )
        try:
            res = submit_manual_open(self._stack.api_url, body)
        except Exception as e:  # noqa: BLE001 - surface any transport/validation error
            QMessageBox.critical(self, "Manual trade failed", str(e))
            return
        QMessageBox.information(
            self, "Manual trade sent",
            f"action_id={res.get('action_id')} status={res.get('status')}",
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._poll.stop()
        super().closeEvent(event)
