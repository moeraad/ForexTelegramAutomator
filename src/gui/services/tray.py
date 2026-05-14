"""System tray icon + menu + desktop notification helper."""
from __future__ import annotations

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


def _build_icon(halted: bool) -> QIcon:
    pix = QPixmap(32, 32)
    pix.fill(QColor("transparent"))
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    color = QColor("#dc322f") if halted else QColor("#859900")
    p.setBrush(color)
    p.setPen(QColor("#073642"))
    p.drawEllipse(4, 4, 24, 24)
    p.setPen(QColor("white"))
    f = p.font()
    f.setBold(True)
    f.setPointSize(11)
    p.setFont(f)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, "CT")
    p.end()
    return QIcon(pix)


class TrayController(QObject):
    show_window = Signal()
    toggle_halt = Signal()
    quit_requested = Signal()

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        app = QApplication.instance()
        if app is None or not QSystemTrayIcon.isSystemTrayAvailable():
            self._tray = None
            return
        self._tray = QSystemTrayIcon(_build_icon(False), parent)
        self._tray.setToolTip("CopyTrades")
        self._tray.activated.connect(self._on_activated)

        menu = QMenu()
        self._stack_label = menu.addAction("(no stack)")
        self._stack_label.setEnabled(False)
        menu.addSeparator()
        self._show_action = menu.addAction("Show window")
        self._show_action.triggered.connect(self.show_window.emit)
        self._halt_action = menu.addAction("Halt")
        self._halt_action.triggered.connect(self.toggle_halt.emit)
        menu.addSeparator()
        self._quit_action = menu.addAction("Quit CopyTrades")
        self._quit_action.triggered.connect(self.quit_requested.emit)

        self._tray.setContextMenu(menu)
        self._tray.show()

    def is_available(self) -> bool:
        return self._tray is not None

    def update_state(self, stack_name: str, halted: bool, today_pnl_text: str = "") -> None:
        if self._tray is None:
            return
        self._tray.setIcon(_build_icon(halted))
        tooltip = f"CopyTrades — {stack_name}"
        if halted:
            tooltip += "  ·  HALTED"
        if today_pnl_text:
            tooltip += f"\nToday: {today_pnl_text}"
        self._tray.setToolTip(tooltip)
        self._stack_label.setText(f"Stack: {stack_name}")
        self._halt_action.setText("Resume (clear halt)" if halted else "Halt")

    def notify(self, title: str, body: str, severity: str = "info") -> None:
        if self._tray is None:
            return
        icon_map = {
            "info": QSystemTrayIcon.MessageIcon.Information,
            "warning": QSystemTrayIcon.MessageIcon.Warning,
            "error": QSystemTrayIcon.MessageIcon.Critical,
        }
        self._tray.showMessage(
            title,
            body,
            icon_map.get(severity, QSystemTrayIcon.MessageIcon.Information),
            5000,
        )

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.show_window.emit()
