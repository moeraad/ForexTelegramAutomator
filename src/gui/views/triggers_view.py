"""Triggers tab: manage the structured phrase -> action_type mapping.

Loads triggers from channels/<stack>.json, lets the user add/edit/delete/move
entries, and writes back (which also re-renders the derived prompt fields).
"""
from __future__ import annotations

import copy
from collections import defaultdict

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QFrame,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSplitter,
    QStyledItemDelegate,
    QTableWidget,
    QTableWidgetItem,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from src.gui.services import profile_io
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


ACTION_TYPES = (
    "OPEN",
    "OPEN_INSTANT",
    "ATTACH_SIGNAL",
    "MOVE_SL_BE",
    "MOVE_SL",
    "TIGHTEN_SL",
    "CLOSE_PARTIAL",
    "CLOSE_FULL",
    "MODIFY_TPS",
    "REOPEN_LAST",
    "REINFORCE",
    "ALERT",
    "IGNORE",
    "UNKNOWN",
    # Reply-intent dispatch types — fire on the CURRENT (reply) message
    # to control what happens with the parent's trigger.
    #   TRIGGER_PARENT → re-run the matcher against the parent's text
    #                    and emit whatever the parent's trigger would.
    #   IGNORE_PARENT  → suppress both the parent's trigger AND the
    #                    AI fallback for this reply.
    "TRIGGER_PARENT",
    "IGNORE_PARENT",
)


class _MultilineCellDelegate(QStyledItemDelegate):
    """Cell editor that uses QPlainTextEdit so operators can enter
    multi-line trigger phrases. The default QTableWidget editor is
    a QLineEdit, which silently strips newlines on paste — a pasted
    multi-line signal block becomes only its first line, the stored
    phrase ends up shorter than the operator expected, and the
    matcher under-matches.

    Newlines saved into a phrase are preserved at rest (in profile
    JSON) and collapsed to spaces by the normaliser at match time.
    Storing the verbatim multi-line form keeps the operator's intent
    visible in the JSON and the editor.
    """

    def createEditor(self, parent, option, index):  # type: ignore[override]
        editor = QPlainTextEdit(parent)
        editor.setMinimumHeight(80)
        return editor

    def setEditorData(self, editor, index):  # type: ignore[override]
        editor.setPlainText(str(index.data() or ""))

    def setModelData(self, editor, model, index):  # type: ignore[override]
        model.setData(index, editor.toPlainText())


def _html_escape(s: str) -> str:
    """Minimal HTML-escape for the Test pane's result rendering. The
    operator-pasted text may contain `<`, `>`, `&` from signal blocks
    — without escaping, QTextEdit would parse them as HTML and
    swallow chunks of the message."""
    return (
        s.replace("&", "&amp;")
         .replace("<", "&lt;")
         .replace(">", "&gt;")
    )


class _BulkClassifyWorker(QThread):
    progress = Signal(int, int)
    done = Signal(list)
    failed = Signal(str)

    def __init__(self, messages: list[str], stack, parent=None) -> None:
        super().__init__(parent)
        self._messages = messages
        self._stack = stack

    def run(self) -> None:
        try:
            from concurrent.futures import ThreadPoolExecutor, as_completed
            from src import ai_discovery, db_settings
            # The discovery template is single-message now; fan out per
            # message through the same thread pool that classify_batch
            # uses internally. Concurrency=4 mirrors the previous default.
            concurrency = 4
            provider = ai_discovery.build_discovery_provider()
            # Discovery context: pull symbol from the stack's profile/DB,
            # leave language_note blank if not set — the prompt's
            # universal rules still apply. Price magnitude is no longer
            # passed here; the discovery prompt derives it from message
            # content alone now.
            symbol = db_settings.get_str(
                self._stack.db_path, "target_symbol", ""
            ) or "XAUUSD"
            context = {"symbol": symbol, "language_note": ""}

            out: list[dict] = []
            done_count = 0
            total = len(self._messages)

            def run_one(msg: str) -> tuple[str, "Classification"]:
                try:
                    return msg, ai_discovery.classify(msg, provider, context)
                except Exception as e:  # noqa: BLE001
                    from src.ai_discovery import Classification
                    return msg, Classification(
                        action_types=("UNKNOWN",),
                        phrase=msg[:60],
                        reasoning=f"classify error: {e}",
                        confidence=0.0,
                    )

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(run_one, m) for m in self._messages]
                for fut in as_completed(futures):
                    msg, c = fut.result()
                    out.append({
                        "action_type": c.action_type,
                        "phrase": c.phrase or msg[:60],
                        "samples": [msg],
                        "note": c.reasoning if c.action_type == "UNKNOWN" else "",
                    })
                    done_count += 1
                    self.progress.emit(done_count, total)
            self.done.emit(out)
        except Exception as e:  # noqa: BLE001
            self.failed.emit(f"{type(e).__name__}: {e}")


class TriggersView(QWidget):
    """Per-profile triggers editor.

    Post-v2 fix: ``get_profile_name`` callback ties this view to the
    ProfileView's picker so the operator's edits land in the profile
    they're actually editing (not the stack's default — which for
    aggregate routing might be a DIFFERENT profile entirely).

    Backward compat: when ``get_profile_name`` is None, falls back to
    ``stack.name`` — the v1 / single-channel behavior.
    """

    def __init__(self, stack: Stack, get_profile_name=None) -> None:
        super().__init__()
        self._stack = stack
        self._get_profile_name = get_profile_name
        self._triggers: list[dict] = []
        self._dirty = False
        self._bulk_worker: _BulkClassifyWorker | None = None
        self._build_ui()
        self.rebind(stack)

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._load_from_disk()

    def _active_profile_name(self) -> str:
        """Profile to operate on: picker > stack default (v1 back-compat)."""
        if self._get_profile_name is not None:
            picked = self._get_profile_name()
            if picked:
                return picked
        return self._stack.name

    def reload(self) -> None:
        """Public re-read trigger — called by ProfileView when the operator
        switches the profile picker so this sub-tab follows along."""
        self._load_from_disk()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        # Dirty marker — kept as a small inline label so the operator
        # sees pending-edits status at a glance. Lives just above the
        # ribbon so it's always visible when there are unsaved edits.
        self._dirty_label = QLabel("")
        self._dirty_label.setStyleSheet("color: #ff9800; padding-left: 4px;")
        layout.addWidget(self._dirty_label)

        # Triggers-tab ribbon. Save / Revert are intentionally NOT here
        # — they live in the profile-wide ribbon at the top of the
        # parent ProfileView and apply to every profile tab. This
        # ribbon focuses on trigger-row operations:
        #   Triggers  — Add / Delete / Move / Copy
        #   Bulk      — Bulk import
        # Keep button references so dirty-state / selection wiring
        # elsewhere in the class still works.
        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        # Stub-out the previously-public button attrs to None so legacy
        # accessors (e.g. self._save_btn.setEnabled) elsewhere noop
        # gracefully instead of AttributeError-ing.
        self._save_btn = None
        self._revert_btn = None
        self._bulk_btn = None
        self._add_btn = None
        self._delete_btn = None
        self._move_btn = None
        self._copy_btn = None
        ribbon = RibbonBar([
            RibbonGroup("Triggers", [
                RibbonAction("ADD", "Add",
                             "Add a new trigger",
                             variant="success", callback=self._on_add),
                RibbonAction("DELETE", "Delete",
                             "Delete the selected trigger",
                             variant="danger", callback=self._on_delete),
                RibbonAction("RIGHT_ARROW", "Move",
                             "Move the selected trigger to another category",
                             callback=self._on_move),
                RibbonAction("COPY", "Copy",
                             "Copy the selected trigger to another category",
                             callback=self._on_copy),
            ]),
            RibbonGroup("Bulk", [
                RibbonAction("DOWNLOAD", "Import",
                             "Bulk import triggers from a JSON / CSV file",
                             callback=self._on_bulk_import),
            ]),
        ])
        layout.addWidget(ribbon)

        # Filter field is built here so it exists before the right
        # pane's heading row references it. Lives on the right pane,
        # aligned with the "Triggers" label (see right_layout below).
        from PySide6.QtWidgets import QLineEdit
        self._filter_edit = QLineEdit()
        self._filter_edit.setPlaceholderText(
            "Filter: phrase / sample / note (substring, case-insensitive)"
        )
        self._filter_edit.textChanged.connect(self._apply_filter)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        # Heading rows on both panes are taller QWidget containers
        # with the same fixed height. The right pane's heading carries
        # the filter field (which is taller than a bare QLabel), so
        # we pin both heading widgets to the QLineEdit's height to
        # keep the two lists aligned at the same Y baseline.
        # Cap the filter width so the section label stays readable on
        # narrow windows.
        self._filter_edit.setMaximumWidth(360)
        header_h = self._filter_edit.sizeHint().height()

        # Left pane — heading + action-types list.
        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        left_header_widget = QWidget()
        left_header_widget.setFixedHeight(header_h)
        left_header = QHBoxLayout(left_header_widget)
        left_header.setContentsMargins(0, 0, 0, 0)
        left_header.setSpacing(6)
        left_header.addWidget(QLabel("<b>Action types</b>"))
        left_header.addStretch()
        left_layout.addWidget(left_header_widget)
        self._types_list = QListWidget()
        self._types_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._types_list.currentItemChanged.connect(self._on_type_selected)
        left_layout.addWidget(self._types_list, 1)
        splitter.addWidget(left)

        # Right pane — heading (section label + filter) + triggers table.
        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)
        right_header_widget = QWidget()
        right_header_widget.setFixedHeight(header_h)
        right_header = QHBoxLayout(right_header_widget)
        right_header.setContentsMargins(0, 0, 0, 0)
        right_header.setSpacing(6)
        self._right_label = QLabel("<b>Triggers</b>")
        right_header.addWidget(self._right_label)
        right_header.addStretch()
        right_header.addWidget(self._filter_edit)
        right_layout.addWidget(right_header_widget)

        self._table = QTableWidget(0, 3)
        self._table.setHorizontalHeaderLabels(["Phrase", "Sample message", "Note"])
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.DoubleClicked)
        self._table.itemChanged.connect(self._on_cell_edited)
        # Multi-line cell editor for Phrase + Sample columns. Without
        # this the default single-line editor strips newlines on paste
        # and the stored phrase ends up truncated to its first line.
        self._multiline_delegate = _MultilineCellDelegate(self._table)
        self._table.setItemDelegateForColumn(0, self._multiline_delegate)
        self._table.setItemDelegateForColumn(1, self._multiline_delegate)
        # Word-wrap + auto-row-height so multi-line phrases / samples
        # actually display all their lines instead of clipping to a
        # one-line row.
        self._table.setWordWrap(True)
        self._table.verticalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.ResizeToContents
        )
        from src.gui.panels._table_utils import apply_full_width_headers
        # Cols: Phrase | Sample message | Note  →  Sample stretches.
        apply_full_width_headers(
            self._table, content_columns=(0, 2), stretch_column=1,
        )
        self._table.setColumnWidth(0, 240)
        self._table.setColumnWidth(2, 160)
        right_layout.addWidget(self._table, 1)

        splitter.addWidget(right)
        splitter.setStretchFactor(0, 0)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([220, 780])

        # Vertical splitter: editor on top, Test pane on the bottom.
        # ``setChildrenCollapsible(True)`` lets the operator collapse
        # the test pane to its title bar (via the QGroupBox checkbox)
        # to maximize space for the triggers table. Default starts
        # with the test pane closed so the editor gets the full view.
        self._v_splitter = QSplitter(Qt.Orientation.Vertical)
        self._v_splitter.setChildrenCollapsible(True)
        self._v_splitter.addWidget(splitter)
        self._v_splitter.addWidget(self._build_test_pane())
        self._v_splitter.setStretchFactor(0, 3)
        self._v_splitter.setStretchFactor(1, 0)
        # Initial sizes: editor takes everything, test pane shows
        # only the QGroupBox title bar (~24px) since it's collapsed
        # by default.
        self._v_splitter.setSizes([760, 28])
        layout.addWidget(self._v_splitter, 1)

    def _build_test_pane(self) -> QWidget:
        """Operator dry-run for the matcher: paste a message, choose
        the state to simulate, click Test, see which triggers would
        match (Layer 1 + Layer 2) and why non-matches were rejected.

        Tests UNSAVED edits — uses self._triggers directly, not the
        on-disk profile. Lets the operator calibrate phrase + context
        tokens + preconditions before saving.

        Collapsible: the QGroupBox title carries a checkbox (toggled
        via ``setCheckable``); unchecking it hides the entire pane
        content, giving the triggers table above the freed vertical
        space. The vertical splitter's setStretchFactor handles the
        actual reflow — Qt collapses an empty QGroupBox down to its
        title bar, which is what the operator wants here.
        """
        group = QGroupBox("Test matcher against a sample message")
        group.setCheckable(True)
        group.setChecked(False)  # collapsed by default — operators
                                 # usually only need the test pane
                                 # while authoring new triggers.
        group.toggled.connect(self._on_test_pane_toggled)
        gl = QVBoxLayout(group)
        gl.setContentsMargins(8, 8, 8, 8)
        gl.setSpacing(6)
        self._test_group = group

        # Wrap the inner widgets in a single container so toggling
        # the group's checkbox shows/hides them as one unit — without
        # this each child would need its own setVisible call.
        self._test_content = QWidget()
        content_layout = QVBoxLayout(self._test_content)
        content_layout.setContentsMargins(0, 0, 0, 0)
        content_layout.setSpacing(6)
        self._test_content.setVisible(False)
        gl.addWidget(self._test_content)
        gl = content_layout  # rest of the existing builder appends here

        self._test_input = QPlainTextEdit()
        self._test_input.setPlaceholderText(
            "paste a sample message here, then click Test…"
        )
        self._test_input.setMaximumHeight(80)
        gl.addWidget(self._test_input)

        state_row = QHBoxLayout()
        state_row.setSpacing(12)
        state_row.addWidget(QLabel("Simulated state:"))
        self._test_has_open = QCheckBox("open position")
        self._test_has_open.setChecked(True)  # the most useful default
        self._test_has_closed = QCheckBox("closed within 24h")
        self._test_has_pending = QCheckBox("pending OPEN")
        for cb in (self._test_has_open, self._test_has_closed, self._test_has_pending):
            state_row.addWidget(cb)
        state_row.addSpacing(8)
        state_row.addWidget(QLabel("Open side:"))
        self._test_side = QComboBox()
        self._test_side.addItems(["(unspecified)", "BUY", "SELL"])
        self._test_side.setFixedWidth(120)
        state_row.addWidget(self._test_side)
        state_row.addStretch()
        self._test_btn = QPushButton("Test")
        self._test_btn.setProperty("variant", "primary")
        self._test_btn.clicked.connect(self._on_test_clicked)
        state_row.addWidget(self._test_btn)
        gl.addLayout(state_row)

        # Verdict — the answer the operator actually wants. Big,
        # colour-coded, no other noise.
        self._test_verdict = QLabel("")
        self._test_verdict.setTextFormat(Qt.TextFormat.RichText)
        self._test_verdict.setWordWrap(True)
        self._test_verdict.setStyleSheet(
            "QLabel { padding: 8px 12px; border-radius: 4px; "
            "font-size: 14px; }"
        )
        gl.addWidget(self._test_verdict)

        # One-line explanation under the verdict — which layer fired
        # and why, in plain prose. Kept short on purpose.
        self._test_explain = QLabel("")
        self._test_explain.setTextFormat(Qt.TextFormat.RichText)
        self._test_explain.setWordWrap(True)
        self._test_explain.setStyleSheet("padding: 0 4px 4px 4px;")
        gl.addWidget(self._test_explain)

        # Details collapse — hidden by default. The full per-rule
        # diagnostics + Layer 2 score table live here for the rare
        # case where the operator needs to debug WHY a specific rule
        # didn't fire (e.g. tuning context tokens or the threshold).
        self._test_details_btn = QPushButton("Show details ▾")
        self._test_details_btn.setFlat(True)
        self._test_details_btn.setCheckable(True)
        self._test_details_btn.setStyleSheet(
            f"QPushButton {{ color: {current_palette().text_muted}; text-align: left; padding: 2px 4px; }}"
        )
        self._test_details_btn.toggled.connect(self._on_test_details_toggled)
        gl.addWidget(self._test_details_btn)

        self._test_details = QTextEdit()
        self._test_details.setReadOnly(True)
        self._test_details.setVisible(False)
        gl.addWidget(self._test_details, 1)

        # Placeholder verdict before the operator clicks Test.
        _tm = current_palette().text_muted
        self._test_verdict.setText(
            f"<span style='color:{_tm};'>"
            "Click <b>Test</b> to see what the matcher would do with a sample message."
            "</span>"
        )

        return group

    def _on_test_details_toggled(self, checked: bool) -> None:
        self._test_details.setVisible(checked)
        self._test_details_btn.setText(
            "Hide details ▴" if checked else "Show details ▾"
        )

    def _on_test_pane_toggled(self, checked: bool) -> None:
        """Collapse / expand the Test pane.

        When unchecked the inner content widget is hidden so only the
        QGroupBox title bar shows — the vertical splitter gives the
        freed space to the editor above. Expanding restores the
        previous ~240px share or grows a fresh expansion if the
        operator dragged the editor over the test pane while collapsed.
        """
        self._test_content.setVisible(checked)
        if not hasattr(self, "_v_splitter"):
            return
        sizes = self._v_splitter.sizes()
        if len(sizes) != 2:
            return
        total = sum(sizes) or self._v_splitter.height() or 800
        if checked:
            # Expand to roughly a third of the available height.
            test_h = max(240, total // 3)
            editor_h = max(200, total - test_h)
            self._v_splitter.setSizes([editor_h, test_h])
        else:
            # Collapse to just the title-bar height; give everything
            # else to the editor.
            title_h = self._test_group.fontMetrics().height() + 16
            self._v_splitter.setSizes([max(200, total - title_h), title_h])

    def _on_test_clicked(self) -> None:
        from src.text_normalize import normalize
        from src import trigger_matcher
        from src.gui.services import profile_io

        text = self._test_input.toPlainText().strip()
        if not text:
            QMessageBox.information(
                self, "Test matcher", "Paste a sample message first.",
            )
            return

        # Compose MatchContext from the operator's simulated-state
        # checkboxes. The matcher's production path builds this from
        # SQL; here we let the operator dictate it so they can probe
        # "what would fire when nothing is open" vs the open case.
        side_idx = self._test_side.currentIndex()
        open_side = None if side_idx == 0 else self._test_side.currentText()
        ctx = trigger_matcher.MatchContext(
            has_open_position=self._test_has_open.isChecked(),
            open_position_side=open_side,
            has_closed_within_24h=self._test_has_closed.isChecked(),
            has_pending_open=self._test_has_pending.isChecked(),
        )

        # Pull the symbol from the current profile so CANCEL_PENDING
        # diagnostics show the channel's real symbol rather than a
        # placeholder. Avoids a UI input the operator would have to
        # mirror from the Profile tab.
        try:
            data = profile_io.load_profile(self._active_profile_name())
            symbol = str(data.get("symbol") or "").strip()
        except Exception:
            symbol = ""

        # Disable the button during the embedding call — Layer 2 makes
        # a network round-trip and can take 100-300 ms. Without a
        # disable the operator could double-click and queue calls.
        self._test_btn.setEnabled(False)
        self._test_btn.setText("Testing…")
        try:
            # Pass the active stack's db_path so the matcher resolves the
            # OpenAI key from the right DB. Without this, the matcher
            # falls back to config.OPENAI_API_KEY which reads from the
            # GUI process's default DB_PATH (the `default` stack) — not
            # the active stack the operator is currently editing.
            result = trigger_matcher.test_match(
                text, self._triggers, ctx, symbol,
                db_path=self._stack.db_path,
            )
        except Exception as e:  # noqa: BLE001 — must not break the editor
            self._test_verdict.setText(
                f"<span style='color:#ef5350;'>"
                f"<b>Test failed:</b> {_html_escape(type(e).__name__)}: "
                f"{_html_escape(str(e))}</span>"
            )
            self._test_explain.setText("")
            self._test_details.setHtml("")
            self._test_btn.setEnabled(True)
            self._test_btn.setText("Test")
            return
        self._test_btn.setEnabled(True)
        self._test_btn.setText("Test")

        self._render_test_result(result, text)

    def _render_test_result(self, result, text: str) -> None:
        """Render the diagnostic result in the new layout: big verdict
        on top, one-line explanation underneath, full per-layer detail
        in the collapsible block."""
        verdict_html, explain_html = self._format_test_verdict(result)
        self._test_verdict.setText(verdict_html)
        self._test_explain.setText(explain_html)
        self._test_details.setHtml(self._format_test_details(result, text))

    @staticmethod
    def _format_test_verdict(result) -> tuple[str, str]:
        """Two strings: the big verdict (with background colour) and a
        plain-prose one-liner under it explaining WHICH layer decided
        and WHY. No per-rule lists here — that's the details block."""
        from src import trigger_matcher

        # Determine which layer (if any) produced the emit. The
        # production match() picks Layer 1 first; Layer 2 only fires
        # when Layer 1 has no matches.
        layer1_matched = [d for d in result.layer1 if d.matched]
        if result.actions and layer1_matched:
            types_str = ", ".join(a.type for a in result.actions)
            verdict = (
                f"<span style='color:#26a69a;'>"
                f"<b>✓ Matched — would emit {_html_escape(types_str)}</b>"
                f"</span>"
            )
            # Pick the strongest emittable Layer-1 match for the prose line
            # (IGNORE rules don't emit, so they never explain an emit).
            emittable_matched = [
                d for d in layer1_matched if d.rule.action_type != "IGNORE"
            ]
            best = max(
                emittable_matched or layer1_matched,
                key=lambda d: len(d.rule.norm_phrase),
            )
            _tm_v = current_palette().text_muted
            explain = (
                f"<span style='color:{_tm_v};'>Matched by the "
                f"<b>deterministic matcher</b> on phrase "
                f"<i>{_html_escape(best.rule.phrase)}</i>"
                f" → emits <b>{_html_escape(best.rule.action_type)}</b>"
                f" without calling Sonnet.</span>"
            )
            return verdict, explain

        if result.actions:
            # No Layer-1 hit, but actions were emitted → Layer 2 fired.
            fired = next((d for d in result.layer2 if d.would_fire), None)
            types_str = ", ".join(a.type for a in result.actions)
            verdict = (
                f"<span style='color:#26a69a;'>"
                f"<b>✓ Matched — would emit {_html_escape(types_str)}</b>"
                f"</span>"
            )
            if fired:
                _tm_v2 = current_palette().text_muted
                explain = (
                    f"<span style='color:{_tm_v2};'>Matched by the "
                    f"<b>embedding matcher</b> "
                    f"(score {fired.score:.3f} vs threshold "
                    f"{trigger_matcher.EMBEDDING_THRESHOLD:.2f}) on phrase "
                    f"<i>{_html_escape(fired.rule.phrase)}</i>"
                    f" → emits <b>{_html_escape(fired.rule.action_type)}</b>"
                    f" without calling Sonnet.</span>"
                )
            else:
                explain = ""
            return verdict, explain

        if result.suppressed_by:
            # An IGNORE trigger matched on whole-message equality — the
            # message is dropped before triage + Sonnet.
            verdict = (
                "<span style='color:#26a69a;'>"
                "<b>✓ Suppressed — would drop as curated noise</b>"
                "</span>"
            )
            _tm_v3 = current_palette().text_muted
            explain = (
                f"<span style='color:{_tm_v3};'>Matched an <b>IGNORE</b> "
                f"trigger on whole-message equality with sample "
                f"<i>{_html_escape(result.suppressed_by.phrase)}</i>"
                f" → dropped before triage + Sonnet.</span>"
            )
            return verdict, explain

        # No match anywhere → falls through to Sonnet in production.
        _tm_v4 = current_palette().text_muted
        verdict = (
            f"<span style='color:{_tm_v4};'>"
            "<b>✗ No match — Sonnet would handle this message</b>"
            "</span>"
        )
        # Compose a one-liner from the closest near-miss so the
        # operator can see WHY nothing fired.
        explain_bits: list[str] = []
        if not result.layer1:
            explain_bits.append("no triggers in profile")
        else:
            explain_bits.append(
                f"Layer 1: 0 of {len(result.layer1)} rules matched"
            )
        if result.layer2_note:
            explain_bits.append(
                f"Layer 2: {_html_escape(result.layer2_note)}"
            )
        elif result.layer2:
            top = result.layer2[0]
            thr = trigger_matcher.EMBEDDING_THRESHOLD
            explain_bits.append(
                f"Layer 2: top similarity was {top.score:.3f} "
                f"(threshold {thr:.2f})"
            )
        _tm_v5 = current_palette().text_muted
        explain = (
            f"<span style='color:{_tm_v5};'>"
            + " · ".join(explain_bits)
            + ". Click <b>Show details</b> below for per-rule diagnostics."
            "</span>"
        )
        return verdict, explain

    @staticmethod
    def _format_test_details(result, text: str) -> str:
        """The full per-rule diagnostic — hidden by default behind the
        Show details toggle. This is what the operator needs when
        tuning a phrase or context token; not what they want every
        time they click Test."""
        from src.text_normalize import normalize
        from src import trigger_matcher

        parts: list[str] = []
        _tm_d = current_palette().text_muted
        norm = normalize(text)
        parts.append(
            f"<p style='color:{_tm_d}; margin:0 0 6px 0; font-size:11px;'>"
            f"<b>Normalised message:</b> <code>{_html_escape(norm)}</code></p>"
        )

        # ----- Layer 1 -----
        parts.append("<p style='margin:6px 0 2px 0;'><b>Layer 1 — deterministic</b></p>")
        if not result.layer1:
            parts.append(
                f"<p style='color:{_tm_d}; margin:0 0 6px 0;'>"
                "(no triggers in profile)</p>"
            )
        else:
            parts.append("<ul style='margin:0 0 6px 16px; padding:0;'>")
            for diag in result.layer1:
                icon = "✓" if diag.matched else "✗"
                color = "#26a69a" if diag.matched else _tm_d
                phrase = _html_escape(diag.rule.phrase)
                parts.append(
                    f"<li style='color:{color};'>"
                    f"{icon} <b>{diag.rule.action_type}</b> "
                    f"<i>{phrase}</i> — {_html_escape(diag.reason)}</li>"
                )
            parts.append("</ul>")

        # ----- Layer 2 -----
        parts.append("<p style='margin:6px 0 2px 0;'><b>Layer 2 — embedding similarity</b></p>")
        if result.layer2_note:
            parts.append(
                f"<p style='color:#ff9800; margin:0 0 6px 0;'>{_html_escape(result.layer2_note)}</p>"
            )
        if not result.layer2:
            if not result.layer2_note:
                parts.append(
                    f"<p style='color:{_tm_d}; margin:0 0 6px 0;'>(no scores)</p>"
                )
        else:
            thr = trigger_matcher.EMBEDDING_THRESHOLD
            parts.append(
                f"<p style='color:{_tm_d}; margin:0 0 4px 0; font-size:11px;'>"
                f"threshold = {thr:.2f}. Top {min(len(result.layer2), 5)} of "
                f"{len(result.layer2)}.</p>"
            )
            parts.append("<ul style='margin:0 0 6px 16px; padding:0;'>")
            for diag in result.layer2[:5]:
                fire_color = "#26a69a" if diag.would_fire else (
                    "#ff9800" if diag.score >= thr else _tm_d
                )
                fire_icon = "✓" if diag.would_fire else (
                    "○" if diag.score >= thr else "·"
                )
                phrase = _html_escape(diag.rule.phrase)
                extra = (
                    f" — <span style='color:#ff9800;'>{_html_escape(diag.skipped_reason)}</span>"
                    if diag.skipped_reason and diag.score >= thr else ""
                )
                parts.append(
                    f"<li style='color:{fire_color};'>"
                    f"{fire_icon} <b>{diag.score:.3f}</b> "
                    f"<b>{diag.rule.action_type}</b> "
                    f"<i>{phrase}</i>{extra}</li>"
                )
            parts.append("</ul>")

        return "".join(parts)

    def _load_from_disk(self) -> None:
        data = profile_io.load_profile(self._active_profile_name())
        self._triggers = profile_io.load_triggers(data)
        self._dirty = False
        self._refresh_types_list(keep_current=True)
        self._refresh_dirty_marker()

    def _save(self) -> None:
        active = self._active_profile_name()
        data = profile_io.load_profile(active)
        data["triggers"] = self._triggers
        try:
            path = profile_io.save_profile(active, data)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self._dirty = False
        self._refresh_dirty_marker()
        QMessageBox.information(
            self, "Saved",
            f"Wrote {path}\n\nUse PROFILE → Reload Listener to apply.",
        )

    def _grouped(self) -> dict[str, list[int]]:
        out: dict[str, list[int]] = defaultdict(list)
        for i, t in enumerate(self._triggers):
            out[t["action_type"]].append(i)
        return out

    def _selected_type(self) -> str | None:
        item = self._types_list.currentItem()
        return item.data(Qt.ItemDataRole.UserRole) if item else None

    def _selected_global_indices(self) -> list[int]:
        at = self._selected_type()
        if at is None:
            return []
        local = sorted({i.row() for i in self._table.selectedIndexes()})
        type_indices = self._grouped().get(at, [])
        return [type_indices[r] for r in local if r < len(type_indices)]

    def _mark_dirty(self) -> None:
        self._dirty = True
        self._refresh_dirty_marker()

    def _refresh_dirty_marker(self) -> None:
        self._dirty_label.setText("● unsaved changes" if self._dirty else "")
        # Save / Revert moved to the profile-wide ribbon (parent
        # ProfileView). Save is always enabled there; the dirty marker
        # above signals when there are edits to commit.

    def _refresh_types_list(self, keep_current: bool = False) -> None:
        previous = self._selected_type() if keep_current else None
        self._types_list.blockSignals(True)
        self._types_list.clear()
        grouped = self._grouped()
        for at in ACTION_TYPES:
            count = len(grouped.get(at, []))
            item = QListWidgetItem(f"{at}  ({count})")
            item.setData(Qt.ItemDataRole.UserRole, at)
            self._types_list.addItem(item)
        self._types_list.blockSignals(False)
        target = previous or ACTION_TYPES[0]
        for row in range(self._types_list.count()):
            it = self._types_list.item(row)
            if it.data(Qt.ItemDataRole.UserRole) == target:
                self._types_list.setCurrentRow(row)
                break
        self._refresh_table()

    def _refresh_table(self) -> None:
        at = self._selected_type()
        self._table.blockSignals(True)
        self._table.setRowCount(0)
        if at is None:
            self._table.blockSignals(False)
            return
        self._right_label.setText(f"<b>Triggers · {at}</b>")
        type_indices = self._grouped().get(at, [])
        self._table.setRowCount(len(type_indices))
        for row, global_idx in enumerate(type_indices):
            t = self._triggers[global_idx]
            samples = t.get("samples") or []
            sample_text = samples[0] if samples else ""
            for col, value in enumerate((t.get("phrase", ""), sample_text, t.get("note", ""))):
                item = QTableWidgetItem(str(value))
                if col == 1:
                    item.setToolTip("\n\n".join(samples) if samples else "")
                self._table.setItem(row, col, item)
        self._table.blockSignals(False)
        # Reapply the filter so switching action_types keeps the same
        # filter active across views (operator can scope a search to
        # "everything matching SL in CLOSE_FULL" then flip to
        # CLOSE_PARTIAL and instantly see those matches too).
        self._apply_filter()

    def _apply_filter(self, *_args) -> None:
        """Hide rows whose phrase / sample / note don't contain the
        current filter text (case-insensitive). Empty filter → show all.

        Searches the underlying ``self._triggers`` data rather than
        re-reading the QTableWidget, so the result is unaffected by
        column rendering order or future column additions. Also
        checks every sample, not just samples[0] — operators often
        store multiple sample messages and the filter should hit any
        of them.
        """
        if not hasattr(self, "_filter_edit"):
            return
        query = (self._filter_edit.text() or "").strip().lower()
        at = self._selected_type()
        if at is None:
            return
        type_indices = self._grouped().get(at, [])
        if not query:
            for row in range(self._table.rowCount()):
                self._table.setRowHidden(row, False)
            return
        for row, global_idx in enumerate(type_indices):
            t = self._triggers[global_idx]
            phrase = str(t.get("phrase", "") or "").lower()
            note = str(t.get("note", "") or "").lower()
            samples = t.get("samples") or []
            samples_text = " ".join(str(s) for s in samples).lower()
            match = (
                query in phrase
                or query in note
                or query in samples_text
            )
            self._table.setRowHidden(row, not match)

    def _on_type_selected(self, *_args) -> None:
        self._refresh_table()

    def _on_cell_edited(self, item: QTableWidgetItem) -> None:
        at = self._selected_type()
        if at is None:
            return
        type_indices = self._grouped().get(at, [])
        row = item.row()
        if row >= len(type_indices):
            return
        global_idx = type_indices[row]
        col = item.column()
        value = item.text().strip()
        trigger = self._triggers[global_idx]
        if col == 0:
            trigger["phrase"] = value
        elif col == 1:
            samples = trigger.get("samples") or []
            if value:
                if samples:
                    samples[0] = value
                else:
                    samples = [value]
            else:
                samples = samples[1:]
            trigger["samples"] = samples
        elif col == 2:
            trigger["note"] = value
        self._mark_dirty()

    def _on_add(self) -> None:
        at = self._selected_type()
        if at is None:
            return
        dlg = _AddTriggerDialog(at, self)
        if dlg.exec() != QDialog.DialogCode.Accepted or not dlg.result:
            return
        self._triggers.append(dlg.result)
        self._mark_dirty()
        self._refresh_types_list(keep_current=True)

    def _on_delete(self) -> None:
        targets = self._selected_global_indices()
        if not targets:
            return
        if QMessageBox.question(
            self, "Delete triggers",
            f"Delete {len(targets)} trigger(s)?",
        ) != QMessageBox.StandardButton.Yes:
            return
        for idx in sorted(targets, reverse=True):
            del self._triggers[idx]
        self._mark_dirty()
        self._refresh_types_list(keep_current=True)

    def _on_move(self) -> None:
        targets = self._selected_global_indices()
        if not targets:
            return
        current = self._selected_type() or ""
        choices = [at for at in ACTION_TYPES if at != current]
        new_type, ok = QInputDialog.getItem(
            self, "Move triggers", "Move to action type:",
            choices, 0, False,
        )
        if not ok or not new_type:
            return
        for idx in targets:
            self._triggers[idx]["action_type"] = new_type
        self._mark_dirty()
        self._refresh_types_list(keep_current=True)

    def _on_copy(self) -> None:
        """Duplicate the selected triggers under a different action type.

        Unlike Move (which mutates `action_type` in place), Copy appends
        deep copies so the original trigger keeps firing for its current
        type. Useful when the same channel phrase legitimately maps to
        more than one action (e.g. a compound "close half + BE" message
        whose sample should produce both CLOSE_PARTIAL and MOVE_SL_BE —
        the trigger_matcher's longest-match conflict policy fires both
        when their action_types differ; see src/trigger_matcher.py:297).
        """
        targets = self._selected_global_indices()
        if not targets:
            return
        current = self._selected_type() or ""
        choices = [at for at in ACTION_TYPES if at != current]
        new_type, ok = QInputDialog.getItem(
            self, "Copy triggers", "Copy to action type:",
            choices, 0, False,
        )
        if not ok or not new_type:
            return
        for idx in targets:
            clone = copy.deepcopy(self._triggers[idx])
            clone["action_type"] = new_type
            self._triggers.append(clone)
        self._mark_dirty()
        self._refresh_types_list(keep_current=True)

    def _on_bulk_import(self) -> None:
        dlg = _BulkImportDialog(self)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        messages = dlg.messages()
        if not messages:
            return
        progress_dlg = _ProgressDialog(len(messages), self)
        worker = _BulkClassifyWorker(messages, self._stack, parent=self)
        worker.progress.connect(progress_dlg.set_progress)
        worker.done.connect(lambda items: self._on_bulk_done(items, progress_dlg))
        worker.failed.connect(lambda err: self._on_bulk_failed(err, progress_dlg))
        progress_dlg.cancelled.connect(worker.quit)
        self._bulk_worker = worker
        from src.gui.services.thread_registry import register
        register(worker, stop_fn=worker.quit)
        worker.start()
        progress_dlg.exec()

    def _on_bulk_done(self, items: list[dict], dlg: "_ProgressDialog") -> None:
        dlg.accept()
        self._triggers.extend(items)
        self._mark_dirty()
        self._refresh_types_list(keep_current=True)
        QMessageBox.information(
            self, "Bulk import",
            f"Classified and appended {len(items)} message(s). Review per "
            "action type and move any misclassifications.",
        )

    def _on_bulk_failed(self, err: str, dlg: "_ProgressDialog") -> None:
        dlg.reject()
        QMessageBox.critical(self, "Bulk import failed", err)


class _AddTriggerDialog(QDialog):
    def __init__(
        self,
        default_action_type: str,
        parent: QWidget,
        *,
        default_phrase: str = "",
        default_sample: str = "",
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add trigger")
        self.resize(520, 320)
        self.result: dict | None = None

        self._type = QComboBox()
        for at in ACTION_TYPES:
            self._type.addItem(at)
        idx = self._type.findText(default_action_type)
        if idx >= 0:
            self._type.setCurrentIndex(idx)

        # QPlainTextEdit (not QLineEdit) because real trigger phrases
        # are often multi-line — structured signal blocks span several
        # lines. QLineEdit silently strips newlines on paste, so a
        # pasted multi-line block would collapse to its first line
        # only and the trigger would never match the original wording.
        # The matcher itself normalises newlines to spaces, so storing
        # the verbatim multi-line text is the right thing to do.
        self._phrase = QPlainTextEdit()
        self._phrase.setPlaceholderText(
            "trigger phrase (verbatim — multi-line OK; the matcher "
            "normalises whitespace before comparing)"
        )
        self._phrase.setMaximumHeight(100)
        self._context_tokens = QLineEdit()
        self._context_tokens.setPlaceholderText(
            "optional: comma-separated tokens that MUST also appear "
            "(e.g. 'TP, الحمدلله')"
        )
        self._context_tokens.setToolTip(
            "Disambiguates short phrases that would otherwise over-trigger. "
            "All listed tokens must be present in the message AS WELL as "
            "the main phrase for the trigger to fire. Leave empty for "
            "phrase-only matching."
        )
        self._sample = QPlainTextEdit()
        self._sample.setPlaceholderText("optional: full sample message")
        self._note = QLineEdit()
        self._note.setPlaceholderText("optional note")
        if default_phrase:
            self._phrase.setPlainText(default_phrase)
        if default_sample:
            self._sample.setPlainText(default_sample)

        form = QFormLayout()
        form.addRow("Action type", self._type)
        form.addRow("Phrase", self._phrase)
        form.addRow("Context tokens", self._context_tokens)
        form.addRow("Sample", self._sample)
        form.addRow("Note", self._note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        phrase = self._phrase.toPlainText().strip()
        if not phrase:
            QMessageBox.warning(self, "Required", "Phrase can't be empty.")
            return
        sample = self._sample.toPlainText().strip()
        ctx_raw = self._context_tokens.text().strip()
        context_tokens = (
            [t.strip() for t in ctx_raw.split(",") if t.strip()]
            if ctx_raw else []
        )
        self.result = {
            "action_type": self._type.currentText(),
            "phrase": phrase,
            "context_tokens": context_tokens,
            "samples": [sample] if sample else [],
            "note": self._note.text().strip(),
        }
        self.accept()


class _BulkImportDialog(QDialog):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Bulk import messages")
        self.resize(640, 480)

        _tm_bi = current_palette().text_muted
        info = QLabel(
            f"<span style='color:{_tm_bi};'>Paste one or more channel "
            "messages. Real signals span multiple lines, so messages "
            "MUST be separated by a <b>blank line</b> (an empty line "
            "between them). Each message is classified via AI; results "
            "are appended as triggers. You can move misclassified "
            "entries afterward.</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText(
            "paste message(s) here…\n\nseparate multiple messages with "
            "a BLANK LINE — single newlines are kept as part of the "
            "same message"
        )

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Classify")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)

        layout = QVBoxLayout(self)
        layout.addWidget(info)
        layout.addWidget(self._text, 1)
        layout.addWidget(buttons)

    def messages(self) -> list[str]:
        """Split the pasted blob into individual messages.

        ALWAYS split on blank lines (one or more empty lines between
        chunks), NEVER on single newlines. Real channel messages are
        routinely multi-line — splitting on `\\n` would shred one
        signal block ("TP1 ✅ \\n ربح 120 \\n الحمدلله") into separate
        single-line "messages", which the classifier then labels as
        independent fragments. The earlier auto-split-on-newline
        fallback caused exactly that — every multi-line trigger
        ended up stored with only its first line.

        Blank-line-only delimiter is the safe default; the help text
        and placeholder make this explicit so the operator knows to
        leave an empty line between messages.
        """
        raw = self._text.toPlainText()
        if not raw.strip():
            return []
        import re
        chunks = [c.strip() for c in re.split(r"\n\s*\n+", raw)]
        return [c for c in chunks if c]


class _ProgressDialog(QDialog):
    cancelled = Signal()

    def __init__(self, total: int, parent: QWidget) -> None:
        super().__init__(parent)
        self.setWindowTitle("Classifying…")
        self.setModal(True)
        self.resize(360, 100)
        self._label = QLabel(f"0 / {total}")
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.setProperty("variant", "danger")
        self._cancel_btn.clicked.connect(self._on_cancel)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label)
        row = QHBoxLayout()
        row.addStretch()
        row.addWidget(self._cancel_btn)
        layout.addLayout(row)

    def set_progress(self, current: int, total: int) -> None:
        self._label.setText(f"{current} / {total}")

    def _on_cancel(self) -> None:
        self.cancelled.emit()
        self.reject()


