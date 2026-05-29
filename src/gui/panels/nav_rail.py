"""Left navigation rail using qfluentwidgets' NavigationInterface.

Drop-in replacement for the old QToolButton-based rail. Preserves the
same public surface so MainWindow doesn't need to change:

    nav = NavRail()
    nav.item_selected.connect(self._switch_view)
    nav.set_active("live")
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QWidget

from qfluentwidgets import (
    FluentIcon,
    InfoBadge,
    InfoBadgePosition,
    NavigationInterface,
    NavigationItemPosition,
)


# (route_key, FluentIcon, label)
_ITEMS = [
    ("live",       FluentIcon.SPEED_HIGH,        "Live"),
    ("journal",    FluentIcon.HISTORY,           "Journal"),
    ("pipeline",   FluentIcon.FLAG,              "Pipeline"),
    ("evaluation", FluentIcon.SEARCH,            "Evaluation"),
    ("cost",       FluentIcon.PIE_SINGLE,        "Cost"),
    ("risk",       FluentIcon.CERTIFICATE,       "Risk"),
    ("replay",     FluentIcon.UPDATE,            "Replay"),
    ("profile",    FluentIcon.PEOPLE,            "Profile"),
    ("prompts",    FluentIcon.CHAT,              "Prompts"),
    ("v2_config",  FluentIcon.IOT,               "V2 Config"),
    ("routes",     FluentIcon.SHARE,             "Routes"),
    ("audit",      FluentIcon.ZOOM,              "Audit"),
    ("bindings",   FluentIcon.LINK,              "Bindings"),
    ("suggestions", FluentIcon.TILES,            "Suggestions"),
    ("settings",   FluentIcon.SETTING,           "Settings"),
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

        # Track item widgets per route so set_badge() can attach an
        # InfoBadge to the actual navigation widget (REVIEW.md §3 — the
        # previous "Live  ●3" text-suffix hack wasn't screen-reader
        # friendly because the count lived inside the label, not as a
        # discrete role).
        self._items: dict[str, object] = {}
        self._badges: dict[str, InfoBadge] = {}
        for route, icon, label in _ITEMS:
            widget = self._nav.addItem(
                routeKey=route,
                icon=icon,
                text=label,
                onClick=lambda checked=False, k=route: self.item_selected.emit(k),
                selectable=True,
                position=NavigationItemPosition.TOP,
                tooltip=label,
            )
            self._items[route] = widget
            # Set accessibleName so AT-SPI / NVDA / VoiceOver announce the
            # item by label even though the rail renders icon-only.
            # Also make the item Tab-focusable so the operator can drive
            # the app without a mouse (REVIEW.md §3 Accessibility).
            # `tooltip` is honoured by NavigationInterface.addItem above,
            # so hovering an icon shows the human-readable label.
            try:
                widget.setAccessibleName(label)
                widget.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
                # Belt + braces — also set toolTip directly in case the
                # qfluentwidgets `tooltip` kwarg isn't honoured by this
                # version.
                widget.setToolTip(label)
            except Exception:
                pass

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

    def set_badge(self, key: str, count: int | str | None) -> None:
        """Attach (or clear) an InfoBadge on the nav item.

        Implemented as a real qfluentwidgets InfoBadge anchored to the
        navigation widget. The label text stays clean and screen
        readers announce the count as a separate accessible element
        rather than as part of the item label (REVIEW.md §3 Accessibility).
        """
        target = self._items.get(key)
        if target is None:
            return
        # Clear case — drop any prior badge for this route.
        existing = self._badges.pop(key, None)
        if existing is not None:
            try:
                existing.deleteLater()
            except Exception:
                pass
        if count in (None, 0, "", "0"):
            return
        try:
            badge = InfoBadge.attension(
                text=str(count),
                parent=target,
                target=target,
                position=InfoBadgePosition.TOP_RIGHT,
            )
        except Exception:
            return
        # Accessible name so screen readers say e.g. "3 new" alongside
        # the item's accessibleName.
        try:
            badge.setAccessibleName(f"{count} new")
        except Exception:
            pass
        self._badges[key] = badge
