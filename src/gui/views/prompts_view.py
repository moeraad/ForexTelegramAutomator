"""PROMPTS view: read-only inspection of every AI prompt in the system.

Pick a prompt (interpreter / triage / evaluator / discovery) and a
mode (demo / live), and the page renders the exact strings the AI
would receive: system prompt, user content, expected output. With
char + token counts. Copy buttons for handing off to playgrounds.
"""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QApplication,
    QButtonGroup,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QRadioButton,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.stack_registry import Stack
from src.prompt_inspector import PROMPT_IDS, estimate_tokens, render


_PROMPT_LABELS = {
    "interpreter": "Interpreter",
    "triage": "Triage",
    "evaluator": "Evaluator",
    "discovery": "Discovery",
    "discovery_batch": "Discovery (batch)",
}


def _mono_font() -> QFont:
    f = QFont("Consolas")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(10)
    return f


class _PromptSection(QWidget):
    """A titled, read-only monospace block with char/token count."""

    def __init__(self, heading: str) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(4)

        top = QHBoxLayout()
        self._heading = QLabel(
            f"<span style='font-size:11px; font-weight:700; "
            f"letter-spacing:1px; color:#787b86;'>{heading.upper()}</span>"
        )
        self._heading.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self._heading)
        self._counter = QLabel("")
        self._counter.setStyleSheet("color: #787b86; font-size: 11px;")
        top.addWidget(self._counter)
        top.addStretch()
        self._copy_btn = QPushButton("Copy")
        self._copy_btn.setFixedWidth(64)
        self._copy_btn.clicked.connect(self._on_copy)
        top.addWidget(self._copy_btn)
        layout.addLayout(top)

        self._view = QPlainTextEdit()
        self._view.setReadOnly(True)
        self._view.setFont(_mono_font())
        self._view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        layout.addWidget(self._view)

    def set_text(self, text: str) -> None:
        self._view.setPlainText(text)
        chars = len(text)
        tokens = estimate_tokens(text)
        self._counter.setText(
            f"{chars:,} chars · ~{tokens:,} tokens"
        )

    def _on_copy(self) -> None:
        QApplication.clipboard().setText(self._view.toPlainText())


class PromptsView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._mode = "demo"
        self._selected = PROMPT_IDS[0]
        self._build_ui()
        self._refresh()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>PROMPTS</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        from src.gui.panels._a11y import mark_heading
        mark_heading(title, "Prompts")
        title_row.addWidget(title)
        hint = QLabel(
            "<span style='color:#787b86;'>read-only inspection of every AI prompt</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(hint)
        title_row.addStretch()
        self._refresh_btn = QPushButton("Refresh")
        self._refresh_btn.clicked.connect(self._refresh)
        title_row.addWidget(self._refresh_btn)
        layout.addLayout(title_row)

        # Prompt selector (radio buttons left-to-right) — wrapped in a
        # dark "segmented bar" so the white-bordered indicators have
        # contrast to read against.
        sel_bar = QWidget()
        sel_bar.setObjectName("PromptsSelBar")
        self._sel_bar = sel_bar
        self._apply_sel_bar_style()
        from src.gui.theme import bus as theme_bus
        theme_bus.theme_changed.connect(lambda _pal: self._apply_sel_bar_style())
        sel_row = QHBoxLayout(sel_bar)
        sel_row.setContentsMargins(12, 6, 12, 6)
        sel_row.addWidget(QLabel("Prompt:"))
        # SegmentedWidget for prompt selection — read-only inspector
        # affordance reads better than radio buttons, and we get
        # accent-coloured indicator built-in (REVIEW.md §4.8). Falls back
        # to QButtonGroup-of-radios if qfluentwidgets is missing.
        self._prompt_group: QButtonGroup | None = None
        try:
            from qfluentwidgets import SegmentedWidget
            self._prompt_seg = SegmentedWidget(self)
            for pid in PROMPT_IDS:
                # SegmentedWidget connects onClick to `itemClicked` which
                # carries a `bool` (the clicked signal). A bare
                # `lambda _k=pid: …` would receive the bool as `_k`,
                # breaking the route lookup — the symptom was: first tab
                # rendered (because _refresh runs in __init__), and
                # every subsequent click left the body empty. Accept and
                # discard the bool explicitly.
                self._prompt_seg.addItem(
                    routeKey=pid,
                    text=_PROMPT_LABELS[pid],
                    onClick=lambda _clicked=False, _k=pid: self._select_prompt(_k),
                )
            self._prompt_seg.setCurrentItem(self._selected)
            sel_row.addWidget(self._prompt_seg)
        except Exception:
            self._prompt_seg = None
            self._prompt_group = QButtonGroup(self)
            for pid in PROMPT_IDS:
                rb = QRadioButton(_PROMPT_LABELS[pid])
                rb.setProperty("prompt_id", pid)
                if pid == self._selected:
                    rb.setChecked(True)
                rb.toggled.connect(self._on_prompt_changed)
                self._prompt_group.addButton(rb)
                sel_row.addWidget(rb)
        sel_row.addSpacing(16)

        # Mode selector — kept as radio buttons; SegmentedWidget for a
        # 2-item demo/live binary would feel oversized.
        sel_row.addWidget(QLabel("Mode:"))
        self._mode_group = QButtonGroup(self)
        for mode_id, label in (("demo", "Demo"), ("live", "Live")):
            rb = QRadioButton(label)
            rb.setProperty("mode_id", mode_id)
            if mode_id == self._mode:
                rb.setChecked(True)
            rb.toggled.connect(self._on_mode_changed)
            self._mode_group.addButton(rb)
            sel_row.addWidget(rb)
        sel_row.addStretch()
        layout.addWidget(sel_bar)

        self._title_lbl = QLabel("")
        self._title_lbl.setStyleSheet("font-weight: 600;")  # color from global QSS
        layout.addWidget(self._title_lbl)

        self._notes_lbl = QLabel("")
        self._notes_lbl.setStyleSheet("font-style: italic;")  # color from global QSS
        self._notes_lbl.setWordWrap(True)
        layout.addWidget(self._notes_lbl)

        # Scrollable content area.
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(scroll.Shape.NoFrame)
        host = QWidget()
        host_layout = QVBoxLayout(host)
        host_layout.setContentsMargins(0, 0, 0, 0)
        host_layout.setSpacing(10)
        self._system_section = _PromptSection("System prompt")
        self._user_section = _PromptSection("User content")
        self._expected_section = _PromptSection("Expected output")
        host_layout.addWidget(self._system_section, 5)
        host_layout.addWidget(self._user_section, 4)
        host_layout.addWidget(self._expected_section, 1)
        scroll.setWidget(host)
        layout.addWidget(scroll, 1)

    def _apply_sel_bar_style(self) -> None:
        from src.gui.theme import current_palette
        p = current_palette()
        # In dark mode the bar uses our nav_bg for contrast. In light
        # mode it uses the strong-border surface so it reads as elevated.
        bar_bg = p.nav_bg if p.name == "dark" else p.surface_alt
        # Only style the bar itself by objectName so the QSS does NOT
        # cascade into the qfluentwidgets SegmentedWidget living inside
        # (a bare `QWidget {bg:...}` rule made its tabs invisible —
        # dark-on-dark — and that's what blanked the prompt selector).
        # Labels and the fallback QRadioButtons pick up colors from the
        # global stylesheet.
        self._sel_bar.setStyleSheet(
            "QWidget#PromptsSelBar { background-color: %s; border-radius: 6px; }"
            % bar_bg
        )

    def _on_prompt_changed(self, checked: bool) -> None:
        if not checked:
            return
        btn = self._prompt_group.checkedButton()
        if btn is None:
            return
        self._selected = btn.property("prompt_id")
        self._refresh()

    def _select_prompt(self, prompt_id: str) -> None:
        """SegmentedWidget click handler — bridges to _refresh()."""
        if prompt_id == self._selected:
            return
        self._selected = prompt_id
        self._refresh()

    def _on_mode_changed(self, checked: bool) -> None:
        if not checked:
            return
        btn = self._mode_group.checkedButton()
        if btn is None:
            return
        self._mode = btn.property("mode_id")
        self._refresh()

    def _refresh(self) -> None:
        rp = render(self._selected, self._stack.db_path, mode=self._mode)
        self._title_lbl.setText(rp.title)
        self._notes_lbl.setText(rp.notes)
        self._system_section.set_text(rp.system_prompt or "(empty)")
        self._user_section.set_text(rp.user_content or "(empty)")
        self._expected_section.set_text(rp.expected_output or "(empty)")
