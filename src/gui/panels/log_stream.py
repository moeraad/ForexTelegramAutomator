"""LOG STREAM panel: live tail of api/bot/listener/api_http with filter + grep."""
from __future__ import annotations

import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QFont, QTextCharFormat, QTextCursor
from PySide6.QtWidgets import (
    QButtonGroup,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.log_tailer import LogTailer
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


_SOURCES: list[tuple[str, str]] = [
    # (key, filename)
    # All resolved against %APPDATA%/CopyTrades/<stack>/logs/<file>
    # (i.e. Path(stack.db_path).parent / "logs"), where both app loggers
    # and NSSM stdout/stderr write. See bootstrap_services_install.py.
    ("system", "system.log"),
    ("trades", "trades.log"),
    ("nssm-api", "nssm-api.err.log"),
    ("nssm-bot", "nssm-bot.err.log"),
    ("nssm-listener", "nssm-listener.err.log"),
]

_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_LEVEL_ORDER = {lvl: i for i, lvl in enumerate(_LEVELS)}

_LEVEL_COLOR = {
    "DEBUG": QColor("#787b86"),
    "INFO": QColor("#d1d4dc"),
    "WARNING": QColor("#ff9800"),
    "ERROR": QColor("#ef5350"),
    "CRITICAL": QColor("#ef5350"),
}

_MAX_LINES = 5000


def _detect_level(line: str) -> str:
    for level in _LEVELS:
        if f" {level} " in line or f"[{level}]" in line or f" {level}:" in line:
            return level
    return "INFO"


class LogStream(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._active_source = "api"
        self._min_level = "INFO"
        self._grep = ""
        self._paused = False
        self._line_count = 0
        self._window_start = time.monotonic()
        self._tailer: LogTailer | None = None
        self._build_ui()
        # Default first source. Falls back to 'api' for backward
        # compatibility if `_SOURCES` ever omits 'system' again.
        _default = "system" if any(k == "system" for k, _f in _SOURCES) else _SOURCES[0][0]
        self._switch_source(_default)

        self._rate_timer = QTimer(self)
        self._rate_timer.setInterval(2000)
        self._rate_timer.timeout.connect(self._update_rate_label)
        self._rate_timer.start()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._reset_view()
        self._switch_source(self._active_source)

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._tailer is not None:
            self._tailer.stop()
        super().closeEvent(event)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        header = QHBoxLayout()
        header.addWidget(QLabel("LOG STREAM"))

        # Source selector — SegmentedWidget gives a connected tab-bar
        # look matching the Prompts view, while falling back to plain
        # radios when qfluentwidgets isn't importable (dev environments,
        # minimal installs).
        self._radio_group: QButtonGroup | None = None
        self._source_seg = None
        # Default-checked source key. system is the catch-all per-stack
        # log so it's the safest first impression for a new operator.
        default_key = "system" if any(
            k == "system" for k, _f in _SOURCES
        ) else _SOURCES[0][0]
        try:
            from qfluentwidgets import SegmentedWidget
            self._source_seg = SegmentedWidget(self)
            for key, _filename in _SOURCES:
                # See PromptsView for the lambda signature — the
                # SegmentedWidget routes `clicked(bool)` into onClick;
                # without accepting & discarding the bool we'd pass it
                # in place of the route key.
                self._source_seg.addItem(
                    routeKey=key,
                    text=key,
                    onClick=lambda _clicked=False, _k=key: self._switch_source(_k),
                )
            self._source_seg.setCurrentItem(default_key)
            header.addWidget(self._source_seg)
        except Exception:
            self._radio_group = QButtonGroup(self)
            for key, _filename in _SOURCES:
                rb = QRadioButton(key)
                rb.setProperty("source_key", key)
                self._radio_group.addButton(rb)
                header.addWidget(rb)
                if key == default_key:
                    rb.setChecked(True)
            self._radio_group.buttonClicked.connect(self._on_source_changed)

        header.addStretch()

        header.addWidget(QLabel("Level:"))
        self._level_combo = QComboBox()
        for lvl in _LEVELS:
            self._level_combo.addItem(lvl, lvl)
        self._level_combo.setCurrentText("INFO")
        self._level_combo.currentTextChanged.connect(self._on_level_changed)
        header.addWidget(self._level_combo)

        header.addWidget(QLabel("grep:"))
        self._grep_edit = QLineEdit()
        self._grep_edit.setMaximumWidth(180)
        self._grep_edit.textChanged.connect(self._on_grep_changed)
        header.addWidget(self._grep_edit)

        self._pause_btn = QPushButton("Pause")
        self._pause_btn.setCheckable(True)
        self._pause_btn.toggled.connect(self._on_pause_toggled)
        header.addWidget(self._pause_btn)

        clear_btn = QPushButton("Clear")
        clear_btn.clicked.connect(self._clear_view)
        header.addWidget(clear_btn)

        self._rate_lbl = QLabel("· tailing")
        self._rate_lbl.setStyleSheet(f"color: {current_palette().text_muted}; padding-left: 8px;")
        header.addWidget(self._rate_lbl)

        layout.addLayout(header)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setMaximumBlockCount(_MAX_LINES)
        font = QFont("Consolas")
        font.setStyleHint(QFont.StyleHint.Monospace)
        font.setPointSize(9)
        self._view.setFont(font)
        # Inherits global QSS surface color; no override needed.
        layout.addWidget(self._view, 1)

    def _on_source_changed(self, btn) -> None:
        key = btn.property("source_key")
        if isinstance(key, str):
            self._switch_source(key)

    def _on_level_changed(self, text: str) -> None:
        if text in _LEVEL_ORDER:
            self._min_level = text

    def _on_grep_changed(self, text: str) -> None:
        self._grep = text.strip()

    def _on_pause_toggled(self, paused: bool) -> None:
        self._paused = paused
        self._pause_btn.setText("Resume" if paused else "Pause")

    def _clear_view(self) -> None:
        self._view.clear()

    def _reset_view(self) -> None:
        self._line_count = 0
        self._window_start = time.monotonic()
        self._view.clear()

    def _switch_source(self, key: str) -> None:
        if self._tailer is not None:
            try:
                self._tailer.line.disconnect(self._on_line)
                self._tailer.rotated.disconnect(self._on_rotated)
            except (TypeError, RuntimeError):
                pass
            self._tailer.stop()
            self._tailer.deleteLater()

        entry = next(((f,) for k, f in _SOURCES if k == key), None)
        if entry is None:
            return
        (filename,) = entry
        self._active_source = key
        self._reset_view()
        path = self._stack.db_path.parent / "logs" / filename
        self._tailer = LogTailer(path, parent=self)
        self._tailer.line.connect(self._on_line)
        self._tailer.rotated.connect(self._on_rotated)
        if not path.exists():
            self._append_meta(f"(no file at {path} yet — tailing once it appears)")
        self._tailer.start()

    def _on_line(self, line: str) -> None:
        if self._paused:
            return
        if self._grep and self._grep.lower() not in line.lower():
            return
        line_level = _detect_level(line)
        if _LEVEL_ORDER[line_level] < _LEVEL_ORDER[self._min_level]:
            return
        self._append_line(line, _LEVEL_COLOR.get(line_level))
        self._line_count += 1

    def _on_rotated(self) -> None:
        self._append_meta("--- log rotated ---")

    def _append_line(self, text: str, color: QColor | None) -> None:
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        fmt = QTextCharFormat()
        if color is not None:
            fmt.setForeground(color)
        cursor.insertText(text + "\n", fmt)
        self._auto_scroll()

    def _append_meta(self, text: str) -> None:
        fmt = QTextCharFormat()
        fmt.setForeground(QColor("#787b86"))
        fmt.setFontItalic(True)
        cursor = self._view.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text + "\n", fmt)
        self._auto_scroll()

    def _auto_scroll(self) -> None:
        bar = self._view.verticalScrollBar()
        if bar.value() >= bar.maximum() - 4:
            bar.setValue(bar.maximum())

    def _update_rate_label(self) -> None:
        now = time.monotonic()
        elapsed = max(0.001, now - self._window_start)
        lpm = (self._line_count / elapsed) * 60.0
        self._window_start = now
        self._line_count = 0
        suffix = "  ·  PAUSED" if self._paused else ""
        self._rate_lbl.setText(f"· {lpm:.0f} lines/min{suffix}")
