"""Shared helpers for tabular widgets.

``apply_full_width_headers(view)`` makes every column resizable by drag
and forces the **last column** to stretch so the total width always
covers the viewport (no whitespace on the right). Optional
``content_columns`` get an initial size-to-contents pass before the
table becomes interactive — useful for narrow ID / status / number
columns so they don't waste horizontal space on first render.
"""
from __future__ import annotations

from PySide6.QtWidgets import QHeaderView, QTableView, QTableWidget


def apply_full_width_headers(
    view: QTableView | QTableWidget,
    content_columns: tuple[int, ...] = (),
    stretch_column: int | None = None,
) -> None:
    """Headers fill 100% of the viewport and every column is drag-resizable.

    ``content_columns`` — initially sized to fit content, then switched to
        Interactive. Use for narrow ID/status/number columns.
    ``stretch_column`` — explicitly nominates which column eats slack on
        resize. Defaults to the last column. The chosen column becomes
        ``Stretch`` (still resizable when the user drags neighbours).
    """
    header = view.horizontalHeader()
    column_count = view.model().columnCount() if view.model() is not None else view.columnCount()

    # Pass 1: content sizing for narrow columns so they don't waste space.
    for col in content_columns:
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    # Lock those widths in by flipping to Interactive (preserves the size).
    for col in content_columns:
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)

    # Everything else: Interactive (drag-resizable).
    for col in range(column_count):
        if col in content_columns:
            continue
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.Interactive)

    # Make one column stretch so total width = viewport width.
    if stretch_column is None:
        header.setStretchLastSection(True)
    else:
        header.setStretchLastSection(False)
        header.setSectionResizeMode(stretch_column, QHeaderView.ResizeMode.Stretch)
