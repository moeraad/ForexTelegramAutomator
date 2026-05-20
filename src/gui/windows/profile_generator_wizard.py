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
    QFormLayout,
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
            "Fetches recent messages, dedupes them, runs each through the "
            "live triage filter, then classifies the survivors. Produces a "
            "draft profile JSON for review."
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
            "<span style='color:#787b86;'>Mid-tier classifier runs on every "
            "prefilter survivor. 500 msgs ≈ $0.05 - $0.20 depending on "
            "provider.</span>"
        )
        cost.setTextFormat(Qt.TextFormat.RichText)
        cost.setWordWrap(True)

        form = QFormLayout()
        form.addRow("Max messages to fetch", self._max)
        form.addRow("Look back", self._lookback)

        self._settings_note = QLabel("")
        self._settings_note.setTextFormat(Qt.TextFormat.RichText)
        self._settings_note.setWordWrap(True)
        self._settings_note.setStyleSheet("color:#787b86;")

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addSpacing(8)
        layout.addWidget(self._settings_note)
        layout.addWidget(cost)
        layout.addStretch()

    def initializePage(self) -> None:
        from src import db_settings
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        prov = (db_settings.get_str(wiz.stack.db_path, "ai_provider", "anthropic")
                or "anthropic").lower()
        classifier_model = (
            "gpt-5-mini" if prov == "openai" else "claude-haiku-4-5-20251001"
        )
        self._settings_note.setText(
            f"<b>Pipeline:</b> fetch → dedup → prefilter → "
            f"classifier ({prov} · {classifier_model})"
        )

    def params(self) -> WizardParameters:
        return WizardParameters(
            max_messages=self._max.value(),
            lookback_days=self._lookback.value(),
        )

    def validatePage(self) -> bool:
        return True


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
        self._counts.setStyleSheet("color: #787b86;")
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
            "prefilter": "Pre-filtering (symbol / ad shape)…",
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

        # Funnel summary — fetched → dedup → prefilter → triage → classify
        # — gives the operator a sense of where messages went and whether
        # the pre-filter / triage stages caught what they should have.
        self._summary = QLabel("")
        self._summary.setTextFormat(Qt.TextFormat.RichText)
        self._summary.setWordWrap(True)
        self._summary.setStyleSheet(
            "QLabel { background: #1a1d29; color: #d1d4dc; padding: 8px 12px; "
            "border: 1px solid #2a2e39; border-radius: 4px; }"
        )

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["Phrase / sample", "Confidence", "Count"])
        self._tree.setColumnWidth(0, 480)
        self._tree.setColumnWidth(1, 80)
        self._tree.setColumnWidth(2, 60)

        layout = QVBoxLayout(self)
        layout.addWidget(self._summary)
        layout.addWidget(self._tree, 1)

    def initializePage(self) -> None:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        results = wiz.results
        self._tree.clear()
        if results is None:
            return
        # Funnel summary banner.
        prefiltered = results.prefiltered_symbol + results.prefiltered_ad
        classified = sum(
            1 for c in results.classifications
            if not c.reasoning.startswith("prefilter:")
        )
        self._summary.setText(
            f"<b>Funnel:</b> fetched {results.raw_fetched} → "
            f"unique {results.unique_after_dedup} → "
            f"pre-filter dropped {prefiltered} "
            f"(symbol={results.prefiltered_symbol}, ad={results.prefiltered_ad}) → "
            f"classifier produced {classified} "
            f"(failed: {results.failed_count})"
        )
        # Multi-label grouping: a classification with action_types=("OPEN",
        # "MODIFY_TPS") appears under BOTH parent buckets so the operator
        # can see the full reach of every message. Same `c` instance is
        # referenced by both child rows; selection in either contributes
        # the message to that bucket in _build_profile.
        grouped: dict[str, list[ClassifiedMessage]] = defaultdict(list)
        for c in results.classifications:
            types = c.action_types or ("UNKNOWN",)
            for at in types:
                grouped[at].append(c)
        # UNKNOWN sorted first — those are low-confidence and need operator
        # attention; everything else alphabetical for stable layout.
        def _bucket_order(name: str) -> tuple[int, str]:
            return (0 if name == "UNKNOWN" else 1, name)
        for action_type in sorted(grouped.keys(), key=_bucket_order):
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
                # Badges: compound (multi-bucket message), pending/market
                # (only on OPEN rows). Help the operator see at a glance
                # which rows carry extra metadata.
                badges: list[str] = []
                if len(e.action_types) > 1:
                    badges.append("[compound]")
                if action_type == "OPEN" and e.pending is True:
                    badges.append("[pending]")
                elif action_type == "OPEN" and e.pending is False:
                    badges.append("[market]")
                badge_str = (" " + " ".join(badges)) if badges else ""
                child = QTreeWidgetItem([
                    f"{phrase}{badge_str}\n   {sample}",
                    f"{e.confidence:.2f}",
                    str(e.msg_count),
                ])
                child.setFlags(child.flags() | Qt.ItemFlag.ItemIsUserCheckable)
                child.setCheckState(0, Qt.CheckState.Checked)
                child.setData(0, Qt.ItemDataRole.UserRole, e)
                # Amber tint for confidence-floored rows so operator
                # prioritizes triage.
                if e.reasoning.startswith("low_confidence"):
                    from PySide6.QtGui import QBrush, QColor
                    amber = QBrush(QColor(255, 193, 7, 60))
                    for col in range(self._tree.columnCount()):
                        child.setBackground(col, amber)
                parent.addChild(child)
            parent.setExpanded(False)
        self._tree.expandAll()

    def selected_classifications(self) -> list[tuple[ClassifiedMessage, str]]:
        """Return (message, bucket) pairs the operator approved.

        A compound message with action_types=("OPEN","MOVE_SL_BE") appears
        twice in the tree — once under OPEN, once under MOVE_SL_BE. The
        operator may check/uncheck them independently. This function
        returns one tuple per APPROVED (message, bucket) pair, preserving
        per-bucket operator decisions for compound rows.
        """
        out: list[tuple[ClassifiedMessage, str]] = []
        root = self._tree.invisibleRootItem()
        for i in range(root.childCount()):
            parent = root.child(i)
            if parent.checkState(0) == Qt.CheckState.Unchecked:
                continue
            # Parse the bucket name back out of the parent label
            # ("OPEN  (12)" -> "OPEN"). This is the only place we depend
            # on the label format; if it ever changes, update here.
            label = parent.text(0).strip()
            bucket = label.split()[0] if label else "UNKNOWN"
            for j in range(parent.childCount()):
                child = parent.child(j)
                if child.checkState(0) == Qt.CheckState.Checked:
                    c: ClassifiedMessage = child.data(0, Qt.ItemDataRole.UserRole)
                    out.append((c, bucket))
        return out


# --- P4 Save --------------------------------------------------------------


class _SavePage(QWizardPage):
    def __init__(self) -> None:
        super().__init__()
        self.setTitle("Generated profile")
        self.setSubTitle(
            "Fill in the description, then Finish to write "
            "channels/<stack>.json."
        )

        self._description = QPlainTextEdit()
        self._description.setPlaceholderText(
            "One-line description of the channel."
        )
        self._description.setMaximumHeight(60)
        self._preview = QPlainTextEdit()
        self._preview.setReadOnly(True)
        f = QFont("Consolas")
        f.setStyleHint(QFont.StyleHint.Monospace)
        f.setPointSize(9)
        self._preview.setFont(f)

        form = QFormLayout()
        form.addRow("Description", self._description)
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
            except (OSError, json.JSONDecodeError):
                pass
        self._refresh_preview()

    def _build_profile(self) -> OrderedDict:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        chosen = wiz.review_page.selected_classifications()  # list[(msg, bucket)]
        symbol = wiz.stack_symbol() or "XAUUSD"

        # Group by the bucket the operator approved each message for, NOT
        # by message.action_types — a compound message may be approved
        # under only one of its buckets (operator unchecked the other).
        grouped: dict[str, list[ClassifiedMessage]] = defaultdict(list)
        for c, bucket in chosen:
            grouped[bucket].append(c)

        # vocabulary_table: per bucket, distinct phrases.
        # OPEN splits into [OPEN — market] / [OPEN — pending] sub-sections
        # so the live interpreter prompt has explicit channel-specific
        # examples of which phrasings map to which pending intent.
        vocab_lines: list[str] = ["VOCABULARY -> ACTION MAP:"]
        for at in sorted(grouped.keys()):
            if at in ("IGNORE", "UNKNOWN", "ALERT", "CONTEXT"):
                continue
            entries = grouped[at]
            if at == "OPEN":
                market_phrases = sorted({c.phrase for c in entries if c.phrase and c.pending is False})
                pending_phrases = sorted({c.phrase for c in entries if c.phrase and c.pending is True})
                ambig_phrases = sorted({c.phrase for c in entries if c.phrase and c.pending is None})
                if market_phrases:
                    vocab_lines.append("  [OPEN — market]")
                    for p in market_phrases[:15]:
                        vocab_lines.append(f"    - {p}")
                if pending_phrases:
                    vocab_lines.append("  [OPEN — pending]")
                    for p in pending_phrases[:15]:
                        vocab_lines.append(f"    - {p}")
                if ambig_phrases:
                    vocab_lines.append("  [OPEN — pending intent unspecified]")
                    for p in ambig_phrases[:15]:
                        vocab_lines.append(f"    - {p}")
                continue
            phrases = sorted({c.phrase for c in entries if c.phrase})
            if not phrases:
                continue
            vocab_lines.append(f"  [{at}]")
            for p in phrases[:15]:
                vocab_lines.append(f"    - {p}")

        # worked_examples: top-2 highest-confidence per bucket. Compound
        # messages still get a single-bucket example per row (one example
        # per bucket they participate in) — that's the natural reading.
        ex_lines: list[str] = []
        idx = 1
        for at in sorted(grouped.keys()):
            if at in ("IGNORE", "UNKNOWN", "CONTEXT"):
                continue
            top = sorted(grouped[at], key=lambda c: -c.confidence)[:2]
            for c in top:
                msg = c.sample_text.replace("\n", " ").strip()
                # Use the message's FULL compound types in the example
                # output — preserves the compound nature for the live
                # interpreter to learn from.
                full_types = [t for t in c.action_types if t not in ("IGNORE", "UNKNOWN", "CONTEXT")]
                if not full_types:
                    full_types = [at]
                type_objs = ", ".join(f'{{"type":"{t}"}}' for t in full_types)
                ex_lines.append(
                    f"Ex{idx} ({at}): \"{msg[:160]}\"\n"
                    f"  -> [{type_objs}]"
                )
                idx += 1

        # triage_keep_triggers: all phrases from actionable buckets.
        triggers: list[str] = []
        for at, items in grouped.items():
            if at in ("IGNORE", "UNKNOWN", "CONTEXT"):
                continue
            triggers.extend(c.phrase for c in items if c.phrase)
        triggers = sorted(set(triggers))[:50]

        # commentary_filter: IGNORE phrases. CONTEXT is intentionally
        # excluded — those are analysis posts worth keeping in chat
        # context, just not acted on directly.
        ignore_phrases = sorted({c.phrase for c in grouped.get("IGNORE", []) if c.phrase})

        # Symbol / instrument config for the universal Stage 0 prefilter.
        # We surface placeholders the operator can edit in the profile JSON;
        # the wizard doesn't auto-discover yet (future enhancement: scan
        # corpus for high-frequency tokens to suggest other_instruments).
        symbol_aliases_existing: list[str] = []
        other_instruments_existing: list[str] = []
        existing_profile_path = Path(wiz.stack.db_path).parent / "profile.json"
        if existing_profile_path.exists():
            try:
                _ex = json.loads(existing_profile_path.read_text(encoding="utf-8"))
                symbol_aliases_existing = list(_ex.get("symbol_aliases") or [])
                other_instruments_existing = list(_ex.get("other_instruments") or [])
            except (OSError, json.JSONDecodeError):
                pass

        return OrderedDict([
            ("name", wiz.stack.name),
            ("description", self._description.toPlainText().strip()),
            ("symbol", symbol),
            ("symbol_aliases", symbol_aliases_existing),
            ("other_instruments", other_instruments_existing),
            ("language", _detect_language(chosen)),
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
                    "action_types": list(c.action_types),
                    "phrase": c.phrase,
                    "pending": c.pending,
                    "samples": [c.sample_text],
                    "approved_bucket": bucket,
                    "note": "",
                }
                for c, bucket in chosen
            ]),
        ])

    def _refresh_preview(self) -> None:
        profile = self._build_profile()
        self._preview.setPlainText(json.dumps(profile, indent=2, ensure_ascii=False))

    def validatePage(self) -> bool:
        wiz: "ProfileGeneratorWizard" = self.wizard()  # type: ignore[assignment]
        profile = dict(self._build_profile())
        # Hard invariant: refuse to write a profile where the same phrase
        # is listed as both a vocabulary trigger (do act) and commentary
        # filter (do NOT act). The interpreter sees contradictory
        # instructions and the resolution is non-deterministic — money
        # is at stake. Operator must go back to the review page and pick
        # one bucket for the conflicting phrase(s).
        conflicts = _detect_phrase_conflicts(wiz.review_page.selected_classifications())
        if conflicts:
            sample = "\n".join(f"  • {p}" for p in conflicts[:10])
            extra = f"\n  …and {len(conflicts) - 10} more" if len(conflicts) > 10 else ""
            QMessageBox.critical(
                self,
                "Conflicting phrases — cannot save",
                f"{len(conflicts)} phrase(s) are classified as BOTH a "
                f"trade trigger AND ignored commentary. The interpreter "
                f"would see contradictory rules.\n\n"
                f"Conflicting phrases:\n{sample}{extra}\n\n"
                f"Go back to Review and uncheck the phrase in one of the "
                f"two buckets so it appears only once.",
            )
            return False
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


def _detect_phrase_conflicts(
    selected: list[tuple[ClassifiedMessage, str]],
) -> list[str]:
    """Return phrases approved as BOTH a trade-action bucket AND IGNORE.

    Operates on (message, approved_bucket) pairs from the review tree —
    if the operator approved the same phrase under one trade bucket and
    also under IGNORE, that's a contradiction the interpreter would see.
    CONTEXT and UNKNOWN are non-actionable but also non-contradictory:
    a phrase being CONTEXT alongside an action means the message is
    informational, which is fine and not flagged here.
    """
    actionable: set[str] = set()
    ignored: set[str] = set()
    for c, bucket in selected:
        phrase = (c.phrase or "").strip()
        if not phrase:
            continue
        if bucket == "IGNORE":
            ignored.add(phrase)
        elif bucket in ("UNKNOWN", "CONTEXT", "ALERT"):
            continue
        else:
            actionable.add(phrase)
    return sorted(actionable & ignored)


def _detect_language(items: list[tuple[ClassifiedMessage, str]]) -> str:
    """Trivial heuristic: if any sample has Arabic chars, mark 'ar'.

    Accepts the new (message, bucket) tuple shape from
    selected_classifications. The bucket is ignored — we only need
    the underlying sample text to detect script.
    """
    seen: set[int] = set()
    count = 0
    for c, _bucket in items:
        if id(c) in seen:
            continue
        seen.add(id(c))
        count += 1
        for ch in c.sample_text:
            if "؀" <= ch <= "ۿ":
                return "ar"
        if count >= 50:
            break
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
