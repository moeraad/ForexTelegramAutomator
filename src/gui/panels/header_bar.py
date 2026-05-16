"""Top header bar: channel switcher + summary + HALT toggle."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

from src.gui.panels.channel_switcher import ChannelSwitcher
from src.gui.services.halt_controller import HaltController
from src.gui.services.stack_registry import Stack


_HALT_STYLE = (
    "QPushButton { background-color: #ef5350; color: white; font-weight: 600; "
    "padding: 6px 14px; border-radius: 4px; } "
    "QPushButton:hover { background-color: #b3271f; }"
)
_RUN_STYLE = (
    "QPushButton { background-color: #26a69a; color: white; font-weight: 600; "
    "padding: 6px 14px; border-radius: 4px; } "
    "QPushButton:hover { background-color: #6d7e00; }"
)


class HeaderBar(QWidget):
    halt_toggled = Signal(bool)
    stack_change_requested = Signal(object)
    new_stack_requested = Signal()

    def __init__(
        self,
        stacks: list[Stack],
        current: Stack,
        halt: HaltController,
    ) -> None:
        super().__init__()
        self._halt = halt
        self.setFixedHeight(48)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)

        self.switcher = ChannelSwitcher(stacks, current)
        self.switcher.stack_change_requested.connect(self.stack_change_requested.emit)
        self.switcher.new_stack_requested.connect(self.new_stack_requested.emit)
        layout.addWidget(self.switcher)

        self.summary = QLabel("Today  +$0.00  ·  Signals 0  ·  Wins 0/0")
        self.summary.setStyleSheet("color: #787b86; padding-left: 16px;")
        layout.addWidget(self.summary)
        layout.addStretch()

        self.theme_btn = QPushButton()
        self.theme_btn.setFixedWidth(40)
        self.theme_btn.setToolTip("Toggle light / dark theme")
        self.theme_btn.clicked.connect(self._on_theme_clicked)
        layout.addWidget(self.theme_btn)
        self._refresh_theme_btn()

        self.halt_btn = QPushButton()
        self.halt_btn.setShortcut("Ctrl+H")
        self.halt_btn.clicked.connect(self._on_clicked)
        layout.addWidget(self.halt_btn)

        halt.state_changed.connect(self._on_state_changed)
        self._render(halt.is_halted())

        # Re-render whenever the global theme changes so the icon flips.
        from src.gui.theme import bus as theme_bus
        theme_bus.theme_changed.connect(lambda _pal: self._refresh_theme_btn())

    def _on_theme_clicked(self) -> None:
        from PySide6.QtWidgets import QApplication
        from src.gui.theme import apply_theme, current_palette, persist
        new = "light" if current_palette().name == "dark" else "dark"
        apply_theme(QApplication.instance(), new)
        persist(new)

    def _refresh_theme_btn(self) -> None:
        from src.gui.theme import current_palette
        # Sun glyph means "click to go light"; moon means "click to go dark".
        self.theme_btn.setText("☀" if current_palette().name == "dark" else "☾")

    def set_summary(self, text: str) -> None:
        self.summary.setText(text)

    def _on_clicked(self) -> None:
        halted = self._halt.toggle()
        self.halt_toggled.emit(halted)

    def _on_state_changed(self, halted: bool) -> None:
        self._render(halted)

    def _render(self, halted: bool) -> None:
        if halted:
            self.halt_btn.setText("⛔ HALTED — click to RUN")
            self.halt_btn.setStyleSheet(_HALT_STYLE)
        else:
            self.halt_btn.setText("● RUNNING — click to HALT")
            self.halt_btn.setStyleSheet(_RUN_STYLE)
        self.halt_btn.setToolTip("Ctrl+H  ·  flips settings.kill_switch for the active stack")
