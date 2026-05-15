"""Top-left channel switcher widget (Discord/Slack style dropdown)."""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QComboBox

from src.gui.services.stack_registry import Stack


# Sentinel data for the "+ New stack..." synthetic item at the bottom.
_NEW_STACK_SENTINEL = "__new_stack__"


class ChannelSwitcher(QComboBox):
    stack_change_requested = Signal(object)
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
        # Visual separator + sentinel for "add new"
        self.insertSeparator(self.count())
        self.addItem("+ New stack…", _NEW_STACK_SENTINEL)
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
        if data == _NEW_STACK_SENTINEL:
            # Snap back to whatever was active before the user clicked
            # the sentinel — we don't want the dropdown to display
            # "+ New stack…" while the wizard is up.
            self.blockSignals(True)
            self.setCurrentIndex(self._last_index)
            self.blockSignals(False)
            self.new_stack_requested.emit()
            return
        if isinstance(data, Stack):
            self._last_index = index
            self.stack_change_requested.emit(data)

    def set_active_silently(self, stack: Stack) -> None:
        self.blockSignals(True)
        idx = next((i for i, s in enumerate(self._stacks) if s.name == stack.name), 0)
        self.setCurrentIndex(idx)
        self._last_index = idx
        self.blockSignals(False)
