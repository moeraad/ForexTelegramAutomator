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
from src.gui.theme import current_palette
from src.prompt_inspector import PROMPT_IDS, estimate_tokens, render


_PROMPT_LABELS = {
    "interpreter": "Interpreter",
    "triage": "Triage",
    "evaluator": "Evaluator",
    "discovery": "Discovery",
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
        _tm = current_palette().text_muted
        self._heading = QLabel(
            f"<span style='font-size:11px; font-weight:700; "
            f"letter-spacing:1px; color:{_tm};'>{heading.upper()}</span>"
        )
        self._heading.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self._heading)
        self._counter = QLabel("")
        self._counter.setStyleSheet(f"color: {_tm}; font-size: 11px;")
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
    """Prompts inspector — post-v2 Prompts/Playground gap fix: gains a
    profile picker so the interpreter + triage prompts can be rendered
    against ANY profile, not just the current stack's default.

    Same picker pattern as ``ProfileView`` (Phase 2) — when the
    operator picks a different profile, the prompts re-render via
    ``prompt_inspector.render(..., profile_name=...)``.
    """

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._mode = "demo"
        self._selected = PROMPT_IDS[0]
        # Profile follows the active stack (set by the header channel
        # switcher). The previous in-view profile picker was removed —
        # operators switch profiles via the header instead.
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
        _tm2 = current_palette().text_muted
        hint = QLabel(
            f"<span style='color:{_tm2};'>read-only inspection of every AI prompt</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(hint)
        title_row.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload prompts")
        self._refresh_btn.clicked.connect(self._refresh)
        title_row.addWidget(self._refresh_btn)
        layout.addLayout(title_row)

        # Profile picker removed — prompts render against the active
        # stack's profile (driven by the header channel switcher).

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
        # Inline labels inside the dark sel_bar inherit the global
        # theme's opaque background, which paints a black rectangle
        # behind their text even though the parent bar has its own
        # colored fill. Force transparency on each label.
        _prompt_lbl = QLabel("Prompt:")
        _prompt_lbl.setStyleSheet("background: transparent;")
        sel_row.addWidget(_prompt_lbl)
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
        _mode_lbl = QLabel("Mode:")
        _mode_lbl.setStyleSheet("background: transparent;")
        sel_row.addWidget(_mode_lbl)
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
        # Match the ribbon / top-panel visual vocabulary: surface bg
        # (raised from page), border_strong 1 px rim, 6 px radius. The
        # previous nav_bg fill was nearly black in dark mode and looked
        # out of place against the rest of the design language.
        # Direct-child labels + radio buttons stay transparent so they
        # inherit the bar's surface rather than painting over it.
        self._sel_bar.setStyleSheet(
            "QWidget#PromptsSelBar { "
            f" background-color: {p.surface};"
            f" border: 1px solid {p.border_strong};"
            "  border-radius: 6px;"
            "}"
            "QWidget#PromptsSelBar > QLabel, "
            "QWidget#PromptsSelBar > QRadioButton "
            "{ background: transparent; }"
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
        rp = render(
            self._selected, self._stack.db_path, mode=self._mode,
            profile_name=self._stack.name,
        )
        self._title_lbl.setText(rp.title)
        self._notes_lbl.setText(rp.notes)
        self._system_section.set_text(rp.system_prompt or "(empty)")
        self._user_section.set_text(rp.user_content or "(empty)")
        self._expected_section.set_text(rp.expected_output or "(empty)")
