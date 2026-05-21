"""Toggleable filter chip widget.

A chip is a small pill button that holds an on/off state. The user
toggles it to add or remove its filter dimension from the active
query. Chips use the palette tokens so they recolor on theme swap.

API:

    chip = Chip("OPEN", value="OPEN")
    chip.toggled_with_value.connect(handler)  # (value, checked)
    chip.set_checked(True)
"""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import QPushButton


def _palette():
    """Cached palette read — returns None when theme isn't importable
    (early init / tests)."""
    try:
        from src.gui.theme import current_palette
        return current_palette()
    except Exception:
        return None


class Chip(QPushButton):
    """Pill-shaped toggle button used in the filter bar.

    Inherits QPushButton so it gets keyboard activation + focus styling
    for free. `setCheckable(True)` flips on click; we wrap the
    toggled signal so handlers receive the chip's `value` directly
    rather than having to inspect the sender.

    `value` is an arbitrary tag the caller stores on the chip — usually
    the underlying filter key (e.g., "OPEN", "BUY", "rejected", "7d").
    """

    toggled_with_value = Signal(object, bool)

    def __init__(
        self,
        label: str,
        *,
        value: object,
        color_token: str | None = None,
        parent=None,
    ) -> None:
        super().__init__(label, parent=parent)
        self._value = value
        self._color_token = color_token
        self.setCheckable(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFlat(True)
        self.toggled.connect(
            lambda checked: self.toggled_with_value.emit(self._value, checked)
        )
        self._apply_style()
        try:
            from src.gui.theme import bus as _bus
            _bus.theme_changed.connect(lambda _p: self._apply_style())
        except Exception:
            pass

    def value(self) -> object:
        return self._value

    def set_checked(self, checked: bool) -> None:
        """Update visual state without firing the toggled signal.
        Used when the parent restores a preset or clears all chips —
        the chip's state shouldn't re-fire the filter pipeline."""
        if self.isChecked() == checked:
            return
        self.blockSignals(True)
        try:
            self.setChecked(checked)
        finally:
            self.blockSignals(False)

    def _apply_style(self) -> None:
        pal = _palette()
        if pal is None:
            return
        # Pick a chip accent — either the optional explicit token
        # (so a side:BUY chip can be green, side:SELL red) or the
        # palette's accent color.
        accent = None
        if self._color_token:
            accent = getattr(pal, self._color_token, None)
        accent = accent or pal.accent
        # Translucent fill when checked; subtle outline when not.
        # Palette tokens are hex strings; rgba()-style alpha is appended
        # by converting the hex to "r, g, b" via QColor.
        from PySide6.QtGui import QColor
        c = QColor(accent)
        rgb = f"{c.red()},{c.green()},{c.blue()}"
        self.setStyleSheet(
            f"QPushButton {{ "
            f"  padding: 4px 12px; "
            f"  border-radius: 10px; "
            f"  border: 1px solid rgba({rgb}, 0.45); "
            f"  color: {pal.text_muted}; "
            f"  background: transparent; "
            f"  font-size: 11px; "
            f"  font-weight: 600; "
            f"}}"
            f"QPushButton:checked {{ "
            f"  background: rgba({rgb}, 0.18); "
            f"  border: 1px solid {accent}; "
            f"  color: {accent}; "
            f"}}"
            f"QPushButton:hover {{ "
            f"  border: 1px solid {accent}; "
            f"}}"
            f"QPushButton:checked:hover {{ "
            f"  background: rgba({rgb}, 0.25); "
            f"}}"
        )
