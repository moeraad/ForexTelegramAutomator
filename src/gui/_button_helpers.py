"""Shared button factories. Keeps icon wiring + variant tagging
out of every view module.

Conventions used across the app:
  variant="success" → green-tinted (Save, Install, Start, Add, Promote)
  variant="danger"  → red-tinted  (Cancel, Remove, Delete, Uninstall, Abort)
  variant="primary" → azure       (Search, Test, Run, Submit, Restore)
  variant="warning" → honey       (Revert, Restart service, Reload listener)
  no variant        → default neutral chrome (toggles, info, Show details)

Colours come from src/gui/styles.qss button-variant rules + the active
theme palette (DARK / LIGHT), so light/dark swap automatically.
"""
from __future__ import annotations

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QApplication, QPushButton, QStyle, QWidget


def make_icon_button(
    fluent_icon_name: str,
    tooltip: str,
    variant: str = "",
    fallback_text: str = "",
) -> QWidget:
    """28x28 transparent icon-only button matching the header services-bar
    style. Accepts any qfluentwidgets ``FluentIcon`` member name (e.g.
    "SAVE", "ADD", "DELETE", "PLAY") and tints with the variant colour
    (success → green, danger → red, primary → azure, warning → honey).
    No variant = accent.

    Falls back to a labelled QPushButton on stripped installs so the
    affordance is still operable.
    """
    try:
        from qfluentwidgets import FluentIcon, TransparentToolButton
        from src.gui.theme import current_palette
        btn = TransparentToolButton()
        btn.setFixedSize(28, 28)
        btn.setIconSize(QSize(18, 18))
        btn.setStyleSheet(
            "QToolButton { background: transparent; border: none; padding: 0; }"
            "QToolButton:hover { background: rgba(127, 127, 127, 0.12); border-radius: 4px; }"
        )
        pal = current_palette()
        colour_map = {
            "success": pal.success,
            "danger": pal.danger,
            "warning": pal.warning,
            "primary": pal.accent,
        }
        tint = colour_map.get(variant, pal.accent)
        glyph = getattr(FluentIcon, fluent_icon_name, None)
        if glyph is not None:
            try:
                btn.setIcon(glyph.icon(color=QColor(tint)))
            except Exception:
                btn.setIcon(glyph.icon())
    except Exception:
        # qfluentwidgets unavailable — degrade to a text button so the
        # action is still discoverable.
        btn = QPushButton(fallback_text or tooltip)
        if variant:
            btn.setProperty("variant", variant)
    btn.setToolTip(tooltip)
    return btn


def make_refresh_button(tooltip: str = "Refresh") -> QWidget:
    """Compact, icon-only refresh button that mirrors the header
    services-bar's transparent-toolbutton style: 28x28, no border, no
    background, accent-coloured FluentIcon, subtle rgba hover. Same
    visual language as the start/stop/restart trio so all icon-only
    affordances look like siblings.

    Falls back to QPushButton with the platform's SP_BrowserReload
    standard icon on stripped installs (no qfluentwidgets available),
    so the button still works and the affordance is still obvious.
    """
    try:
        from qfluentwidgets import FluentIcon, TransparentToolButton
        from src.gui.theme import current_palette
        btn = TransparentToolButton()
        btn.setFixedSize(28, 28)
        btn.setIconSize(QSize(18, 18))
        btn.setStyleSheet(
            "QToolButton { background: transparent; border: none; padding: 0; }"
            "QToolButton:hover { background: rgba(127, 127, 127, 0.12); border-radius: 4px; }"
        )
        try:
            btn.setIcon(FluentIcon.SYNC.icon(color=QColor(current_palette().accent)))
        except Exception:
            btn.setIcon(FluentIcon.SYNC.icon())
    except Exception:
        btn = QPushButton()
        app = QApplication.instance()
        if app is not None:
            btn.setIcon(app.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
            btn.setIconSize(QSize(14, 14))
        btn.setFixedSize(28, 28)
    btn.setToolTip(tooltip)
    return btn
