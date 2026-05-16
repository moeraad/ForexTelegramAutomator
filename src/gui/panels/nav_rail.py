"""Left navigation rail using qfluentwidgets' NavigationInterface.

Drop-in replacement for the old QToolButton-based rail. Preserves the
same public surface so MainWindow doesn't need to change:

    nav = NavRail()
    nav.item_selected.connect(self._switch_view)
    nav.set_active("live")
"""
from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QWidget

from qfluentwidgets import (
    FluentIcon,
    NavigationInterface,
    NavigationItemPosition,
)


# (route_key, FluentIcon, label)
_ITEMS = [
    ("live",     FluentIcon.SPEED_HIGH,        "Live"),
    ("journal",  FluentIcon.HISTORY,           "Journal"),
    ("rejected", FluentIcon.CANCEL,            "Rejected"),
    ("cost",     FluentIcon.PIE_SINGLE,        "Cost"),
    ("risk",     FluentIcon.CERTIFICATE,       "Risk"),
    ("replay",   FluentIcon.UPDATE,            "Replay"),
    ("profile",  FluentIcon.PEOPLE,            "Profile"),
    ("prompts",  FluentIcon.CHAT,              "Prompts"),
    ("settings", FluentIcon.SETTING,           "Settings"),
]


class NavRail(QWidget):
    """Compatibility wrapper over qfluentwidgets.NavigationInterface."""

    item_selected = Signal(str)

    def __init__(self, active: str = "live") -> None:
        super().__init__()
        self.setObjectName("NavRail")
        self.setFixedWidth(64)

        # showMenuButton=False keeps the rail collapsed (icon-only).
        # showReturnButton=False — no back arrow.
        self._nav = NavigationInterface(
            self, showMenuButton=False, showReturnButton=False
        )
        self._nav.setExpandWidth(64)

        for route, icon, label in _ITEMS:
            self._nav.addItem(
                routeKey=route,
                icon=icon,
                text=label,
                onClick=lambda checked=False, k=route: self.item_selected.emit(k),
                selectable=True,
                position=NavigationItemPosition.TOP,
            )

        # Lay the nav widget directly in this container with no margins
        # so the rail stays exactly 64px wide.
        from PySide6.QtWidgets import QVBoxLayout
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._nav)

        self.set_active(active)

    def set_active(self, key: str) -> None:
        try:
            self._nav.setCurrentItem(key)
        except Exception:
            pass
