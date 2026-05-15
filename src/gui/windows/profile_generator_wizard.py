"""Profile Generator Wizard.

Walks the user through fetching channel history -> deduping ->
classifying via AI -> reviewing -> saving channels/<stack>.json.
"""
from __future__ import annotations

import json
from collections import OrderedDict, defaultdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QCheckBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
    QWizard,
    QWizardPage,
)

from src.gui.services.profile_wizard import (
    ClassifiedMessage,
    ProfileWizardWorker,
    WizardParameters,
    WizardResults,
)
from src.gui.services.stack_registry import Stack


_PAGE_INTRO = 0
_PAGE_PROGRESS = 1
_PAGE_REVIEW = 2
_PAGE_SAVE = 3


_BOILERPLATE_HEADER = (
    "You are a signal interpreter for a trading channel. You read each "
    "incoming message together with the current SYSTEM STATE block and "
    "decide what trading actions to emit. Your output is consumed by an "
    "automated trading system with NO human approval gate, so accuracy "
    "and idempotency are critical."
)

_BOILERPLATE_COMPOUND = (
    "COMPOUND MESSAGES:\n"
    "  A single message may carry multiple actions. Emit each as its own "
    "object in the output list. E.g. \"secure entry and book half\" -> "
    "[{\"type\":\"MOVE_SL_BE\"}, {\"type\":\"CLOSE_PARTIAL\",\"fraction\":0.5}]."
)

_BOILERPLATE_DIRECTIONAL = (
    "DIRECTIONAL COMMAND FLOW (bare \"BUY/SELL <symbol>\" with NO entry/SL/TP):\n"
    "  No position open               -> emit OPEN_INSTANT(side=BUY|SELL)\n"
    "  Naked same-side open           -> emit no actions (idempotent)\n"
    "  Naked opposite-side open       -> emit CLOSE_FULL + OPEN_INSTANT (flip)\n"
    "  Managed same-side open         -> emit no actions (verbal repeat)\n"
    "  Managed opposite-side open     -> emit CLOSE_FULL + OPEN_INSTANT (flip)\n"
    "Later, when a STRUCTURED signal (entry+SL+TPs) arrives and a naked "
    "position exists matching its side -> emit ATTACH_SIGNAL."
)

_BOILERPLATE_SHORTHAND = (
    "Example: Msg \"new trade from 1295-1294. SL 1308. Targets 85 then 65\"\n"
    "  SL=1308 explicit; entries below SL -> SELL.\n"
    "  \"95-94\" -> 1294-1295 (below 1308). \"85\"->1285, \"65\"->1265.\n"
    "  Output: OPEN side=SELL entry_low=1294 entry_high=1295 sl=1308 tps=[1285,1265]."
)


# --- P1 Intro / parameters ------------------------------------------------


class _IntroPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Generate channel profile from history")
        self.setSubTitle(
            "Fetches recent messages from the watched channel, dedupes, "
            "classifies each via cheap AI, and produces a draft profile JSON."
        )
        self._max = QSpinBox()
        self._max.setRange(50, 5000)
        self._max.setSingleStep(50)
        self._max.setValue(500)
        self._lookback = QSpinBox()
        self._lookback.setRange(1, 365)
        self._lookback.setValue(30)
        self._lookback.setSuffix(" days")

        cost = QLabel(
            "<span style='color:#586e75;'>Each unique message ~1k tokens "
            "× cheap-tier model. 500 msgs at Haiku ≈ $0.10 - $0.30.</span>"
        )
        cost.setTextFormat(Qt.TextFormat.RichText)
        cost.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Max messages to fetch", self._max)
        form.addRow("Look back", self._lookback)

        self._settings_note = QLabel("")
        self._settings_note.setTextFormat(Qt.TextFormat.RichText)
        self._settings_note.setWordWrap(True)
        self._settings_note.setStyleSheet("color:#586e75;")

        # Collapsible custom-rules editor (hidden by default).
        self._rules_toggle = QPushButton("▸ Custom classifier rules (optional)")
        self._rules_toggle.setFlat(True)
        self._rules_toggle.setStyleSheet(
            "QPushButton { text-align: left; color: #073642; padding: 4px 0; }"
            "QPushButton:hover { color: #268bd2; }"
        )
        self._rules_toggle.clicked.connect(self._toggle_rules)
        self._rules_expanded = False
        self._rules_hint = QLabel(
            "<span style='color:#586e75;'>Free-form guidance for the "
            "classifier. Plain language. Edits save back to the DB so "
            "the same rules apply to future runs.</span>"
        )
        self._rules_hint.setTextFormat(Qt.TextFormat.RichText)
        self._rules_hint.setWordWrap(True)
        self._rules_hint.hide()
        self._rules_edit = QPlainTextEdit()
        self._rules_edit.setPlaceholderText(
            "e.g.\n"
            "- Messages starting with 📅 are scheduled announcements -> IGNORE.\n"
            "- The phrase 'احسب نفسك' is colloquial CLOSE_FULL.\n"
            "- When BUY and SELL both appear with no zone, IGNORE."
        )
        self._rules_edit.setMinimumHeight(140)
        self._rules_edit.setMaximumHeight(220)
        self._rules_edit.hide()
        self._rules_counter = QLabel("")
        self._rules_counter.setStyleSheet("color: #93a1a1; font-size: 11px;")
        self._rules_counter.hide()
        self._rules_edit.textChanged.connect(self._refresh_rules_counter)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(self._settings_note)
        layout.addWidget(cost)
        layout.addSpacing(8)
        layout.addWidget(self._rules_toggle)
        layout.addWidget(self._rules_hint)
        layout.addWidget(self._rules_edit)
        layout.addWidget(self._rules_counter)
        layout.addStretch()

    def _toggle_rules(self) -> None:
        self._rules_expanded = not self._rules_expanded
        for w in (self._rules_hint, self._rules_edit, self._rules_counter):
            w.setVisible(self._rules_expanded)
        self._rules_toggle.setText(
            ("▾" if self._rules_expanded else "▸")
            + " Custom classifier rules (optional)"
        )

    def _refresh_rules_counter(self) -> None:
        n = len(self._rules_edit.toPlainText())
        color = "#dc322f" if n > 2000 else "#93a1a1"
        self._rules_counter.setText(
            f"<span style='color:{color};'>{n} chars · soft limit 2000</span>"
        )
        self._rules_counter.setTextFormat(Qt.TextFormat.RichText)

    def initializePage(self) -> None:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        bs, conc, prov, model = _resolve_classifier_settings(wiz.stack)
        self._settings_note.setText(
            f"<b>Classifier:</b> {prov} · {model} · "
            f"batch={bs} · concurrency={conc} "
            "(change in Settings → Tuning → CLASSIFIER)"
        )
        from src import db_settings
        existing = db_settings.get_str(wiz.stack.db_path, "classifier_custom_prompt", "")
        self._rules_edit.blockSignals(True)
        self._rules_edit.setPlainText(existing)
        self._rules_edit.blockSignals(False)
        self._refresh_rules_counter()

    def params(self) -> WizardParameters:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        bs, conc, _prov, _model = _resolve_classifier_settings(wiz.stack)
        return WizardParameters(
            max_messages=self._max.value(),
            lookback_days=self._lookback.value(),
            batch_size=bs,
            concurrency=conc,
        )

    def validatePage(self) -> bool:
        # Persist any edits to custom rules back to the DB so they take
        # effect for this run AND survive across runs.
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        from src import db_settings
        rules = self._rules_edit.toPlainText().strip()
        db_settings.set_str(wiz.stack.db_path, "classifier_custom_prompt", rules)
        return True


def _resolve_classifier_settings(stack) -> tuple[int, int, str, str]:
    """Read classifier settings from the stack DB with sane fallbacks."""
    from src import db_settings
    db = stack.db_path
    bs = db_settings.get_int(db, "classifier_batch_size", 10) or 10
    try:
        conc = int(db_settings.get_str(db, "classifier_concurrency", "4") or "4")
    except ValueError:
        conc = 4
    prov_raw = (db_settings.get_str(db, "classifier_provider", "") or "").lower()
    if prov_raw not in ("anthropic", "openai"):
        prov_raw = (db_settings.get_str(db, "ai_provider", "anthropic") or "anthropic").lower()
    if prov_raw == "openai":
        model = db_settings.get_str(db, "classifier_openai_model", "") or "gpt-5-nano"
    else:
        model = db_settings.get_str(db, "classifier_anthropic_model", "") or "claude-haiku-4-5-20251001"
    return bs, conc, prov_raw, model


# --- P2 Progress ----------------------------------------------------------


class _ProgressPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Working…")
        self.setSubTitle("Hands off until this completes — or click Cancel.")
        self._stage = QLabel("starting…")
        self._bar = QProgressBar()
        self._bar.setRange(0, 100)
        self._counts = QLabel("")
        self._counts.setStyleSheet("color: #586e75;")
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.clicked.connect(self._on_cancel)
        self._done = False
        self._worker: ProfileWizardWorker | None = None
        self._results: WizardResults | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(self._stage)
        layout.addWidget(self._bar)
        layout.addWidget(self._counts)
        row = QHBoxLayout()
        row.addWidget(self._cancel_btn)
        row.addStretch()
        layout.addLayout(row)
        layout.addStretch()

    def initializePage(self) -> None:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        self._done = False
        self._results = None
        params = wiz.intro_page.params()
        self._worker = ProfileWizardWorker(wiz.stack, params, parent=self)
        self._worker.stage_changed.connect(self._on_stage)
        self._worker.progress.connect(self._on_progress)
        self._worker.completed.connect(self._on_done)
        self._worker.failed.connect(self._on_failed)
        from src.gui.services.thread_registry import register
        register(self._worker, stop_fn=self._worker.cancel)
        self._worker.start()

    def cleanupPage(self) -> None:
        if self._worker and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(3000)

    def _on_stage(self, stage: str) -> None:
        labels = {
            "fetch": "Fetching messages from Telegram…",
            "dedup": "Deduplicating…",
            "classify": "Classifying messages via AI…",
            "done": "Done.",
        }
        self._stage.setText(labels.get(stage, stage))
        self._bar.setValue(0)

    def _on_progress(self, current: int, total: int) -> None:
        if total <= 0:
            self._bar.setRange(0, 0)
        else:
            self._bar.setRange(0, total)
            self._bar.setValue(current)
        self._counts.setText(f"{current} / {total}")

    def _on_done(self, results: WizardResults) -> None:
        self._results = results
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        wiz.results = results
        self._done = True
        self._cancel_btn.setEnabled(False)
        self.completeChanged.emit()
        wiz.next()

    def _on_failed(self, err: str) -> None:
        QMessageBox.critical(self, "Wizard failed", err)
        self._done = False
        self._cancel_btn.setText("Back")
        self._cancel_btn.setEnabled(True)
        self.completeChanged.emit()

    def _on_cancel(self) -> None:
        if self._worker:
            self._worker.cancel()
        self.wizard().back()

    def isComplete(self) -> bool:
        return self._done


# --- P3 Review ------------------------------------------------------------


class _ReviewPage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Review classifications")
        self.setSubTitle(
            "Uncheck rows you want to drop. Buckets with 0 entries are "
            "ignored in the generated profile."
        )

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Phrase / sample", "Confidence", "Count"])
        self._tree.setColumnWidth(0, 480)
        self._tree.setColumnWidth(1, 80)
        self._tree.setColumnWidth(2, 60)

        layout = QVBoxLayout(self)
        layout.addWidget(self._tree, 1)

    def initializePage(self) -> None:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        results = wiz.results
        self._tree.clear()
        if results is None:
            return
        grouped: dict[str, list[ClassifiedMessage]] = defaultdict(list)
        for c in results.classifications:
            grouped[c.action_type].append(c)
        for action_type in sorted(grouped.keys()):
            entries = sorted(grouped[action_type], key=lambda c: -c.confidence)
            parent = QTreeWidgetItem([
                f"{action_type}  ({len(entries)})",
                "",
                str(sum(e.msg_count for e in entries)),
            ])
            parent.setFlags(parent.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            parent.setCheckState(0, Qt.CheckState.Checked)
            font = QFont()
            font.setBold(True)
            parent.setFont(0, font)
            self._tree.addTopLevelItem(parent)
            for e in entries:
                phrase = e.phrase[:90] + ("…" if len(e.phrase) > 90 else "")
                sample = e.sample_text.replace("\n", " ")[:140]
                child = QTreeWidgetItem([
                    f"{phrase}\n   {sample}",
                    f"{e.confidence:.2f}",
                    str(e.msg_count),
                ])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                child.setData(0, Qt.ItemDataRole.UserRole, e)
                parent.addChild(child)
            parent.setExpanded(False)
        self._tree.expandAll()

    def selected_classifications(self) -> list[ClassifiedMessage]:
        out: list[ClassifiedMessage] = []
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            if parent.checkState(0) == Qt.CheckState.Unchecked:
                continue
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    c: ClassifiedMessage = child.data(0, Qt.ItemDataRole.UserRole)
                    out.append(c)
        return out


# --- P4 Save --------------------------------------------------------------


class _SavePage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Generated profile")
        self.setSubTitle(
            "Fill in description + price_range_hint, then Finish to write "
            "channels/<stack>.json."
        )

        self._description = QPlainTextEdit()
        self._description.setPlaceholderText(
            "One-line description of the channel."
        )
        self._description.setMaximumHeight(60)
        self._price_hint = QPlainTextEdit()
        self._price_hint.setPlaceholderText(
            "e.g. 'Gold trades roughly 4000-5500 USD/oz in 2026.'"
        )
        self._price_hint.setMaximumHeight(60)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self._preview.setFont(f)

        form = QFormLayout()
        form.addRow("Description", self._description)
        form.addRow("Price range hint", self._price_hint)
        self._refresh_btn = QPushButton("Refresh preview")
        self._refresh_btn.clicked.connect(self._refresh_preview)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(self._refresh_btn)
        layout.addWidget(QLabel("<b>Preview</b>"))
        layout.addWidget(self._preview, 1)

    def initializePage(self) -> None:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        existing_path = Path("channels") / f"{wiz.stack.name}.json"
        if existing_path.exists():
            try:
                data = json.loads(existing_path.read_text(encoding="utf-8"))
                self._description.setPlainText(data.get("description", ""))
                self._price_hint.setPlainText(data.get("price_range_hint", ""))
            except (OSError, json.JSONDecodeError):
                pass
        self._refresh_preview()

    def _build_profile(self) -> OrderedDict:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        chosen = wiz.review_page.selected_classifications()
        symbol = wiz.stack_symbol() or "XAUUSD"
        grouped: dict[str, list[ClassifiedMessage]] = defaultdict(list)
        for c in chosen:
            grouped[c.action_type].append(c)
        # vocabulary_table: per action_type, distinct phrases
        vocab_lines: list[str] = ["VOCABULARY -> ACTION MAP:"]
        for at in sorted(grouped.keys()):
            if at in ("IGNORE", "UNKNOWN", "ALERT"):
                continue
            phrases = sorted({c.phrase for c in grouped[at] if c.phrase})
            if not phrases:
                continue
            vocab_lines.append(f"  [{at}]")
            for p in phrases[:15]:
                vocab_lines.append(f"    - {p}")
        # worked_examples: top-2 highest-confidence per type
        ex_lines: list[str] = []
        idx = 1
        for at in sorted(grouped.keys()):
            if at in ("IGNORE", "UNKNOWN"):
                continue
            top = sorted(grouped[at], key=lambda c: -c.confidence)[:2]
            for c in top:
                msg = c.sample_text.replace("\n", " ").strip()
                ex_lines.append(
                    f"Ex{idx} ({at}): \"{msg[:160]}\"\n"
                    f"  -> [{{\"type\":\"{at}\"}}]"
                )
                idx += 1
        # triage_keep_triggers: all non-IGNORE/UNKNOWN phrases
        triggers: list[str] = []
        for at, items in grouped.items():
            if at in ("IGNORE", "UNKNOWN"):
                continue
            triggers.extend(c.phrase for c in items if c.phrase)
        triggers = sorted(set(triggers))[:50]
        # commentary_filter: IGNORE phrases
        ignore_phrases = sorted({c.phrase for c in grouped.get("IGNORE", []) if c.phrase})

        return OrderedDict([
            ("name", wiz.stack.name),
            ("description", self._description.toPlainText().strip()),
            ("symbol", symbol),
            ("language", _detect_language(chosen)),
            ("price_range_hint", self._price_hint.toPlainText().strip()),
            ("shorthand_decode_example", _BOILERPLATE_SHORTHAND),
            ("header", _BOILERPLATE_HEADER),
            ("vocabulary_table", "\n".join(vocab_lines)),
            ("compound_messages", _BOILERPLATE_COMPOUND),
            ("commentary_filter",
             "COMMENTARY FILTER (do NOT act on these):\n  "
             + "\n  ".join(f"- {p}" for p in ignore_phrases[:30])),
            ("directional_command_flow", _BOILERPLATE_DIRECTIONAL),
            ("worked_examples", "\n\n".join(ex_lines)),
            ("triage_keep_triggers",
             "ALWAYS-KEEP TRIGGERS (high-signal phrases):\n  "
             + " | ".join(triggers)),
            ("triggers", [
                {
                    "action_type": c.action_type,
                    "phrase": c.phrase,
                    "samples": [c.sample_text],
                    "note": "",
                }
                for c in chosen
            ]),
        ])

    def _refresh_preview(self) -> None:
        profile = self._build_profile()
        self._preview.setPlainText(json.dumps(profile, indent=2, ensure_ascii=False))

    def validatePage(self) -> bool:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        profile = dict(self._build_profile())
        from src.gui.services import profile_io
        try:
            path = profile_io.save_profile(wiz.stack.name, profile)
        except OSError as e:
            QMessageBox.critical(self, "Save failed", str(e))
            return False
        QMessageBox.information(
            self, "Profile saved",
            f"Wrote {path}\n\nUse PROFILE -> Reload Listener to apply.",
        )
        return True


def _detect_language(items: list[ClassifiedMessage]) -> str:
    """Trivial heuristic: if any sample has Arabic chars, mark 'ar'."""
    for c in items[:50]:
        for ch in c.sample_text:
            if "؀" <= ch <= "ۿ":
                return "ar"
    return "en"


# --- Wizard ---------------------------------------------------------------


class ProfileGeneratorWizard(QWizard):
    def __init__(self, stack: Stack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("CopyTrades — Generate channel profile")
        self.setMinimumSize(820, 620)
        self.setWizardStyle(QWizard.WizardStyle.ModernStyle)
        self.stack = stack
        self.results: WizardResults | None = None

        self.intro_page = _IntroPage()
        self.progress_page = _ProgressPage()
        self.review_page = _ReviewPage()
        self.save_page = _SavePage()
        self.setPage(_PAGE_INTRO, self.intro_page)
        self.setPage(_PAGE_PROGRESS, self.progress_page)
        self.setPage(_PAGE_REVIEW, self.review_page)
        self.setPage(_PAGE_SAVE, self.save_page)
        self.setStartId(_PAGE_INTRO)

    def stack_symbol(self) -> str:
        from src import db_settings
        existing_path = Path("channels") / f"{self.stack.name}.json"
        if existing_path.exists():
            try:
                data = json.loads(existing_path.read_text(encoding="utf-8"))
                if data.get("symbol"):
                    return data["symbol"]
            except (OSError, json.JSONDecodeError):
                pass
        return "XAUUSD"
