"""Reusable stat-card widget that follows the active theme.

Replaces the duplicated ``_stat_box`` helpers in journal/cost/risk/
rejected/replay views. Connects to ``theme.bus.theme_changed`` and
restyles itself when the user toggles light/dark.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QFrame, QLabel, QVBoxLayout


class StatCard(QFrame):
    def __init__(
        self,
        label: str,
        value: str = "—",
        accent: str = "",       # "" = use text color; "success" / "danger" / "warning" / accent
        width: int = 140,
        height: int = 80,
    ) -> None:
        super().__init__()
        self._accent_key = accent
        self.setObjectName("StatCard")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setFixedSize(width, height)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 8)
        layout.setSpacing(2)
        self._label = QLabel(label.upper())
        self._value = QLabel(value)
        self._value.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self._value.setWordWrap(True)
        layout.addWidget(self._label)
        layout.addWidget(self._value)
        layout.addStretch()
        self._restyle()
        from src.gui.theme import bus as theme_bus
        theme_bus.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _pal) -> None:
        # Guards against "Internal C++ object (StatCard) already deleted"
        # when views recreate stat cards on each refresh — the old
        # card's Python wrapper is still subscribed to theme_changed.
        try:
            self._restyle()
        except RuntimeError:
            pass

    def set_value(self, value: str, accent: str = "") -> None:
        self._value.setText(value)
        if accent != self._accent_key:
            self._accent_key = accent
            self._restyle()

    def _restyle(self) -> None:
        from src.gui.theme import current_palette
        p = current_palette()
        value_color = {
            "":        p.text,
            "accent":  p.accent,
            "success": p.success,
            "danger":  p.danger,
            "warning": p.warning,
        }.get(self._accent_key, p.text)
        self.setStyleSheet(
            f"QFrame#StatCard {{ background: {p.surface}; border: 1px solid {p.border};"
            f" border-radius: 4px; }}"
            f"QFrame#StatCard QLabel {{ border: none; background: transparent; }}"
        )
        self._label.setStyleSheet(
            f"color: {p.text_muted}; font-size: 9px; letter-spacing: 1px;"
            f" border: none; background: transparent;"
        )
        self._value.setStyleSheet(
            f"color: {value_color}; font-size: 16px; font-weight: 700;"
            f" border: none; background: transparent;"
        )
