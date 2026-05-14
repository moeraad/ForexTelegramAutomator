"""First-launch stack picker dialog.

Lists stacks the user has set up via Add stack. On a fresh install the
list is empty - click Add stack to create the first one. Each stack
gets its own blank channel profile JSON named after the stack.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
)

from src.gui.services.stack_registry import (
    Stack,
    build_stack_for_new_entry,
    discover_stacks,
)
from src.gui.services.stacks_config_io import StackEntry, load_entries, save_entries
from src.gui.windows.telegram_wizard import _write_blank_channel_profile


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
        self._add_btn = QPushButton("+ Add stack")
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
        dlg = _AddStackDialog(parent=self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result_entry:
            return
        new_entry = dlg.result_entry
        existing = load_entries()
        if any(e.name == new_entry.name for e in existing):
            QMessageBox.warning(
                self, "Name in use",
                f"A stack named '{new_entry.name}' already exists. Pick a different name.",
            )
            return
        existing.append(new_entry)
        save_entries(existing)
        self._stacks = discover_stacks()
        self._refresh_list()
        for row in range(self.list_widget.count()):
            item = self.list_widget.item(row)
            stack: Stack = item.data(Qt.ItemDataRole.UserRole)
            if stack.name == new_entry.name:
                self.list_widget.setCurrentRow(row)
                break

    def _on_accept(self) -> None:
        item = self.list_widget.currentItem()
        if item is None:
            QMessageBox.information(
                self, "Select a stack",
                "Pick a stack from the list or click + Add stack first.",
            )
            return
        self.selected_stack = item.data(Qt.ItemDataRole.UserRole)
        self.accept()


class _AddStackDialog(QDialog):
    def __init__(self, parent: QDialog) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add stack")
        self.resize(420, 200)
        self.result_entry: StackEntry | None = None

        layout = QVBoxLayout(self)
        info = QLabel(
            "<span style='color:#586e75;'>A stack runs its own services, DB, "
            "and channel profile. Files live under "
            "<code>%APPDATA%/CopyTrades/&lt;name&gt;/</code>. A blank profile "
            "JSON is created at <code>channels/&lt;name&gt;.json</code> — fill "
            "it in later from the PROFILE view.</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        form = QFormLayout()
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. my-gold-stack")
        self._symbol = QLineEdit()
        self._symbol.setText("XAUUSD")
        form.addRow("Stack name", self._name)
        form.addRow("Symbol", self._symbol)
        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        name = self._name.text().strip()
        symbol = self._symbol.text().strip().upper() or "XAUUSD"
        if not name:
            QMessageBox.warning(self, "Required", "Stack name can't be empty.")
            return
        if any(ch in name for ch in r'\/:*?"<>|'):
            QMessageBox.warning(
                self, "Invalid name",
                "Stack name can't contain \\ / : * ? \" < > | (used as folder name).",
            )
            return
        _write_blank_channel_profile(name, symbol)
        stack = build_stack_for_new_entry(name, name)
        self.result_entry = StackEntry(
            name=stack.name,
            profile_path=str(stack.profile_path),
            project_path=str(stack.project_path),
            db_path="",
            service_names=list(stack.service_names),
        )
        self.accept()
