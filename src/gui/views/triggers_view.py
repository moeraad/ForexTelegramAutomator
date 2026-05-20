"""Triggers tab: manage the structured phrase -> action_type mapping.

Loads triggers from channels/<stack>.json, lets the user add/edit/delete/move
entries, and writes back (which also re-renders the derived prompt fields).
"""
from __future__ import annotations

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
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._triggers: list[dict] = []
        self._dirty = False
        self._bulk_worker: _BulkClassifyWorker | None = None
        self._build_ui()
        self.rebind(stack)

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._load_from_disk()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)

        toolbar = QHBoxLayout()
        self._dirty_label = QLabel("")
        self._dirty_label.setStyleSheet("color: #ff9800;")
        toolbar.addWidget(self._dirty_label)
        toolbar.addStretch()
        try:
            from qfluentwidgets import PrimaryPushButton
            self._save_btn = PrimaryPushButton("Save")
        except Exception:
            self._save_btn = QPushButton("Save")
        self._save_btn.clicked.connect(self._save)
        toolbar.addWidget(self._save_btn)
        self._revert_btn = QPushButton("Revert")
        self._revert_btn.clicked.connect(self._load_from_disk)
        toolbar.addWidget(self._revert_btn)
        self._bulk_btn = QPushButton("Bulk import…")
        self._bulk_btn.clicked.connect(self._on_bulk_import)
        toolbar.addWidget(self._bulk_btn)
        layout.addLayout(toolbar)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.setChildrenCollapsible(False)

        left = QWidget()
        left_layout = QVBoxLayout(left)
        left_layout.setContentsMargins(0, 0, 0, 0)
        left_layout.setSpacing(4)
        # Mirror the right pane's toolbar row height so the list and
        # table start at the same Y. Without this the right pane's
        # +Add/Delete/Move toolbar pushes the table down ~30 px and the
        # two scrollable areas look misaligned.
        left_toolbar = QHBoxLayout()
        left_toolbar.addWidget(QLabel("<b>Action types</b>"))
        left_toolbar.addStretch()
        # Invisible spacer button — same size as the right-pane buttons
        # so vertical alignment holds even after a theme swap changes
        # button heights.
        spacer_btn = QPushButton("")
        spacer_btn.setEnabled(False)
        spacer_btn.setFlat(True)
        spacer_btn.setStyleSheet("QPushButton { background: transparent; border: none; }")
        spacer_btn.setFixedSize(0, 28)
        left_toolbar.addWidget(spacer_btn)
        left_layout.addLayout(left_toolbar)
        self._types_list = QListWidget()
        self._types_list.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._types_list.currentItemChanged.connect(self._on_type_selected)
        left_layout.addWidget(self._types_list, 1)
        splitter.addWidget(left)

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(4)

        right_toolbar = QHBoxLayout()
        self._right_label = QLabel("<b>Triggers</b>")
        right_toolbar.addWidget(self._right_label)
        right_toolbar.addStretch()
        self._add_btn = QPushButton("+ Add")
        self._add_btn.clicked.connect(self._on_add)
        right_toolbar.addWidget(self._add_btn)
        self._delete_btn = QPushButton("Delete")
        self._delete_btn.clicked.connect(self._on_delete)
        right_toolbar.addWidget(self._delete_btn)
        self._move_btn = QPushButton("Move to…")
        self._move_btn.clicked.connect(self._on_move)
        right_toolbar.addWidget(self._move_btn)
        right_layout.addLayout(right_toolbar)

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

        # Vertical splitter: the existing list/table on top, the Test
        # pane on the bottom. Operator can resize as needed; defaults
        # give the editor ~70% of vertical space and the test pane ~30%.
        v_splitter = QSplitter(Qt.Orientation.Vertical)
        v_splitter.setChildrenCollapsible(False)
        v_splitter.addWidget(splitter)
        v_splitter.addWidget(self._build_test_pane())
        v_splitter.setStretchFactor(0, 3)
        v_splitter.setStretchFactor(1, 1)
        v_splitter.setSizes([520, 240])
        layout.addWidget(v_splitter, 1)

    def _build_test_pane(self) -> QWidget:
        """Operator dry-run for the matcher: paste a message, choose
        the state to simulate, click Test, see which triggers would
        match (Layer 1 + Layer 2) and why non-matches were rejected.

        Tests UNSAVED edits — uses self._triggers directly, not the
        on-disk profile. Lets the operator calibrate phrase + context
        tokens + preconditions before saving.
        """
        group = QGroupBox("Test matcher against a sample message")
        gl = QVBoxLayout(group)
        gl.setContentsMargins(8, 8, 8, 8)
        gl.setSpacing(6)

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
            "QPushButton { color: #787b86; text-align: left; padding: 2px 4px; }"
        )
        self._test_details_btn.toggled.connect(self._on_test_details_toggled)
        gl.addWidget(self._test_details_btn)

        self._test_details = QTextEdit()
        self._test_details.setReadOnly(True)
        self._test_details.setVisible(False)
        gl.addWidget(self._test_details, 1)

        # Placeholder verdict before the operator clicks Test.
        self._test_verdict.setText(
            "<span style='color:#787b86;'>"
            "Click <b>Test</b> to see what the matcher would do with a sample message."
            "</span>"
        )

        return group

    def _on_test_details_toggled(self, checked: bool) -> None:
        self._test_details.setVisible(checked)
        self._test_details_btn.setText(
            "Hide details ▴" if checked else "Show details ▾"
        )

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
            data = profile_io.load_profile(self._stack.name)
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
            # Pick the strongest Layer-1 match for the prose line.
            best = max(layer1_matched, key=lambda d: len(d.rule.norm_phrase))
            explain = (
                f"<span style='color:#787b86;'>Matched by the "
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
                explain = (
                    f"<span style='color:#787b86;'>Matched by the "
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

        # No match anywhere → falls through to Sonnet in production.
        verdict = (
            "<span style='color:#787b86;'>"
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
        explain = (
            "<span style='color:#787b86;'>"
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
        norm = normalize(text)
        parts.append(
            f"<p style='color:#787b86; margin:0 0 6px 0; font-size:11px;'>"
            f"<b>Normalised message:</b> <code>{_html_escape(norm)}</code></p>"
        )

        # ----- Layer 1 -----
        parts.append("<p style='margin:6px 0 2px 0;'><b>Layer 1 — deterministic</b></p>")
        if not result.layer1:
            parts.append(
                "<p style='color:#787b86; margin:0 0 6px 0;'>"
                "(no triggers in profile)</p>"
            )
        else:
            parts.append("<ul style='margin:0 0 6px 16px; padding:0;'>")
            for diag in result.layer1:
                icon = "✓" if diag.matched else "✗"
                color = "#26a69a" if diag.matched else "#787b86"
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
                    "<p style='color:#787b86; margin:0 0 6px 0;'>(no scores)</p>"
                )
        else:
            thr = trigger_matcher.EMBEDDING_THRESHOLD
            parts.append(
                f"<p style='color:#787b86; margin:0 0 4px 0; font-size:11px;'>"
                f"threshold = {thr:.2f}. Top {min(len(result.layer2), 5)} of "
                f"{len(result.layer2)}.</p>"
            )
            parts.append("<ul style='margin:0 0 6px 16px; padding:0;'>")
            for diag in result.layer2[:5]:
                fire_color = "#26a69a" if diag.would_fire else (
                    "#ff9800" if diag.score >= thr else "#787b86"
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
        data = profile_io.load_profile(self._stack.name)
        self._triggers = profile_io.load_triggers(data)
        self._dirty = False
        self._refresh_types_list(keep_current=True)
        self._refresh_dirty_marker()

    def _save(self) -> None:
        data = profile_io.load_profile(self._stack.name)
        data["triggers"] = self._triggers
        try:
            path = profile_io.save_profile(self._stack.name, data)
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
        self._save_btn.setEnabled(self._dirty)

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
    def __init__(self, default_action_type: str, parent: QWidget) -> None:
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

        info = QLabel(
            "<span style='color:#787b86;'>Paste one or more channel "
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
