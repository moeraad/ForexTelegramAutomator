"""Red banner shown when a service crashes. Stacks unresolved alerts."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)


@dataclass
class _Alert:
    service: str
    tail: list[str]
    log_path: str


class CrashBanner(QWidget):
    """Single-line red banner with expandable details and Restart button."""

    restart_requested = Signal(str)   # service name

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._queue: deque[_Alert] = deque()
        self._current: _Alert | None = None
        self._expanded = False
        self._build_ui()
        self.hide()

    # --- UI ---------------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        bar = QFrame()
        bar.setStyleSheet(
            "QFrame { background-color: #dc322f; }"
        )
        bar_layout = QHBoxLayout(bar)
        bar_layout.setContentsMargins(12, 6, 8, 6)
        bar_layout.setSpacing(8)

        self._icon = QLabel("⚠")
        self._icon.setStyleSheet("color: white; font-size: 18px;")
        bar_layout.addWidget(self._icon)

        self._summary = QLabel("")
        self._summary.setStyleSheet("color: white;")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        bar_layout.addWidget(self._summary, 1)

        self._queue_label = QLabel("")
        self._queue_label.setStyleSheet("color: #fdf6e3;")
        bar_layout.addWidget(self._queue_label)

        self._toggle_btn = QPushButton("Details ▾")
        self._toggle_btn.setFlat(True)
        self._toggle_btn.setStyleSheet(
            "QPushButton { color: white; background: rgba(255,255,255,40); "
            "border: none; padding: 2px 10px; border-radius: 3px; }"
            "QPushButton:hover { background: rgba(255,255,255,80); }"
        )
        self._toggle_btn.clicked.connect(self._on_toggle)
        bar_layout.addWidget(self._toggle_btn)

        self._restart_btn = QPushButton("Restart service")
        self._restart_btn.setStyleSheet(
            "QPushButton { color: #dc322f; background: white; "
            "border: none; padding: 2px 10px; border-radius: 3px; "
            "font-weight: 600; }"
            "QPushButton:hover { background: #fdf6e3; }"
        )
        self._restart_btn.clicked.connect(self._on_restart)
        bar_layout.addWidget(self._restart_btn)

        self._dismiss_btn = QPushButton("✕")
        self._dismiss_btn.setFixedWidth(28)
        self._dismiss_btn.setStyleSheet(
            "QPushButton { color: white; background: transparent; "
            "border: none; font-size: 16px; }"
            "QPushButton:hover { background: rgba(255,255,255,40); }"
        )
        self._dismiss_btn.clicked.connect(self._on_dismiss)
        bar_layout.addWidget(self._dismiss_btn)

        layout.addWidget(bar)

        # Expandable detail pane
        self._detail_frame = QFrame()
        self._detail_frame.setStyleSheet(
            "QFrame { background-color: #fdf6e3; border-bottom: 1px solid #d6cfb8; }"
        )
        detail_layout = QVBoxLayout(self._detail_frame)
        detail_layout.setContentsMargins(12, 8, 12, 8)
        detail_layout.setSpacing(4)
        self._detail_path = QLabel("")
        self._detail_path.setStyleSheet("color: #586e75; font-size: 11px;")
        self._detail_tail = QPlainTextEdit()
        self._detail_tail.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self._detail_tail.setFont(f)
        self._detail_tail.setMaximumHeight(180)
        detail_layout.addWidget(self._detail_path)
        detail_layout.addWidget(self._detail_tail)
        self._detail_frame.hide()
        layout.addWidget(self._detail_frame)

    # --- Public API -------------------------------------------------------

    def push_alert(self, service: str, tail: list[str], log_path: str) -> None:
        alert = _Alert(service=service, tail=tail, log_path=log_path)
        self._queue.append(alert)
        if self._current is None:
            self._show_next()
        else:
            self._refresh_queue_label()

    def dismiss_service(self, service: str) -> None:
        """Drop all alerts for a service that recovered."""
        self._queue = deque(a for a in self._queue if a.service != service)
        if self._current and self._current.service == service:
            self._show_next()
        else:
            self._refresh_queue_label()

    # --- Internals --------------------------------------------------------

    def _show_next(self) -> None:
        if not self._queue:
            self._current = None
            self.hide()
            return
        self._current = self._queue.popleft()
        first = self._current.tail[-1] if self._current.tail else "(no stderr captured)"
        first = first[:160] + ("…" if len(first) > 160 else "")
        self._summary.setText(
            f"<b>{self._current.service}</b> stopped — "
            f"<span style='color:#fdf6e3;'>{_html_escape(first)}</span>"
        )
        self._detail_path.setText(f"log: {self._current.log_path}")
        self._detail_tail.setPlainText("\n".join(self._current.tail))
        self._detail_frame.setVisible(self._expanded)
        self._refresh_queue_label()
        self.show()

    def _refresh_queue_label(self) -> None:
        if len(self._queue) > 0:
            self._queue_label.setText(f"+{len(self._queue)} more")
            self._queue_label.show()
        else:
            self._queue_label.hide()

    def _on_toggle(self) -> None:
        self._expanded = not self._expanded
        self._detail_frame.setVisible(self._expanded)
        self._toggle_btn.setText("Details ▴" if self._expanded else "Details ▾")

    def _on_restart(self) -> None:
        if self._current is None:
            return
        self.restart_requested.emit(self._current.service)
        # Banner stays until the watcher signals recovered; user can also Dismiss.

    def _on_dismiss(self) -> None:
        self._show_next()


def _html_escape(s: str) -> str:
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )
