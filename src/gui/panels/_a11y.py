"""Small accessibility helpers (REVIEW.md §3).

Qt has no first-class heading role, but ``QAccessibleWidget`` exposes
``accessibleName`` and ``accessibleDescription`` which screen readers
(NVDA, JAWS, VoiceOver via Qt accessibility bridge) announce. By
setting both consistently on view title ``QLabel``s, an AT can
distinguish a heading from body text even though the visual style
lives in inline rich HTML.
"""
from __future__ import annotations

from PySide6.QtWidgets import QLabel


def mark_heading(label: QLabel, plain_text: str | None = None) -> None:
    """Tag a label as a heading for screen readers.

    Pass ``plain_text`` when the visible label is rich HTML (so the
    accessible name stays free of markup). When omitted, the label's
    current ``text()`` is reused — fine for plain-text labels.
    """
    name = plain_text if plain_text is not None else label.text()
    try:
        label.setAccessibleName(name)
        label.setAccessibleDescription("heading")
    except Exception:
        pass
