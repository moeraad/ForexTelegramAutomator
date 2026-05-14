"""Left icon nav rail. Uses Segoe Fluent Icons / MDL2 Assets glyphs."""
from __future__ import annotations

from PySide6.QtCore import Qt, QSize, Signal
from PySide6.QtGui import QColor, QFont, QFontInfo, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import QButtonGroup, QToolButton, QVBoxLayout, QWidget


# Segoe Fluent Icons / Segoe MDL2 Assets — Windows-bundled icon font.
# Codepoints (Private Use Area):
#   E9F9 BarChart4   E8F4 Subscriptions   E711 Cancel        E8B7 PaymentCard
#   E730 Shield      E72C Refresh         E70F Edit          E713 Settings
_ITEMS: list[tuple[str, str, str]] = [
    ("live",     "", "Live"),
    ("journal",  "", "Journal"),
    ("rejected", "", "Rejected"),
    ("cost",     "", "Cost"),
    ("risk",     "", "Risk"),
    ("replay",   "", "Replay"),
    ("profile",  "", "Profile"),
    ("settings", "", "Settings"),
]

_RAIL_STYLE = (
    "QWidget#NavRail { background-color: #073642; }"
    "QToolButton { background: transparent; color: #93a1a1; border: none; "
    "padding: 4px 0; font-size: 10px; }"
    "QToolButton:hover { background-color: #094352; color: white; }"
    "QToolButton:checked { background-color: #094352; color: white; "
    "border-left: 3px solid #2aa198; }"
)


def _glyph_font() -> QFont:
    candidates = ("Segoe Fluent Icons", "Segoe MDL2 Assets", "Segoe UI Symbol")
    f = QFont()
    for name in candidates:
        f.setFamily(name)
        if QFontInfo(f).family().lower().replace(" ", "") == name.lower().replace(" ", ""):
            break
    f.setPixelSize(16)
    return f


def _draw_pixmap(glyph: str, color: QColor, size: int = 22) -> QPixmap:
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setFont(_glyph_font())
    p.setPen(color)
    p.drawText(pix.rect(), Qt.AlignmentFlag.AlignCenter, glyph)
    p.end()
    return pix


def _icon_for(glyph: str) -> QIcon:
    icon = QIcon()
    icon.addPixmap(_draw_pixmap(glyph, QColor("#93a1a1")), QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(_draw_pixmap(glyph, QColor("white")),   QIcon.Mode.Normal, QIcon.State.On)
    return icon


class NavRail(QWidget):
    item_selected = Signal(str)

    def __init__(self, active: str = "live") -> None:
        super().__init__()
        self.setObjectName("NavRail")
        self.setFixedWidth(64)
        self.setStyleSheet(_RAIL_STYLE)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 8)
        layout.setSpacing(0)

        self._group = QButtonGroup(self)
        self._group.setExclusive(True)
        self._buttons: dict[str, QToolButton] = {}

        for key, glyph, label in _ITEMS:
            btn = QToolButton()
            btn.setIcon(_icon_for(glyph))
            btn.setIconSize(QSize(22, 22))
            btn.setText(label)
            btn.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextUnderIcon)
            btn.setCheckable(True)
            btn.setAutoRaise(False)
            btn.setMinimumHeight(52)
            btn.setFixedWidth(64)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.setToolTip(label)
            btn.clicked.connect(lambda _checked=False, k=key: self.item_selected.emit(k))
            self._buttons[key] = btn
            self._group.addButton(btn)
            layout.addWidget(btn)

        layout.addStretch()
        self.set_active(active)

    def set_active(self, key: str) -> None:
        btn = self._buttons.get(key)
        if btn is not None:
            btn.setChecked(True)
