"""Light / dark theme management.

Defines two palettes (``LIGHT``, ``DARK``), an ``apply_theme()`` that:
  - renders ``styles.qss`` as a string.Template against the palette
  - calls ``qfluentwidgets.setTheme()`` so Fluent widgets follow along
  - persists the current name to ``state.json`` for next launch
  - emits a signal so any in-process widget that needs to re-theme can
    re-read ``current_palette()``

Widgets that hardcode colors (charts, inline ``setStyleSheet``) should
read tokens from the palette instead so they swap correctly.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from string import Template
from typing import Literal

from PySide6.QtCore import QObject, Signal


ThemeName = Literal["dark", "light"]


@dataclass(frozen=True)
class Palette:
    name: ThemeName
    # surfaces
    bg: str
    surface: str
    surface_alt: str
    surface_hover: str
    surface_pressed: str
    nav_bg: str
    # text
    text: str
    text_muted: str
    text_dim: str
    # accents
    accent: str
    accent_hover: str
    # status
    success: str
    danger: str
    warning: str
    info: str
    # borders / lines
    border: str
    border_strong: str
    grid: str
    # button-specific (lets dark mode "glow" without affecting light mode)
    button_bg: str
    button_hover_bg: str
    button_hover_border: str
    button_pressed_bg: str


DARK = Palette(
    name="dark",
    bg="#131722",
    surface="#1e222d",
    surface_alt="#1a1e28",
    surface_hover="#2a2e39",
    surface_pressed="#363a45",
    nav_bg="#0c0e15",
    text="#d1d4dc",
    text_muted="#787b86",
    text_dim="#5d6068",
    accent="#448aff",                # brighter electric blue
    accent_hover="#82b1ff",          # sky-blue glow on hover
    success="#00e676",               # neon green
    danger="#ff5252",                # hot coral
    warning="#ffd740",               # electric amber
    info="#40c4ff",                  # cyan
    border="#2a3144",                # slightly blue-tinted border
    border_strong="#4c525e",
    grid="#1e222d",
    button_bg="#1e222d",
    button_hover_bg="rgba(68, 138, 255, 0.18)",   # translucent blue glow
    button_hover_border="#448aff",
    button_pressed_bg="rgba(68, 138, 255, 0.30)",
)


LIGHT = Palette(
    name="light",
    bg="#f5f7fa",
    surface="#ffffff",
    surface_alt="#f0f2f5",
    surface_hover="#e8ecef",
    surface_pressed="#d4d7de",
    nav_bg="#1e222d",                # dark rail even in light mode — more usable
    text="#1e222d",
    text_muted="#5d6068",
    text_dim="#90939a",
    accent="#1976d2",                # deeper, calmer blue
    accent_hover="#1565c0",
    success="#00897b",               # teal — readable on white
    danger="#d32f2f",
    warning="#f57c00",
    info="#0277bd",
    border="#d4d7de",
    border_strong="#a8acb5",
    grid="#e8ecef",
    button_bg="#ffffff",
    button_hover_bg="#e3f2fd",       # pale blue
    button_hover_border="#1976d2",
    button_pressed_bg="#bbdefb",
)


_palettes = {"dark": DARK, "light": LIGHT}
_current: Palette = DARK


class _ThemeBus(QObject):
    """Emits ``theme_changed`` when the active palette swaps so widgets
    can rebuild their colors without a restart."""
    theme_changed = Signal(object)  # Palette


bus = _ThemeBus()


def current_palette() -> Palette:
    return _current


def palette_for(name: ThemeName) -> Palette:
    return _palettes.get(name, DARK)


def apply_theme(app, name: ThemeName) -> None:
    """Apply palette globally: QSS template + qfluentwidgets + signal."""
    global _current
    pal = _palettes.get(name)
    if pal is None:
        pal = DARK
    _current = pal

    # Tell qfluentwidgets — must happen BEFORE the QSS so widget repaints land last.
    try:
        from qfluentwidgets import Theme, setTheme, setThemeColor
        from PySide6.QtGui import QColor
        setTheme(Theme.DARK if pal.name == "dark" else Theme.LIGHT)
        setThemeColor(QColor(pal.accent))
    except Exception:
        pass

    qss_path = Path(__file__).resolve().parent / "styles.qss"
    if qss_path.exists():
        template_text = qss_path.read_text(encoding="utf-8")
        rendered = Template(template_text).safe_substitute(
            bg=pal.bg,
            surface=pal.surface,
            surface_alt=pal.surface_alt,
            surface_hover=pal.surface_hover,
            surface_pressed=pal.surface_pressed,
            nav_bg=pal.nav_bg,
            text=pal.text,
            text_muted=pal.text_muted,
            text_dim=pal.text_dim,
            accent=pal.accent,
            accent_hover=pal.accent_hover,
            success=pal.success,
            danger=pal.danger,
            warning=pal.warning,
            info=pal.info,
            border=pal.border,
            border_strong=pal.border_strong,
            grid=pal.grid,
            button_bg=pal.button_bg,
            button_hover_bg=pal.button_hover_bg,
            button_hover_border=pal.button_hover_border,
            button_pressed_bg=pal.button_pressed_bg,
        )
        app.setStyleSheet(rendered)

    bus.theme_changed.emit(pal)


def persist(name: ThemeName) -> None:
    """Save active theme to state.json so next launch restores it."""
    from src.gui.services.stack_state import load_state, save_state
    state = load_state()
    save_state(replace(state, theme=name))


def load_persisted() -> ThemeName:
    """Read active theme from state.json. Defaults to 'dark'."""
    from src.gui.services.stack_state import load_state
    state = load_state()
    name = getattr(state, "theme", None) or "dark"
    return "light" if name == "light" else "dark"
