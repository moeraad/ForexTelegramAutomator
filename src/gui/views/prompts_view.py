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
            f"letter-spacing:1px; color:#586e75;'>{heading.upper()}</span>"
        )
        self._heading.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self._heading)
        self._counter = QLabel("")
        self._counter.setStyleSheet("color: #93a1a1; font-size: 11px;")
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
        title_row.addWidget(title)
        hint = QLabel(
            "<span style='color:#93a1a1;'>read-only inspection of every AI prompt</span>"
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
        sel_bar.setStyleSheet(
            "QWidget { background-color: #073642; border-radius: 6px; }"
            "QLabel { color: #93a1a1; font-weight: 600; padding: 0 6px; }"
            "QRadioButton { color: white; padding: 6px 10px; }"
            "QRadioButton::indicator { width: 14px; height: 14px;"
            " border-radius: 8px; background: transparent;"
            " border: 2px solid white; }"
            "QRadioButton::indicator:hover { border: 2px solid #268bd2; }"
            "QRadioButton::indicator:checked { background: #268bd2;"
            " border: 2px solid white; }"
        )
        sel_row = QHBoxLayout(sel_bar)
        sel_row.setContentsMargins(12, 6, 12, 6)
        sel_row.addWidget(QLabel("Prompt:"))
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

        # Mode selector
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
        self._title_lbl.setStyleSheet("color: #073642; font-weight: 600;")
        layout.addWidget(self._title_lbl)

        self._notes_lbl = QLabel("")
        self._notes_lbl.setStyleSheet("color: #586e75; font-style: italic;")
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

    def _on_prompt_changed(self, checked: bool) -> None:
        if not checked:
            return
        btn = self._prompt_group.checkedButton()
        if btn is None:
            return
        self._selected = btn.property("prompt_id")
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
