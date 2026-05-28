"""Top-left channel switcher widget (Discord/Slack style dropdown)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox

from src.gui.services.stack_registry import Stack


class ChannelSwitcher(QComboBox):
    stack_change_requested = Signal(object)
    # Kept for wiring compatibility (HeaderBar still forwards it, and
    # MainWindow / picker_window can still trigger the wizard via other
    # entrypoints). The picker itself no longer offers a "+ New stack…"
    # item per the operator's request — stack creation lives at startup
    # only via the picker_window flow.
    new_stack_requested = Signal()

    def __init__(self, stacks: list[Stack], current: Stack) -> None:
        super().__init__()
        self._stacks: list[Stack] = list(stacks)
        self._last_index = 0
        self.setMinimumWidth(180)
        self._rebuild_items(current)
        self.currentIndexChanged.connect(self._on_changed)

    def _rebuild_items(self, current: Stack | None) -> None:
        self.blockSignals(True)
        self.clear()
        for s in self._stacks:
            self.addItem(s.name, s)
        if current is not None:
            idx = next(
                (i for i, s in enumerate(self._stacks) if s.name == current.name), 0
            )
            self.setCurrentIndex(idx)
            self._last_index = idx
        self.blockSignals(False)

    def set_stacks(self, stacks: list[Stack], current: Stack) -> None:
        self._stacks = list(stacks)
        self._rebuild_items(current)

    def _on_changed(self, index: int) -> None:
        data = self.itemData(index)
        if isinstance(data, Stack):
            self._last_index = index
            self.stack_change_requested.emit(data)

    def set_active_silently(self, stack: Stack) -> None:
        self.blockSignals(True)
        idx = next((i for i, s in enumerate(self._stacks) if s.name == stack.name), 0)
        self.setCurrentIndex(idx)
        self._last_index = idx
        self.blockSignals(False)
