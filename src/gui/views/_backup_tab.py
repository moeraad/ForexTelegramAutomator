"""Settings → Backup tab.

Three buttons:
  - "Backup now"           writes a zip to the default backups folder
  - "Backup to…"           lets the operator pick a target directory
  - "Restore from zip…"    pick a zip, confirm, replace files

A small list of recent backups is shown for context.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.backup_io import (
    list_backups,
    make_backup,
    restore_backup,
)


def _fmt_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


class BackupTab(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self._build_ui()
        self._refresh_list()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(10)

        info = QLabel(
            "<span style='color:#787b86;'>Backs up every stack's DB, "
            "channel profile, and trades log to a single zip. Restoring "
            "requires an app restart afterwards. Secrets (bot token, AI "
            "keys) are DPAPI-encrypted to this Windows account — restore "
            "on a different account or machine will require re-running the "
            "setup wizard.</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        actions = QHBoxLayout()
        self._backup_btn = QPushButton("Backup now")
        self._backup_btn.clicked.connect(self._on_backup_default)
        actions.addWidget(self._backup_btn)
        self._backup_to_btn = QPushButton("Backup to…")
        self._backup_to_btn.clicked.connect(self._on_backup_to)
        actions.addWidget(self._backup_to_btn)
        self._restore_btn = QPushButton("Restore from zip…")
        self._restore_btn.clicked.connect(self._on_restore)
        actions.addWidget(self._restore_btn)
        actions.addStretch()
        layout.addLayout(actions)

        layout.addWidget(QLabel("Recent backups"))
        self._list = QListWidget()
        layout.addWidget(self._list, 1)

    def _refresh_list(self) -> None:
        self._list.clear()
        backups = list_backups()
        if not backups:
            self._list.addItem(QListWidgetItem("(no backups yet)"))
            return
        for path in backups[:20]:
            stat = path.stat()
            ts = datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M:%S")
            self._list.addItem(QListWidgetItem(
                f"{ts}  ·  {_fmt_size(stat.st_size)}  ·  {path}"
            ))

    def _on_backup_default(self) -> None:
        try:
            result = make_backup()
        except OSError as e:
            QMessageBox.critical(self, "Backup failed", str(e))
            return
        QMessageBox.information(
            self, "Backup complete",
            f"Wrote {result.zip_path}\n\n"
            f"{result.stack_count} stack(s)  ·  {_fmt_size(result.total_bytes)}",
        )
        self._refresh_list()

    def _on_backup_to(self) -> None:
        target = QFileDialog.getExistingDirectory(
            self, "Choose backup folder",
        )
        if not target:
            return
        try:
            result = make_backup(target_dir=Path(target))
        except OSError as e:
            QMessageBox.critical(self, "Backup failed", str(e))
            return
        QMessageBox.information(
            self, "Backup complete",
            f"Wrote {result.zip_path}\n\n"
            f"{result.stack_count} stack(s)  ·  {_fmt_size(result.total_bytes)}",
        )
        self._refresh_list()

    def _on_restore(self) -> None:
        path_str, _ = QFileDialog.getOpenFileName(
            self, "Pick a backup zip", "", "Zip (*.zip)",
        )
        if not path_str:
            return
        ans = QMessageBox.warning(
            self, "Restore — confirm",
            "Restore will OVERWRITE the current %APPDATA%/CopyTrades/ files.\n\n"
            "You should close the GUI services tab and stop services first.\n"
            "After restore, restart the app for the changes to take effect.\n\n"
            "Proceed?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        )
        if ans != QMessageBox.StandardButton.Yes:
            return
        result = restore_backup(Path(path_str))
        if not result.success:
            QMessageBox.critical(self, "Restore failed", result.error)
            return
        msg = f"Restored {result.stack_count} stack(s)."
        if result.warnings:
            msg += "\n\nWarnings:\n  · " + "\n  · ".join(result.warnings[:6])
        msg += "\n\nRestart the app to load the restored files."
        QMessageBox.information(self, "Restore complete", msg)
        self._refresh_list()
