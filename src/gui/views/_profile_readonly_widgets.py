"""Read-only structured renderers for derived profile fields.

The Editor tab uses these for vocabulary_table / commentary_filter /
worked_examples / triage_keep_triggers. The Triggers tab is the
authoritative editor for the underlying data; these widgets only show
what will be rendered into the prompt on save, and emit an
``editRequested`` signal so the parent can jump to the Triggers tab.
"""
from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLayout,
    QListWidget,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src.gui.theme import current_palette


def _edit_note_style() -> str:
    return f"color: {current_palette().text_muted}; font-style: italic;"


class _ReadOnlyBase(QWidget):
    """Frame + 'derived from triggers' note + edit-in-triggers button."""

    editRequested = Signal()

    def __init__(self, note: str, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(4)

        top_row = QHBoxLayout()
        note_label = QLabel(note)
        note_label.setStyleSheet(_edit_note_style())
        note_label.setWordWrap(True)
        top_row.addWidget(note_label, 1)
        edit_btn = QPushButton("Edit in Triggers tab")
        # Use sizeHint width + a generous padding rather than a fixed
        # width so the global QSS padding (5px 14px) doesn't clip the
        # label. Set minimumWidth via the text metric so the button
        # always shows the full caption.
        fm = edit_btn.fontMetrics()
        edit_btn.setMinimumWidth(fm.horizontalAdvance(edit_btn.text()) + 40)
        edit_btn.clicked.connect(self.editRequested)
        top_row.addWidget(edit_btn, 0, Qt.AlignmentFlag.AlignRight)
        outer.addLayout(top_row)

        self._frame = QFrame()
        self._frame.setFrameShape(QFrame.Shape.StyledPanel)
        self._restyle_frame()
        self._frame_layout = QVBoxLayout(self._frame)
        self._frame_layout.setContentsMargins(8, 8, 8, 8)
        self._frame_layout.setSpacing(4)
        outer.addWidget(self._frame, 1)
        from src.gui.theme import bus as theme_bus
        theme_bus.theme_changed.connect(self._on_theme_changed)

    def _on_theme_changed(self, _pal) -> None:
        # ProfileView rebuilds these widgets on every _load_from_disk,
        # but the dead Python wrappers stay subscribed to theme_changed
        # until GC. Swallow the RuntimeError when the C++ side is gone.
        try:
            self._restyle_frame()
        except RuntimeError:
            pass

    def _restyle_frame(self) -> None:
        from src.gui.theme import current_palette
        p = current_palette()
        self._frame.setStyleSheet(
            f"QFrame {{ background-color: {p.surface}; border: 1px solid {p.border};"
            f" border-radius: 4px; }}"
        )


# --- vocabulary_table -----------------------------------------------------


class TreeFieldView(_ReadOnlyBase):
    """Tree: action_type -> phrases."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Derived from the Triggers tab. Each action type lists the "
            "phrases that map to it.",
            parent,
        )
        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Phrase", "Count"])
        self._tree.setColumnWidth(0, 480)
        self._tree.setColumnWidth(1, 60)
        self._tree.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._tree.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._tree.setRootIsDecorated(True)
        self._tree.setMinimumHeight(180)
        self._frame_layout.addWidget(self._tree)

    def set_triggers(self, triggers: list[dict]) -> None:
        self._tree.clear()
        grouped: dict[str, list[dict]] = defaultdict(list)
        for t in triggers:
            at = (t.get("action_type") or "").upper()
            if at in ("IGNORE", "UNKNOWN"):
                continue
            grouped[at].append(t)
        for at in sorted(grouped.keys()):
            entries = grouped[at]
            parent = QTreeWidgetItem([f"{at}", str(len(entries))])
            parent.setForeground(0, Qt.GlobalColor.darkBlue)
            self._tree.addTopLevelItem(parent)
            for t in entries:
                phrase = (t.get("phrase") or "").strip()
                if not phrase:
                    continue
                child = QTreeWidgetItem([phrase, ""])
                parent.addChild(child)
            parent.setExpanded(True)


# --- commentary_filter ----------------------------------------------------


class BulletListView(_ReadOnlyBase):
    """Flat list of IGNORE phrases."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Phrases the AI must ignore. Edit in Triggers tab -> IGNORE.",
            parent,
        )
        self._list = QListWidget()
        self._list.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._list.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self._list.setMinimumHeight(140)
        self._frame_layout.addWidget(self._list)

    def set_triggers(self, triggers: list[dict]) -> None:
        self._list.clear()
        seen = set()
        for t in triggers:
            if (t.get("action_type") or "").upper() != "IGNORE":
                continue
            phrase = (t.get("phrase") or "").strip()
            if not phrase or phrase in seen:
                continue
            seen.add(phrase)
            self._list.addItem(f"•  {phrase}")
        if self._list.count() == 0:
            self._list.addItem("(no commentary filter phrases yet)")


# --- triage_keep_triggers ------------------------------------------------


class _FlowLayout(QLayout):
    """Wrapping flow layout — chips reflow on resize."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._items: list = []
        self.setContentsMargins(0, 0, 0, 0)
        self.setSpacing(6)

    def addItem(self, item) -> None:  # type: ignore[override]
        self._items.append(item)

    def count(self) -> int:  # type: ignore[override]
        return len(self._items)

    def itemAt(self, index: int):  # type: ignore[override]
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # type: ignore[override]
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # type: ignore[override]
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # type: ignore[override]
        return True

    def heightForWidth(self, width: int) -> int:  # type: ignore[override]
        return self._do_layout((0, 0, width, 0), test_only=True)

    def setGeometry(self, rect) -> None:  # type: ignore[override]
        super().setGeometry(rect)
        self._do_layout((rect.x(), rect.y(), rect.width(), rect.height()), test_only=False)

    def sizeHint(self):  # type: ignore[override]
        return self.minimumSize()

    def minimumSize(self):  # type: ignore[override]
        from PySide6.QtCore import QSize
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        return size

    def _do_layout(self, rect_tuple, test_only: bool) -> int:
        x0, y0, width, _h = rect_tuple
        x, y = x0, y0
        line_height = 0
        for item in self._items:
            wid = item.widget()
            spacing = self.spacing()
            hint = item.sizeHint()
            next_x = x + hint.width() + spacing
            if next_x - spacing > x0 + width and line_height > 0:
                x = x0
                y += line_height + spacing
                next_x = x + hint.width() + spacing
                line_height = 0
            if not test_only:
                from PySide6.QtCore import QRect
                wid.setGeometry(QRect(x, y, hint.width(), hint.height()))
            x = next_x
            line_height = max(line_height, hint.height())
        return y + line_height - y0


class ChipFlowView(_ReadOnlyBase):
    """Wrapping chip layout for the triage_keep_triggers flat list."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "High-signal phrases the cheap triage model must always keep "
            "(escalate to the interpreter). Derived from Triggers tab "
            "(non-IGNORE entries).",
            parent,
        )
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setMinimumHeight(120)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._chip_host = QWidget()
        self._flow = _FlowLayout(self._chip_host)
        self._scroll.setWidget(self._chip_host)
        self._frame_layout.addWidget(self._scroll)
        # Cache the last triggers list so we can re-skin on theme swap
        # without losing the data.
        self._last_triggers: list[dict] = []
        from src.gui.theme import bus as _theme_bus
        _theme_bus.theme_changed.connect(lambda _pal: self.set_triggers(self._last_triggers))

    def set_triggers(self, triggers: list[dict]) -> None:
        self._last_triggers = list(triggers)
        while self._flow.count():
            item = self._flow.takeAt(0)
            if item and item.widget():
                item.widget().deleteLater()
        phrases = sorted({
            (t.get("phrase") or "").strip()
            for t in triggers
            if (t.get("action_type") or "").upper() not in ("IGNORE", "UNKNOWN")
        } - {""})
        if not phrases:
            placeholder = QLabel("(no trigger phrases yet)")
            placeholder.setStyleSheet(f"color: {current_palette().text_muted};")
            self._flow.addWidget(placeholder)
            return
        # Pull colours from the active palette so chips swap with the
        # theme (dark/light). Previously hardcoded to dark-mode hexes
        # which left them unreadable on the light theme.
        from src.gui.theme import current_palette
        p = current_palette()
        chip_css = (
            "QLabel { background-color: %s; color: %s; "
            "border: 1px solid %s; border-radius: 10px; "
            "padding: 2px 10px; }"
        ) % (p.surface_hover, p.text, p.border)
        for phrase in phrases:
            chip = QLabel(phrase)
            chip.setStyleSheet(chip_css)
            chip.setSizePolicy(QSizePolicy.Policy.Maximum, QSizePolicy.Policy.Fixed)
            self._flow.addWidget(chip)


# --- worked_examples ------------------------------------------------------


class ExamplesTableView(_ReadOnlyBase):
    """Table: # / action_type / sample message / output JSON."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(
            "Few-shot examples baked into the AI prompt. One example per "
            "action_type is auto-picked (highest confidence first).",
            parent,
        )
        self._table = QTableWidget(0, 4)
        self._table.setHorizontalHeaderLabels(["#", "Action type", "Message", "Output"])
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setAlternatingRowColors(True)
        self._table.setMinimumHeight(200)
        self._table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        # Cols: # | Action type | Message | Output  →  Message stretches.
        apply_full_width_headers(
            self._table, content_columns=(0, 1, 3), stretch_column=2,
        )
        self._table.setColumnWidth(3, 200)
        self._frame_layout.addWidget(self._table)

    def set_triggers(self, triggers: list[dict]) -> None:
        self._table.setRowCount(0)
        grouped: dict[str, list[dict]] = defaultdict(list)
        for t in triggers:
            at = (t.get("action_type") or "").upper()
            if at in ("IGNORE", "UNKNOWN"):
                continue
            grouped[at].append(t)
        rows: list[tuple[str, str, str]] = []
        for at in sorted(grouped.keys()):
            entries_with_samples = [
                t for t in grouped[at] if t.get("samples")
            ]
            if not entries_with_samples:
                continue
            entry = entries_with_samples[0]
            sample = (entry.get("samples") or [""])[0]
            output = f'[{{"type":"{at}"}}]'
            rows.append((at, sample, output))
        self._table.setRowCount(len(rows))
        for i, (at, sample, output) in enumerate(rows):
            idx_item = QTableWidgetItem(str(i + 1))
            at_item = QTableWidgetItem(at)
            sample_item = QTableWidgetItem(sample.replace("\n", " "))
            sample_item.setToolTip(sample)
            out_item = QTableWidgetItem(output)
            for item in (idx_item, at_item, sample_item, out_item):
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
            self._table.setItem(i, 0, idx_item)
            self._table.setItem(i, 1, at_item)
            self._table.setItem(i, 2, sample_item)
            self._table.setItem(i, 3, out_item)
