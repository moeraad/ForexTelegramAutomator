"""Inline control to resize a pending (watching) OPEN order from the DETAIL panel."""
from __future__ import annotations

import json
import urllib.request

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.theme import current_palette


class _ResizeWorker(QThread):
    """POSTs the resize request off the UI thread."""
    done = Signal(bool, str)  # (ok, message)

    def __init__(self, url: str, lots: float) -> None:
        super().__init__()
        self._url = url
        self._lots = lots

    def run(self) -> None:
        try:
            data = json.dumps({"lots": self._lots}).encode()
            req = urllib.request.Request(
                self._url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode())
            self.done.emit(True, json.dumps(body))
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            try:
                detail = json.loads(e.read().decode()).get("detail", str(e))
            except Exception:
                detail = f"HTTP {e.code}"
            self.done.emit(False, str(detail))
        except Exception as e:  # noqa: BLE001 - surface any transport error inline
            self.done.emit(False, str(e))


class ResizeControl(QWidget):
    """Current-lot label + editable lot + Apply + risk warning.

    `risk_cap_pct` is the operator's per-trade SL-risk cap (max_sl_loss_percent)
    used only to decide whether to show the amber 'above cap' warning.
    """

    def __init__(self, api_base: str, action_id: int, current_lot: float,
                 risk_cap_pct: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api_base = api_base.rstrip("/")
        self._action_id = action_id
        self._risk_cap_pct = risk_cap_pct
        self._worker: _ResizeWorker | None = None

        pal = current_palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 6)

        row = QHBoxLayout()
        row.addWidget(QLabel(f"Lot ({current_lot:.2f}):"))
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(2)
        self._spin.setSingleStep(0.01)
        self._spin.setRange(0.01, 100.0)
        self._spin.setValue(current_lot if current_lot > 0 else 0.01)
        row.addWidget(self._spin)
        self._apply = QPushButton("Apply")
        self._apply.clicked.connect(self._on_apply)
        row.addWidget(self._apply)
        row.addStretch()
        root.addLayout(row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {pal.text_muted}; font-size: 11px;")
        root.addWidget(self._status)

    def _on_apply(self) -> None:
        if self._worker is not None and self._worker.isRunning():
            return
        lots = round(self._spin.value(), 2)
        self._apply.setEnabled(False)
        self._status.setText(f"Applying {lots:.2f}…")
        url = f"{self._api_base}/actions/{self._action_id}/resize_pending"
        self._worker = _ResizeWorker(url, lots)
        self._worker.done.connect(self._on_done)
        self._worker.finished.connect(self._worker.deleteLater)
        from src.gui.services.thread_registry import register
        register(self._worker, stop_fn=self._worker.quit)
        self._worker.start()

    def _on_done(self, ok: bool, message: str) -> None:
        self._apply.setEnabled(True)
        pal = current_palette()
        if not ok:
            self._status.setStyleSheet("color: #ef5350; font-size: 11px;")
            self._status.setText(f"Resize failed: {message}")
            return
        body = json.loads(message)
        pct = body.get("risk_pct_estimate")
        dollars = body.get("risk_dollars") or 0
        if pct is None:
            note = f"risks ≈ ${dollars:,.0f} if SL hits (balance unknown)"
            color = pal.text_muted
        elif pct > self._risk_cap_pct:
            note = (f"~{pct:.1f}% of balance — above your {self._risk_cap_pct:.1f}% "
                    f"cap (≈ ${dollars:,.0f})")
            color = "#ff9800"
        else:
            note = f"~{pct:.1f}% of balance (≈ ${dollars:,.0f})"
            color = "#26a69a"
        self._status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._status.setText(
            f"Resized to {body.get('lots', 0.0):.2f} "
            f"(seq {body.get('resize_seq', '?')}). {note}"
        )
