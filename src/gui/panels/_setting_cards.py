"""Shared Fluent SettingCard helpers used by Settings → Tuning and
Profile → Editor (REVIEW.md §3/§4 follow-up).

Both tabs need the same shell: an icon + title + subtitle + an editor
widget mounted on the right (for one-line controls) or in an expanded
panel below (for multi-line / heavy controls). Without this helper the
two tabs drifted out of sync.

The helper deliberately does NOT own the editor widget's lifecycle —
callers keep their `self._editors[key] = widget` registry and the
dirty-tracking signal wiring. We just wrap the widget in a card.
"""
from __future__ import annotations

from typing import Literal

from PySide6.QtWidgets import QWidget


CardKind = Literal["compact", "expand"]


def make_setting_card(
    *,
    icon,
    title: str,
    subtitle: str,
    widget: QWidget,
    kind: CardKind = "compact",
    expand_min_height: int = 140,
    expand_max_height: int = 220,
):
    """Wrap ``widget`` in a qfluentwidgets SettingCard.

    Args:
      icon: a ``FluentIcon`` member (or any value SettingCard accepts).
      title: short row title shown to the operator.
      subtitle: one-line content hint (typically the first sentence of
        the full tooltip).
      widget: the editor / read-only widget to embed. The caller has
        already populated it and wired any change signals.
      kind:
        ``"compact"`` mounts ``widget`` on the right of a single-row
        ``SettingCard``.
        ``"expand"`` mounts ``widget`` inside an ``ExpandSettingCard``'s
        expanded panel, so the row stays compact until clicked. Use
        this for multi-line ``QPlainTextEdit``, tables, and any widget
        that needs >40px of vertical space.
      expand_min_height / expand_max_height: only honoured for ``kind=expand``.

    Returns:
      The SettingCard / ExpandSettingCard instance, ready to be added
      to a ``SettingCardGroup``.

    Falls back to a bare ``QWidget`` row stack if qfluentwidgets isn't
    importable (so the editor still renders on a stripped-down install).
    """
    try:
        from qfluentwidgets import ExpandSettingCard, SettingCard
    except Exception:
        return _fallback_row(title, subtitle, widget)

    if kind == "expand":
        card = ExpandSettingCard(icon=icon, title=title, content=subtitle)
        # FIXED height (not min/max range) so the `view` frame and the
        # `spaceWidget` ExpandSettingCard reserves for the panel collapse
        # below it agree on size. Setting only min/max made the layout
        # sizeHint (uses widget sizeHint) >> the actual rendered height
        # (uses widget minimumHeight), producing a 50+ px gray gap below
        # the editor field. Users scroll inside the QPlainTextEdit if
        # they need to see more — much cleaner than gray dead space.
        widget.setFixedHeight(expand_min_height)
        try:
            # Tighter bottom margin too — no need for 16px of padding
            # past the field when there's nothing there.
            card.viewLayout.setContentsMargins(48, 8, 16, 8)
            card.viewLayout.addWidget(widget)
        except AttributeError:
            card.hBoxLayout.addWidget(widget)
        # qfluentwidgets bug workaround: ExpandSettingCard's collapse
        # animation drives `setFixedHeight(h_top + vh - scrollBar.value)`
        # on every tick, but the animation doesn't always land on the
        # scrollBar's maximum, so the card stays a few-dozen-px taller
        # than the header (and the operator sees a "ghost margin"
        # persisting after the body disappears). We pin the height to
        # the header's height on every animation tick AND on finish so
        # the collapse looks instant rather than flashy.
        try:
            ani = card.expandAni
            header_h = card.card.height()

            def _snap_to_header():
                if not card.isExpand:
                    card.setFixedHeight(header_h)

            ani.valueChanged.connect(lambda _v: _snap_to_header())
            ani.finished.connect(_snap_to_header)
        except Exception:
            pass
        return card

    card = SettingCard(icon=icon, title=title, content=subtitle)
    card.hBoxLayout.addWidget(widget)
    card.hBoxLayout.addSpacing(16)
    return card


def _fallback_row(title: str, subtitle: str, widget: QWidget) -> QWidget:
    from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout
    container = QWidget()
    outer = QVBoxLayout(container)
    outer.setContentsMargins(8, 6, 8, 6)
    outer.setSpacing(2)
    head = QHBoxLayout()
    title_lbl = QLabel(title)
    title_lbl.setStyleSheet("font-weight: 600;")
    head.addWidget(title_lbl)
    head.addStretch()
    outer.addLayout(head)
    if subtitle:
        sub = QLabel(subtitle)
        sub.setProperty("role", "muted")
        sub.setWordWrap(True)
        outer.addWidget(sub)
    outer.addWidget(widget)
    return container


def truncate_subtitle(tooltip: str, max_len: int = 140) -> str:
    """Pull the first sentence of a longer tooltip for the card subtitle."""
    if not tooltip:
        return ""
    head = tooltip.split(". ", 1)[0]
    if not head.endswith("."):
        head = head + "."
    if len(head) > max_len:
        head = head[: max_len - 1] + "…"
    return head
