"""Compact Office-style ribbon bar: horizontal rows of icon+label
buttons grouped by function, with a small group caption underneath and
a 1-px vertical separator between groups.

Used by views that have a meaningful set of action buttons benefiting
from explicit grouping (Settings tabs first; others can adopt later).
Each tab owns its own `RibbonBar` instance; nothing in this module
talks to QTabWidget, so the bar can be embedded anywhere.

Layout per group:
  ┌────────┐  ┌────────┐
  │ 💾 Save │  │ ↻ Rel  │     ← buttons row (icon left, label right)
  └────────┘  └────────┘
       ─── Settings ───       ← group caption

Layout per bar (groups + separators + trailing stretch):
  [group A] │ [group B] │ [group C]                            (stretches)
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QSizePolicy,
    QToolButton,
    QVBoxLayout,
    QWidget,
)


@dataclass
class RibbonAction:
    """One ribbon button.

    icon: FluentIcon enum member name, e.g. "SAVE". Empty string for
          text-only fallback. Tint colour comes from variant.
    label: visible button text (kept short — fits the compact 44-px bar).
    tooltip: optional hover text (mostly redundant with label, but useful
             for clarifying multi-purpose actions).
    variant: "success" | "danger" | "warning" | "primary" | "" (default).
             Drives icon tint AND background via the existing styles.qss
             button-variant rules, so a destructive action reads red,
             a creation action green, etc.
    callback: zero-arg callable wired to clicked. None = inert (used
              when callers wire signals after construction).
    """
    icon: str
    label: str
    tooltip: str = ""
    variant: str = ""
    callback: Callable[[], None] | None = None


@dataclass
class RibbonGroup:
    """A named cluster of ribbon actions. Caption is rendered under the
    button row to make the grouping legible without a visual border."""
    title: str
    actions: list[RibbonAction] = field(default_factory=list)


class RibbonBar(QWidget):
    """Office-style ribbon strip. Icon stacked above the label inside
    each button so the icon stands clear of the text (the side-by-side
    layout merged the two visually on tight labels like "Stop ALL").
    Total height ~78 px when populated (button cell 58 + caption 10 +
    padding)."""

    def __init__(self, groups: list[RibbonGroup], parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._buttons: list[QPushButton] = []
        # (button, action) pairs so we can re-style each one with fresh
        # palette tokens when the user toggles dark/light.
        self._styled_buttons: list[tuple[QToolButton, RibbonAction]] = []
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setFixedHeight(86)
        self.setObjectName("RibbonBar")
        # CRITICAL: a bare QWidget ignores QSS background by default.
        # WA_StyledBackground enables QSS-driven painting; without it
        # the background-color / border rules silently no-op (which is
        # why the earlier "card style" pass looked indistinguishable).
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self._apply_frame_style()
        # Live theme swap — both the bar AND each button get re-styled.
        try:
            from src.gui.theme import bus as theme_bus
            theme_bus.theme_changed.connect(lambda _pal: self._on_theme_changed())
        except Exception:
            pass

        # Outer layout: wraps the button row in a small inset so the
        # background card has visible padding around the contents.
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(0)

        for i, g in enumerate(groups):
            layout.addWidget(self._build_group(g))
            if i < len(groups) - 1:
                layout.addWidget(self._build_separator())
        layout.addStretch(1)

    def _on_theme_changed(self) -> None:
        """Re-apply the frame style AND re-tint every button so colours
        swap atomically when the user toggles dark/light.

        theme_bus is a process-global signal; in tests the QWidget can
        be destroyed before this fires. Guard so a stale connection
        doesn't blow up on `Internal C++ object already deleted`.
        """
        try:
            self._apply_frame_style()
        except RuntimeError:
            return
        for btn, action in self._styled_buttons:
            try:
                self._restyle_button(btn, action)
            except RuntimeError:
                # Button C++ side has been freed (test teardown). The
                # rest of the list is likely in the same state — bail.
                return

    def _apply_frame_style(self) -> None:
        """Paint the bar as a discrete Office-style toolbar.

        Contrast pattern, both light AND dark:
            page bg   →   ribbon panel = surface (lighter / "raised")
                      →   buttons inside = bg (darker, tinted)

        In light mode: ribbon is near-white surface (#F0F1F4), buttons
        are the slightly gray page-bg (#E1E2E7) — reads as a white
        toolbar with gray button tiles.

        In dark mode: ribbon is slightly raised navy (#0F1230), buttons
        are the deeper page-bg navy (#07091A) — same relationship, just
        inverted lightness. Both modes give the same "tinted button on
        cleaner panel" feel.
        """
        try:
            from src.gui.theme import current_palette
            pal = current_palette()
            self.setStyleSheet(
                f"QWidget#RibbonBar {{"
                f"  background-color: {pal.surface};"
                f"  border: 1px solid {pal.border_strong};"
                f"  border-radius: 4px;"
                f"}}"
            )
        except Exception:
            pass

    def buttons(self) -> list[QPushButton]:
        """All buttons in declaration order — handy for tests / external
        wiring (e.g. enabling/disabling on selection)."""
        return list(self._buttons)

    # ---- internals ----------------------------------------------------

    def _build_group(self, g: RibbonGroup) -> QWidget:
        # The group's cell + button-row containers must stay TRANSPARENT
        # so the ribbon panel's surface colour shows through both above
        # the captions AND between groups. Without explicit
        # `background: transparent`, the global QSS cascade can paint
        # them with the surface_alt or button-bg token (Qt+QSS treats
        # an unstyled QWidget child as polish-able), producing a visible
        # grey band stretching the whole height of the buttons.
        cell = QWidget()
        cell.setStyleSheet("background: transparent;")
        v = QVBoxLayout(cell)
        v.setContentsMargins(6, 0, 6, 0)
        v.setSpacing(1)

        row = QWidget()
        row.setStyleSheet("background: transparent;")
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(4)
        for action in g.actions:
            btn = self._build_button(action)
            self._buttons.append(btn)
            row_layout.addWidget(btn)
        v.addWidget(row)

        caption = QLabel(g.title.upper())
        caption.setAlignment(Qt.AlignmentFlag.AlignHCenter)
        caption.setProperty("role", "muted")
        # Caption also explicitly transparent — the global QLabel rules
        # don't set a bg, but being defensive about it costs nothing
        # and prevents future stylesheet edits from accidentally tinting
        # this strip.
        caption.setStyleSheet(
            "font-size: 9px; letter-spacing: 0.6px; padding: 0; background: transparent;"
        )
        v.addWidget(caption)
        return cell

    def _build_separator(self) -> QFrame:
        sep = QFrame()
        sep.setFrameShape(QFrame.Shape.VLine)
        sep.setFrameShadow(QFrame.Shadow.Plain)
        try:
            from src.gui.theme import current_palette
            sep.setStyleSheet(f"color: {current_palette().border}; background: {current_palette().border};")
        except Exception:
            pass
        sep.setFixedWidth(1)
        sep.setContentsMargins(0, 4, 0, 4)
        return sep

    def _build_button(self, a: RibbonAction) -> QPushButton:
        """Transparent tool-button: colour-tinted FluentIcon on the
        left, plain theme-text label on the right. Same visual
        vocabulary as the header services-bar + refresh icons. The
        icon carries the semantic (variant) colour; the text doesn't
        need to fight a solid background. Hover gets a subtle rgba
        backdrop the way the refresh button does.

        Falls back to a normal QPushButton with the variant background
        when qfluentwidgets isn't available — affordance still works
        and the colour is still semantic, just less elegant.
        """
        try:
            from qfluentwidgets import FluentIcon  # noqa: F401 — import probe
            # Plain QToolButton (NOT TransparentToolButton — that
            # qfluentwidgets class overrides the paint pipeline and
            # silently ignores ToolButtonTextUnderIcon, drawing the
            # label either beside the icon or on top of it). Vanilla
            # QToolButton honours the under-icon layout correctly.
            btn = QToolButton()
            btn.setText(a.label)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
            btn.setFixedSize(64, 58)
            btn.setIconSize(QSize(22, 22))
            # Apply theme-driven QSS + icon tint in one place so the
            # same code path runs on theme-change re-style.
            self._restyle_button(btn, a)
            # Remember the pair so _on_theme_changed can re-tint later.
            self._styled_buttons.append((btn, a))
        except Exception:
            btn = QPushButton(a.label)
            btn.setMinimumHeight(26)
            if a.variant:
                btn.setProperty("variant", a.variant)

        if a.tooltip:
            btn.setToolTip(a.tooltip)
        if a.callback is not None:
            btn.clicked.connect(lambda _checked=False, cb=a.callback: cb())
        return btn

    def _restyle_button(self, btn: QToolButton, a: RibbonAction) -> None:
        """Apply theme-driven QSS background/border + variant-tinted
        FluentIcon to a single button. Called once at construction
        and again on every theme swap so colours stay coherent.
        """
        try:
            from qfluentwidgets import FluentIcon
            from src.gui.theme import current_palette
            pal = current_palette()
        except Exception:
            return
        # Buttons paint the slightly-darker bg token so each cell reads
        # as a tinted tile on the lighter ribbon panel. The tonal step
        # is uniform across light/dark — in light mode this is "gray
        # buttons on white panel"; in dark mode it's "darker navy
        # buttons on slightly raised navy panel".
        btn.setStyleSheet(
            "QToolButton {"
            f"  background: {pal.bg};"
            f"  border: 1px solid {pal.border};"
            "  border-radius: 4px;"
            "  padding: 4px 2px 2px 2px;"
            "  font-size: 10px;"
            f"  color: {pal.text};"
            "}"
            "QToolButton:hover {"
            f"  background: {pal.surface_hover};"
            f"  border-color: {pal.accent};"
            "}"
            "QToolButton:pressed {"
            f"  background: {pal.surface_pressed};"
            "}"
        )
        cmap = {
            "success": pal.success,
            "danger": pal.danger,
            "warning": pal.warning,
            "primary": pal.accent,
        }
        tint = cmap.get(a.variant, pal.text)
        glyph = getattr(FluentIcon, a.icon, None) if a.icon else None
        if glyph is not None:
            try:
                btn.setIcon(glyph.icon(color=QColor(tint)))
            except Exception:
                btn.setIcon(glyph.icon())
