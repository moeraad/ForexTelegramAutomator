"""Splash window: per-stack parallel bootstrap progress."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.stack_registry import Stack


_STEPS = ("NSSM", "Services", "Running", "API")
_PILL_IDLE = "○"
_PILL_BUSY = "◐"
_PILL_OK = "●"
_PILL_FAIL = "✕"


class _StackRow(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._labels: dict[str, QLabel] = {}
        self._errors: list[str] = []
        layout = QHBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 4)
        name_lbl = QLabel(stack.name)
        name_lbl.setMinimumWidth(140)
        name_lbl.setStyleSheet("font-weight: 600;")
        layout.addWidget(name_lbl)
        for step in _STEPS:
            pill = QLabel(f"{_PILL_IDLE} {step}")
            pill.setMinimumWidth(110)
            self._labels[step] = pill
            layout.addWidget(pill)
        layout.addStretch()

    def on_started(self, step: str) -> None:
        self._set(step, _PILL_BUSY, "color: #b58900;")

    def on_succeeded(self, step: str) -> None:
        self._set(step, _PILL_OK, "color: #859900;")

    def on_failed(self, step: str, err: str) -> None:
        self._set(step, _PILL_FAIL, "color: #dc322f;")
        self._errors.append(f"{step}: {err}")

    def errors(self) -> list[str]:
        return list(self._errors)

    def _set(self, step: str, glyph: str, css: str) -> None:
        lbl = self._labels.get(step)
        if lbl is None:
            return
        lbl.setText(f"{glyph} {step}")
        lbl.setStyleSheet(css)


class SplashWindow(QWidget):
    skip_requested = Signal()
    abort_requested = Signal()

    def __init__(self, stacks: list[Stack]) -> None:
        super().__init__()
        self.setWindowTitle("CopyTrades — Starting")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(640, 80 + 36 * max(1, len(stacks)))
        self._rows: dict[str, _StackRow] = {}
        self._error_lbl = QLabel("")
        self._error_lbl.setStyleSheet("color: #dc322f; padding: 8px;")
        self._error_lbl.setWordWrap(True)
        self._error_lbl.setVisible(False)

        layout = QVBoxLayout(self)
        title = QLabel("Preparing CopyTrades")
        title.setStyleSheet("font-size: 16px; font-weight: 600;")
        title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(title)

        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.HLine)
        layout.addWidget(sep)

        for stack in stacks:
            row = _StackRow(stack)
            self._rows[stack.name] = row
            layout.addWidget(row)

        layout.addWidget(self._error_lbl)

        self.progress = QProgressBar()
        self.progress.setRange(0, max(1, len(stacks)))
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        buttons = QHBoxLayout()
        skip_btn = QPushButton("Skip")
        abort_btn = QPushButton("Abort")
        skip_btn.clicked.connect(self.skip_requested.emit)
        abort_btn.clicked.connect(self.abort_requested.emit)
        buttons.addStretch()
        buttons.addWidget(skip_btn)
        buttons.addWidget(abort_btn)
        layout.addLayout(buttons)

    def on_step_started(self, stack: str, step: str) -> None:
        row = self._rows.get(stack)
        if row:
            row.on_started(step)

    def on_step_succeeded(self, stack: str, step: str) -> None:
        row = self._rows.get(stack)
        if row:
            row.on_succeeded(step)

    def on_step_failed(self, stack: str, step: str, err: str) -> None:
        row = self._rows.get(stack)
        if row:
            row.on_failed(step, err)
        self._refresh_errors()

    def on_stack_completed(self, _stack: str) -> None:
        self.progress.setValue(self.progress.value() + 1)

    def _refresh_errors(self) -> None:
        lines: list[str] = []
        for name, row in self._rows.items():
            for err in row.errors():
                lines.append(f"{name}: {err}")
        if lines:
            self._error_lbl.setText("\n".join(lines))
            self._error_lbl.setVisible(True)
