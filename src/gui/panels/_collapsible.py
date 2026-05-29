"""Collapsible section widget: clickable header + togglable content.

Used by the Actions panel to hide the chip-filter rows under an
expandable header so they don't dominate vertical space when not in use.

Usage:

    panel = CollapsibleSection("Filters", expanded=False)
    panel.add_layout(some_layout)
    panel.add_widget(some_widget)

Header shows a ▾ / ▸ chevron + the title; click anywhere on the header
to toggle. The content visibility is plain show/hide (no animation) —
animation would compete with the rest of the GUI's snap-to-state
behavior and add complexity for no real readability win.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.gui.theme import current_palette


class CollapsibleSection(QWidget):
    """Header-controlled show/hide container. Header is a flat button so
    it gets keyboard activation + focus styling for free. Content sits in
    a child QFrame so callers can add either layouts or widgets via the
    `add_*` helpers without juggling the QFrame themselves."""

    toggled = Signal(bool)  # True = expanded

    def __init__(
        self,
        title: str,
        *,
        expanded: bool = False,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._title = title
        self._expanded = expanded
        self._build_ui()
        self._apply_state()

    def _build_ui(self) -> None:
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)
        # Header: chevron + title + (optional summary) on the right.
        # QPushButton(flat=True) keeps the keyboard activation behavior
        # of a button without painting a chrome rectangle.
        self._header = QPushButton()
        self._header.setFlat(True)
        self._header.setCursor(Qt.CursorShape.PointingHandCursor)
        _pal = current_palette()
        self._header.setStyleSheet(
            f"QPushButton {{ "
            f"  text-align: left; "
            f"  padding: 6px 4px; "
            f"  border: none; "
            f"  font-size: 11px; "
            f"  font-weight: 700; "
            f"  letter-spacing: 1px; "
            f"  color: {_pal.text}; "
            f"}}"
            f"QPushButton:hover {{ color: {_pal.text}; }}"
        )
        self._header.clicked.connect(self.toggle)
        outer.addWidget(self._header)

        self._content = QFrame()
        self._content.setFrameShape(QFrame.Shape.NoFrame)
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(4)
        # SizePolicy stays Preferred even when hidden so the parent
        # layout doesn't reflow into a smaller box visually.
        self._content.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        outer.addWidget(self._content)

    def _apply_state(self) -> None:
        chevron = "▾" if self._expanded else "▸"
        self._header.setText(f"{chevron}  {self._title}")
        self._content.setVisible(self._expanded)

    def add_layout(self, layout) -> None:
        self._content_layout.addLayout(layout)

    def add_widget(self, widget: QWidget) -> None:
        self._content_layout.addWidget(widget)

    def toggle(self) -> None:
        self.set_expanded(not self._expanded)

    def set_expanded(self, expanded: bool) -> None:
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self._apply_state()
        self.toggled.emit(expanded)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_summary(self, text: str) -> None:
        """Optional inline summary appended to the header — used to show
        active-filter count when the section is collapsed so the
        operator still sees something is filtering."""
        chevron = "▾" if self._expanded else "▸"
        suffix = f"   {text}" if text else ""
        self._header.setText(f"{chevron}  {self._title}{suffix}")
