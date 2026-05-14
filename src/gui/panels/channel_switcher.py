"""Top-left channel switcher widget (Discord/Slack style dropdown)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox

from src.gui.services.stack_registry import Stack


class ChannelSwitcher(QComboBox):
    stack_change_requested = Signal(object)

    def __init__(self, stacks: list[Stack], current: Stack) -> None:
        super().__init__()
        self._stacks = stacks
        self.setMinimumWidth(180)
        for s in stacks:
            self.addItem(s.name, s)
        idx = next((i for i, s in enumerate(stacks) if s.name == current.name), 0)
        self.setCurrentIndex(idx)
        self.currentIndexChanged.connect(self._on_changed)

    def _on_changed(self, index: int) -> None:
        if 0 <= index < len(self._stacks):
            self.stack_change_requested.emit(self._stacks[index])

    def set_active_silently(self, stack: Stack) -> None:
        self.blockSignals(True)
        idx = next((i for i, s in enumerate(self._stacks) if s.name == stack.name), 0)
        self.setCurrentIndex(idx)
        self.blockSignals(False)
