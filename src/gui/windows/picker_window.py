"""Stack picker dialog.

Lists every stack registered in stacks_config.json. Used at GUI startup
when more than one stack exists, OR when GUI_FORCE_PICKER=1.

"Add stack" launches the full 9-page setup wizard (TelegramWizard(None))
— same flow as first launch — so the new stack lands fully configured
(AI keys, bot token, Telegram session, channel pick, services).
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.gui.services.stack_registry import Stack, discover_stacks


class PickerWindow(QDialog):
    def __init__(self, stacks: list[Stack]) -> None:
        super().__init__()
        self.setWindowTitle("CopyTrades - Choose stack")
        self.resize(460, 360)
        self._stacks: list[Stack] = list(stacks)
        self.selected_stack: Stack | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("Pick an existing stack, or add a new one."))

        self.list_widget = QListWidget()
        self._refresh_list()
        layout.addWidget(self.list_widget, 1)

        add_row = QHBoxLayout()
        add_row.addStretch()
        self._add_btn = QPushButton("+ New stack…")
        self._add_btn.setProperty("variant", "success")
        self._add_btn.setToolTip(
            "Open the full setup wizard to create and configure a new stack."
        )
        self._add_btn.clicked.connect(self._on_add_stack)
        add_row.addWidget(self._add_btn)
        layout.addLayout(add_row)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.list_widget.itemDoubleClicked.connect(lambda _i: self._on_accept())

    def _refresh_list(self) -> None:
        self.list_widget.clear()
        for stack in self._stacks:
            item = QListWidgetItem(
                f"{stack.name}   .   profile: {stack.profile_path.stem}"
            )
            item.setData(Qt.ItemDataRole.UserRole, stack)
            self.list_widget.addItem(item)
        if self._stacks:
            self.list_widget.setCurrentRow(0)

    def _on_add_stack(self) -> None:
        # Full setup wizard — same flow as first launch.
        from src.gui.windows.telegram_wizard import TelegramWizard
        wiz = TelegramWizard(None, self)
        result = wiz.exec()
        if not result or wiz.stack is None:
            return
        # Refresh local list + select the new stack.
        self._stacks = discover_stacks()
        self._refresh_list()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            stack: Stack = item.data(Qt.ItemDataRole.UserRole)
            if stack.name == wiz.stack.name:
                self.list_widget.setCurrentRow(row)
                break

    def _on_accept(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            QMessageBox.information(
                self, "Select a stack",
                "Pick a stack from the list or click + New stack first.",
            )
            return
        self.selected_stack = item.data(Qt.ItemDataRole.UserRole)
        self.accept()
