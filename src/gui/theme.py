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
    # Tokyo Night — deep navy-purple base with soft azure / mint / rose
    # accents. Saturated enough to feel premium without going full neon.
    # Tested for long-session readability on trader / dev workflows.
    bg="#1a1b26",                    # deep navy-purple
    surface="#24283b",               # raised navy band
    surface_alt="#1f2233",
    surface_hover="#2f3550",
    surface_pressed="#3a4060",
    nav_bg="#0f1115",                # deepest indigo, rail/nav contrast
    text="#c0caf5",                  # soft periwinkle-white body text
    text_muted="#565f89",            # muted indigo for secondary labels
    text_dim="#3b4366",
    accent="#7aa2f7",                # azure blue — brand + CTAs
    accent_hover="#9cb7fb",          # lighter azure on hover
    success="#9ece6a",               # fresh mint-green
    danger="#f7768e",                # soft rose-red
    warning="#e0af68",               # warm honey
    info="#7dcfff",                  # bright cyan — informational
    border="#2c3147",                # navy rim
    border_strong="#4a527a",
    grid="#24283b",
    button_bg="#24283b",
    button_hover_bg="rgba(122, 162, 247, 0.18)",   # translucent azure glow
    button_hover_border="#7aa2f7",
    button_pressed_bg="rgba(122, 162, 247, 0.30)",
)


LIGHT = Palette(
    name="light",
    # Tokyo Day — light counterpart to the Tokyo Night dark palette.
    # Pale slate-cream base with deeper azure / forest / rose accents
    # (darker than the night variants so contrast stays AA on white).
    # Same hue family as DARK so toggling between modes feels coherent.
    bg="#e1e2e7",                    # pale slate-cream
    surface="#f0f1f4",               # raised paper
    surface_alt="#e8e9ee",
    surface_hover="#d8d9e0",
    surface_pressed="#c4c6d0",
    nav_bg="#1a1b26",                # navy rail (matches Tokyo Night)
    text="#3760bf",                  # deep azure body text
    text_muted="#6172b0",            # muted indigo
    text_dim="#8c96cc",              # faded indigo
    accent="#2e7de9",                # azure blue — brand + CTAs
    accent_hover="#5b7fd8",          # softer hover
    success="#587539",               # forest mint (darker for readability)
    danger="#c64343",                # rose red (deeper than dark variant)
    warning="#8c6c3e",               # honey brown
    info="#007197",                  # deep cyan
    border="#cacbd1",                # cool slate rim
    border_strong="#9999a5",
    grid="#e8e9ee",
    button_bg="#f0f1f4",
    button_hover_bg="rgba(46, 125, 233, 0.12)",   # translucent azure wash
    button_hover_border="#2e7de9",
    button_pressed_bg="rgba(46, 125, 233, 0.22)",
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


def _render_chevron_png(pal: Palette) -> str:
    """Bake the chevron SVG with the active palette's muted-text color
    into a PNG, return its absolute path with forward slashes (QSS
    requires that on Windows). Returns "" if anything goes wrong; the
    QSS then falls back to its default arrow."""
    try:
        import os
        from PySide6.QtCore import Qt
        from PySide6.QtGui import QColor, QPainter, QPixmap
        from PySide6.QtSvg import QSvgRenderer
        src = Path(__file__).resolve().parent / "resources" / "icons" / "chevron-down.svg"
        if not src.exists():
            return ""
        cache_dir = Path(os.environ.get("APPDATA", str(Path.home()))) / "CopyTrades" / "cache"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"chevron-{pal.name}.png"
        size = 12
        pix = QPixmap(size, size)
        pix.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pix)
        QSvgRenderer(str(src)).render(painter)
        painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
        painter.fillRect(pix.rect(), QColor(pal.text_muted))
        painter.end()
        pix.save(str(out), "PNG")
        return str(out).replace("\\", "/")
    except Exception:
        return ""


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

    chevron_url = _render_chevron_png(pal)

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
            chevron_url=chevron_url,
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
