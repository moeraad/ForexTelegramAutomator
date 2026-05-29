"""Fluent InfoBar wrapper for service crash alerts.

Replaces the old hand-rolled red banner. Same public API as before
(``push_alert``, ``dismiss_service``, ``restart_requested``) so
MainWindow doesn't need to change.

Behavior:
- Each crash spawns a closable InfoBar.error with the service name,
  the tail-line summary, a "Restart" button, and a "Details" toggle
  that expands the full 10-line err-log tail underneath.
- ``dismiss_service(name)`` clears any open InfoBars for a service
  that just recovered.
- InfoBars stack vertically at the top of the host container.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from qfluentwidgets import (
    InfoBar,
    InfoBarIcon,
    InfoBarPosition,
    PushButton,
)

from src.gui.theme import current_palette


class CrashBanner(QWidget):
    """Hosts a stack of Fluent InfoBars, one per active crash alert."""

    restart_requested = Signal(str)   # service name

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self._bars: dict[str, InfoBar] = {}  # service -> bar (latest one wins)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 6, 8, 0)
        layout.setSpacing(6)
        self._host_layout = layout
        self._host = self  # InfoBar wants a parent widget to attach into.

    # --- Public API -------------------------------------------------------

    def push_alert(self, service: str, tail: list[str], log_path: str) -> None:
        # If we already have an InfoBar for this service, close it and
        # spawn a fresh one so the latest tail wins.
        old = self._bars.pop(service, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass

        # Strip ANSI from the headline summary too so colorlog/rich
        # decorations from the crashed service don't render as garbage.
        summary_line = (tail[-1] if tail else "(no stderr captured)")
        summary_line = _CrashContent._ANSI_RE.sub("", summary_line)
        summary_line = summary_line[:160] + ("…" if len(summary_line) > 160 else "")

        # Build a small custom widget for the InfoBar to host: details
        # toggle, restart button, log path.
        custom = _CrashContent(service, list(tail), log_path)
        custom.restart_clicked.connect(
            lambda s=service: self.restart_requested.emit(s)
        )

        bar = InfoBar.error(
            title=f"{service} stopped",
            content=summary_line,
            orient=Qt.Orientation.Vertical,  # let our custom widget sit below
            isClosable=True,
            position=InfoBarPosition.TOP_LEFT,
            duration=-1,                      # persistent; user dismisses
            parent=self,
        )
        bar.addWidget(custom)
        self._bars[service] = bar

    def dismiss_service(self, service: str) -> None:
        bar = self._bars.pop(service, None)
        if bar is not None:
            try:
                bar.close()
            except Exception:
                pass


class _CrashContent(QWidget):
    """The body inside the InfoBar — details toggle + restart button."""

    restart_clicked = Signal()

    # ANSI escape sequences (color codes from rich/colorlog) render as
    # garbage in QPlainTextEdit. Strip them on the way in so the tail
    # reads cleanly regardless of how the underlying service decorated
    # its output (REVIEW.md §3 Empty/loading/error states).
    import re as _re
    _ANSI_RE = _re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

    @classmethod
    def _strip_ansi(cls, lines: list[str]) -> list[str]:
        return [cls._ANSI_RE.sub("", line) for line in lines]

    def __init__(self, service: str, tail: list[str], log_path: str) -> None:
        super().__init__()
        self._service = service
        self._tail = self._strip_ansi(tail)
        self._log_path = log_path
        self._expanded = False
        self._build_ui()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 4, 0, 0)
        outer.setSpacing(6)

        row = QHBoxLayout()
        self._toggle = PushButton("Details ▾")
        self._toggle.setFixedWidth(96)
        self._toggle.clicked.connect(self._on_toggle)
        row.addWidget(self._toggle)
        self._restart = PushButton("Restart service")
        self._restart.setProperty("variant", "warning")
        self._restart.clicked.connect(self.restart_clicked)
        row.addWidget(self._restart)
        row.addStretch()
        outer.addLayout(row)

        self._path_lbl = QLabel(f"log: {self._log_path}")
        self._path_lbl.setStyleSheet(f"color: {current_palette().text_muted}; font-size: 11px;")
        self._path_lbl.hide()
        outer.addWidget(self._path_lbl)

        self._tail_view = QPlainTextEdit()
        self._tail_view.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self._tail_view.setFont(f)
        self._tail_view.setPlainText("\n".join(self._tail) or "(no stderr captured)")
        self._tail_view.setMaximumHeight(160)
        self._tail_view.hide()
        outer.addWidget(self._tail_view)

    def _on_toggle(self) -> None:
        self._expanded = not self._expanded
        self._path_lbl.setVisible(self._expanded)
        self._tail_view.setVisible(self._expanded)
        self._toggle.setText("Details ▴" if self._expanded else "Details ▾")
