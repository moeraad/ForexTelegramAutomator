"""PROFILE view: structured editor for channels/<name>.json.

Each top-level field becomes a labeled section. Short fields use QLineEdit,
long fields use a monospace QPlainTextEdit. Save writes the JSON back to
disk; Reload Listener restarts the listener service so it picks up the new
prompt without manual intervention.
"""
from __future__ import annotations

import json
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSplitter,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src.gui.services import nssm_client
from src.gui.services.playground import PlaygroundResult, format_actions, run_playground
from src.gui.services.stack_registry import Stack


_SHORT_FIELDS = ("name", "symbol", "language", "price_range_hint")
_PROSE_FIELDS = ("description", "header", "compound_messages")
_DERIVED_FIELDS = (
    "vocabulary_table",
    "commentary_filter",
    "worked_examples",
    "triage_keep_triggers",
)
# Hidden from the Editor entirely — managed in the Triggers tab.
_EDITOR_HIDDEN = frozenset({"triggers"})
_LANGUAGES = ("ar", "en", "fr", "es", "ru", "tr", "de", "pt")
_FIELD_ORDER = (
    "name",
    "description",
    "symbol",
    "language",
    "price_range_hint",
    "shorthand_decode_example",
    "header",
    "vocabulary_table",
    "compound_messages",
    "commentary_filter",
    "directional_command_flow",
    "worked_examples",
    "triage_keep_triggers",
)
_REQUIRED = ("name", "symbol", "header", "vocabulary_table", "worked_examples")


def _is_long_field(key: str) -> bool:
    return key not in _SHORT_FIELDS


def _mono_font() -> QFont:
    f = QFont("Consolas")
    f.setStyleHint(QFont.StyleHint.Monospace)
    f.setPointSize(10)
    return f


def _prose_font() -> QFont:
    f = QFont()
    f.setPointSize(10)
    return f


class ProfileView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._original: OrderedDict[str, str] = OrderedDict()
        self._editors: dict[str, QWidget] = {}
        self._dirty = False
        self._build_ui()
        self.rebind(stack)

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._load_from_disk()
        if hasattr(self, "_triggers_tab"):
            self._triggers_tab.rebind(stack)

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        self._title = QLabel()
        self._title.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(self._title)
        self._dirty_marker = QLabel("")
        self._dirty_marker.setStyleSheet("color: #b58900;")
        top.addWidget(self._dirty_marker)
        top.addStretch()

        self._reload_btn = QPushButton("Save")
        self._reload_btn.clicked.connect(self._save)
        top.addWidget(self._reload_btn)

        self._revert_btn = QPushButton("Revert")
        self._revert_btn.clicked.connect(self._load_from_disk)
        top.addWidget(self._revert_btn)

        self._export_btn = QPushButton("Export…")
        self._export_btn.clicked.connect(self._export)
        top.addWidget(self._export_btn)

        self._restart_btn = QPushButton("Reload Listener")
        self._restart_btn.setToolTip("nssm restart <listener service>  ·  applies the saved profile")
        self._restart_btn.clicked.connect(self._reload_listener)
        top.addWidget(self._restart_btn)

        self._generate_btn = QPushButton("Generate from channel history…")
        self._generate_btn.setToolTip(
            "Fetch recent channel messages, classify each via AI, "
            "and derive a profile JSON from the results."
        )
        self._generate_btn.clicked.connect(self._open_generator_wizard)
        top.addWidget(self._generate_btn)

        layout.addLayout(top)

        self._meta_label = QLabel()
        self._meta_label.setStyleSheet("color: #93a1a1; padding-bottom: 4px;")
        layout.addWidget(self._meta_label)

        self._tabs = QTabWidget()
        self._editor_tab = QWidget()
        editor_layout = QVBoxLayout(self._editor_tab)
        editor_layout.setContentsMargins(0, 8, 0, 0)
        self._scroll = QScrollArea()
        self._scroll.setWidgetResizable(True)
        self._scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._content = QWidget()
        self._content_layout = QVBoxLayout(self._content)
        self._content_layout.setContentsMargins(0, 0, 0, 0)
        self._content_layout.setSpacing(12)
        self._scroll.setWidget(self._content)
        editor_layout.addWidget(self._scroll)
        self._tabs.addTab(self._editor_tab, "Editor")

        from src.gui.views.triggers_view import TriggersView
        self._triggers_tab = TriggersView(self._stack)
        self._tabs.addTab(self._triggers_tab, "Triggers")

        self._playground = _PlaygroundTab(lambda: self._stack)
        self._tabs.addTab(self._playground, "Playground")
        layout.addWidget(self._tabs, 1)

    def _load_from_disk(self) -> None:
        path = self._stack.profile_path
        self._title.setText(
            f"<span style='font-size:16px; font-weight:700;'>PROFILE</span>"
            f"&nbsp;&nbsp;<span style='color:#586e75;'>{self._stack.name}</span>"
        )
        self._meta_label.setText(f"file: {path}")
        if not path.exists():
            self._render_error(f"profile file not found: {path}")
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            self._render_error(f"profile JSON is invalid: {e}")
            return
        if not isinstance(data, dict):
            self._render_error("profile JSON must be an object at the top level")
            return
        ordered: OrderedDict[str, str] = OrderedDict()
        seen = set(_EDITOR_HIDDEN)
        for key in _FIELD_ORDER:
            if key in data and key not in _EDITOR_HIDDEN:
                ordered[key] = str(data[key]) if data[key] is not None else ""
                seen.add(key)
        for key, value in data.items():
            if key in seen:
                continue
            ordered[key] = str(value) if value is not None else ""
        self._original = ordered
        self._render_form(ordered)
        self._set_dirty(False)

    def _render_error(self, message: str) -> None:
        self._clear_content()
        lbl = QLabel(message)
        lbl.setStyleSheet("color: #dc322f; padding: 32px;")
        lbl.setWordWrap(True)
        self._content_layout.addWidget(lbl)
        self._content_layout.addStretch()

    def _clear_content(self) -> None:
        while self._content_layout.count() > 0:
            item = self._content_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        self._editors.clear()

    def _render_form(self, data: OrderedDict[str, str]) -> None:
        self._clear_content()
        # Load triggers once for derived widgets — they pull from the
        # structured array rather than re-parsing rendered prompt text.
        from src.gui.services import profile_io
        from src.gui.views._profile_readonly_widgets import (
            BulletListView,
            ChipFlowView,
            ExamplesTableView,
            TreeFieldView,
        )
        profile_dict = profile_io.load_profile(self._stack.name)
        triggers = profile_io.load_triggers(profile_dict)

        for key, value in data.items():
            section_label = QLabel(
                f"<span style='color:#93a1a1; font-size:10px; letter-spacing:1px; font-weight:700;'>"
                f"{key.upper()}</span>"
            )
            section_label.setTextFormat(Qt.TextFormat.RichText)
            self._content_layout.addWidget(section_label)

            if key in _DERIVED_FIELDS:
                widget = self._build_derived_widget(key)
                if widget is not None:
                    widget.set_triggers(triggers)
                    widget.editRequested.connect(self._jump_to_triggers_tab)
                    self._editors[key] = widget
                    self._content_layout.addWidget(widget)
                    continue
            if key == "language":
                combo = QComboBox()
                combo.setEditable(True)
                for lang in _LANGUAGES:
                    combo.addItem(lang)
                if value:
                    idx = combo.findText(value)
                    if idx >= 0:
                        combo.setCurrentIndex(idx)
                    else:
                        combo.setEditText(value)
                combo.currentTextChanged.connect(self._on_changed)
                self._editors[key] = combo
                self._content_layout.addWidget(combo)
                continue
            if key in _SHORT_FIELDS:
                line = QLineEdit(value)
                line.textChanged.connect(self._on_changed)
                self._editors[key] = line
                self._content_layout.addWidget(line)
                continue

            edit = QPlainTextEdit()
            if key in _PROSE_FIELDS:
                edit.setFont(_prose_font())
                edit.setMinimumHeight(60 if key == "description" else 100)
            else:
                edit.setFont(_mono_font())
                edit.setMinimumHeight(140 if key == "header" else 80)
            edit.setPlainText(value)
            edit.textChanged.connect(self._on_changed)
            self._editors[key] = edit
            self._content_layout.addWidget(edit)
        self._content_layout.addStretch()

    def _build_derived_widget(self, key: str):
        from src.gui.views._profile_readonly_widgets import (
            BulletListView,
            ChipFlowView,
            ExamplesTableView,
            TreeFieldView,
        )
        if key == "vocabulary_table":
            return TreeFieldView()
        if key == "commentary_filter":
            return BulletListView()
        if key == "worked_examples":
            return ExamplesTableView()
        if key == "triage_keep_triggers":
            return ChipFlowView()
        return None

    def _jump_to_triggers_tab(self) -> None:
        for idx in range(self._tabs.count()):
            if self._tabs.tabText(idx).strip().lower() == "triggers":
                self._tabs.setCurrentIndex(idx)
                return

    def _on_changed(self) -> None:
        self._set_dirty(self._current() != self._original)

    def _current(self) -> OrderedDict[str, str]:
        out: OrderedDict[str, str] = OrderedDict()
        for key in self._original.keys():
            widget = self._editors.get(key)
            if isinstance(widget, QLineEdit):
                out[key] = widget.text()
            elif isinstance(widget, QComboBox):
                out[key] = widget.currentText()
            elif isinstance(widget, QPlainTextEdit):
                out[key] = widget.toPlainText()
            else:
                # Derived widgets: keep the existing rendered value (it'll be
                # recomputed on save from the triggers array anyway).
                out[key] = self._original[key]
        return out

    def _set_dirty(self, dirty: bool) -> None:
        self._dirty = dirty
        self._dirty_marker.setText("●  unsaved" if dirty else "")

    def _validate(self, data: OrderedDict[str, str]) -> str | None:
        for key in _REQUIRED:
            if key not in data or not data[key].strip():
                return f"required field '{key}' is empty"
        return None

    def _save(self) -> None:
        data = self._current()
        err = self._validate(data)
        if err:
            QMessageBox.warning(self, "Cannot save", err)
            return
        from src.gui.services import profile_io
        # Merge editor-changes onto the on-disk JSON so the triggers array
        # (which the editor does NOT show) survives + drives derived fields.
        on_disk = profile_io.load_profile(self._stack.name)
        merged = dict(on_disk)
        for k, v in data.items():
            if k in _DERIVED_FIELDS:
                continue
            merged[k] = v
        try:
            path = profile_io.save_profile(self._stack.name, merged)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return
        self._original = data
        self._set_dirty(False)
        QMessageBox.information(
            self, "Saved",
            f"Wrote {path}\n\nClick 'Reload Listener' to apply the new prompt.",
        )

    def _export(self) -> None:
        suggested = f"{self._stack.name}_profile_backup.json"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export profile", suggested, "JSON (*.json)"
        )
        if not path_str:
            return
        try:
            Path(path_str).write_text(
                json.dumps(dict(self._current()), indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            QMessageBox.information(self, "Exported", f"Wrote {path_str}")
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))

    def _open_generator_wizard(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved profile changes. Discard them and run the "
                "generator (it overwrites this file)?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            )
            if ans != QMessageBox.StandardButton.Yes:
                return
        from src.gui.windows.profile_generator_wizard import ProfileGeneratorWizard
        wiz = ProfileGeneratorWizard(self._stack, self)
        if wiz.exec():
            self._load_from_disk()

    def _reload_listener(self) -> None:
        if self._dirty:
            ans = QMessageBox.question(
                self, "Unsaved changes",
                "You have unsaved profile changes. Save them before reloading?",
                QMessageBox.StandardButton.Save
                | QMessageBox.StandardButton.Discard
                | QMessageBox.StandardButton.Cancel,
            )
            if ans == QMessageBox.StandardButton.Cancel:
                return
            if ans == QMessageBox.StandardButton.Save:
                self._save()
                if self._dirty:
                    return
        listener_svc = self._stack.service_names[2]
        ok, msg = nssm_client.nssm_restart(listener_svc)
        if ok:
            QMessageBox.information(
                self, "Listener reloaded",
                f"{listener_svc} restarted.\nThe new prompt will apply to the next incoming message.",
            )
        else:
            QMessageBox.warning(
                self, "Restart failed",
                f"Could not restart {listener_svc}.\n\n{msg or 'nssm command failed'}",
            )


_AI_OPTIONS: list[tuple[str, str, str]] = [
    ("Default (.env)", "", ""),
    ("Anthropic — Claude Sonnet 4.6", "anthropic", "claude-sonnet-4-6"),
    ("Anthropic — Claude Opus 4.7",   "anthropic", "claude-opus-4-7"),
    ("Anthropic — Claude Haiku 4.5",  "anthropic", "claude-haiku-4-5-20251001"),
    ("OpenAI — gpt-5",       "openai", "gpt-5"),
    ("OpenAI — gpt-5-mini",  "openai", "gpt-5-mini"),
    ("OpenAI — gpt-5-nano",  "openai", "gpt-5-nano"),
]


class _PlaygroundRunner(QThread):
    finished_with = Signal(object)

    def __init__(self, stack: Stack, message: str, provider: str, model: str) -> None:
        super().__init__()
        self._stack = stack
        self._message = message
        self._provider = provider
        self._model = model

    def run(self) -> None:
        result = run_playground(
            self._stack,
            self._message,
            provider_override=self._provider or None,
            interpreter_model_override=self._model or None,
        )
        self.finished_with.emit(result)


class _PlaygroundTab(QWidget):
    def __init__(self, get_stack):
        super().__init__()
        self._get_stack = get_stack
        self._runner: _PlaygroundRunner | None = None
        self._build_ui()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        notice = QLabel(
            "<span style='color:#586e75;'>"
            "Live AI call against the active stack's state (no DB writes). "
            "Save profile changes before running so the prompt picks them up. "
            "Costs real tokens."
            "</span>"
        )
        notice.setTextFormat(Qt.TextFormat.RichText)
        notice.setWordWrap(True)
        layout.addWidget(notice)

        layout.addWidget(QLabel("MESSAGE  ·  paste a Telegram message verbatim"))
        self._input = QPlainTextEdit()
        self._input.setFont(_mono_font())
        self._input.setMinimumHeight(110)
        self._input.setPlaceholderText("e.g.  Xauusd buy limit.4708.21\\nSL 4705.17\\nTp 4736.10")
        layout.addWidget(self._input)

        action_row = QHBoxLayout()
        action_row.addWidget(QLabel("AI:"))
        self._ai_combo = QComboBox()
        for label, provider, model in _AI_OPTIONS:
            self._ai_combo.addItem(label, (provider, model))
        self._ai_combo.setMinimumWidth(260)
        action_row.addWidget(self._ai_combo)
        self._run_btn = QPushButton("Run  ·  triage → interpret")
        self._run_btn.clicked.connect(self._on_run)
        action_row.addWidget(self._run_btn)
        self._status = QLabel("")
        self._status.setStyleSheet("color: #93a1a1; padding-left: 12px;")
        action_row.addWidget(self._status)
        action_row.addStretch()
        layout.addLayout(action_row)

        results = QSplitter(Qt.Orientation.Horizontal)
        self._left_panel = QPlainTextEdit()
        self._left_panel.setReadOnly(True)
        self._left_panel.setFont(_mono_font())
        self._left_panel.setPlaceholderText("Triage + context appear here after Run.")
        self._right_panel = QPlainTextEdit()
        self._right_panel.setReadOnly(True)
        self._right_panel.setFont(_mono_font())
        self._right_panel.setPlaceholderText("Interpreter response appears here after Run.")
        results.addWidget(self._left_panel)
        results.addWidget(self._right_panel)
        results.setStretchFactor(0, 1)
        results.setStretchFactor(1, 2)
        layout.addWidget(results, 1)

    def _on_run(self) -> None:
        if self._runner is not None and self._runner.isRunning():
            return
        message = self._input.toPlainText().strip()
        if not message:
            QMessageBox.information(self, "Empty message", "Paste a message before Run.")
            return
        stack = self._get_stack()
        self._run_btn.setEnabled(False)
        self._status.setText("running…")
        self._left_panel.setPlainText("")
        self._right_panel.setPlainText("")
        provider, model = self._ai_combo.currentData()
        self._runner = _PlaygroundRunner(stack, message, provider, model)
        self._runner.finished_with.connect(self._on_finished)
        self._runner.finished.connect(self._on_thread_done)
        from src.gui.services.thread_registry import register
        register(self._runner, stop_fn=self._runner.quit)
        self._runner.start()

    def _on_thread_done(self) -> None:
        self._run_btn.setEnabled(True)
        self._runner = None

    def _on_finished(self, result: PlaygroundResult) -> None:
        ctx = result.context
        left = [
            f"PROFILE       {ctx.profile}",
            f"PROVIDER      {ctx.provider}",
            f"INTERPRETER   {ctx.interpreter_model}",
            f"TRIAGE MODEL  {ctx.triage_model}",
            f"OPEN POSNS    {ctx.open_count}",
            f"ELAPSED       {ctx.elapsed_total_ms} ms",
        ]
        if result.triage is not None:
            left += [
                "",
                "TRIAGE",
                f"  decision: {result.triage.decision}",
                f"  latency:  {result.triage.latency_ms} ms",
                f"  usage:    {result.triage.usage}",
                "  raw:",
                "    " + (result.triage.raw_text or "").replace("\n", "\n    "),
            ]
        left += ["", "STATE BLOCK (preview):", ctx.open_positions_block_preview]
        self._left_panel.setPlainText("\n".join(left))

        if result.error:
            self._right_panel.setPlainText(f"ERROR\n  {result.error}")
            self._status.setText("error")
            return

        if result.triage is None:
            self._right_panel.setPlainText("(no triage outcome)")
            self._status.setText("aborted")
            return

        if result.triage.decision != "keep":
            self._right_panel.setPlainText(
                f"INTERPRETER SKIPPED\n  triage decision: {result.triage.decision}"
            )
            self._status.setText(f"triage={result.triage.decision}")
            return

        if result.interpret is None:
            self._right_panel.setPlainText("(no interpreter outcome — see ERROR on the left)")
            self._status.setText("interpreter missing")
            return

        i = result.interpret
        out = [
            "INTERPRETER",
            f"  latency: {i.latency_ms} ms",
            f"  usage:   {i.usage}",
            "",
            f"ACTIONS  ({len(i.actions)})",
            format_actions(i.actions),
            "",
            "REASONING",
            i.reasoning or "(none)",
        ]
        self._right_panel.setPlainText("\n".join(out))
        self._status.setText(
            f"ok  ·  {len(i.actions)} action(s)  ·  {i.latency_ms} ms"
        )
