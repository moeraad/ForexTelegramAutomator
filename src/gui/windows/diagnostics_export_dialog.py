"""Diagnostics bundle dialog: pick a time window, then write a ZIP.

Lives behind the "Export diagnostics" button in Settings → Services.
The user picks a window (with quick presets), confirms what's
included, sees an estimated size, and on Export gets a QFileDialog
to choose where the ZIP lands.

The actual collection runs in a QThread so the GUI doesn't freeze
during a multi-MB read/zip pass.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from pathlib import Path

from PySide6.QtCore import Qt, QDateTime, QThread, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDateTimeEdit,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.diagnostics_export import (
    ExportOptions,
    ExportResult,
    Mt5Terminal,
    collect_diagnostics,
    enumerate_mt5_terminals,
    estimate_bundle_size,
)
from src.gui.services.stack_registry import Stack


class _ExportWorker(QThread):
    progress = Signal(str, int, int)     # stage, current, total
    completed = Signal(object)           # ExportResult
    failed = Signal(str)

    def __init__(self, opts: ExportOptions, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._opts = opts

    def run(self) -> None:
        try:
            result = collect_diagnostics(
                self._opts,
                progress=lambda s, c, t: self.progress.emit(s, c, t),
            )
            self.completed.emit(result)
        except Exception as e:  # noqa: BLE001 — surface to UI
            self.failed.emit(f"{type(e).__name__}: {e}")


class DiagnosticsExportDialog(QDialog):
    """Window picker + estimated-size readout + Export button.

    Window defaults to "last 24 hours" because that covers most
    incident-triage exports. Presets snap the pickers; Export freezes
    them at click time and hands an ExportOptions to the worker.
    """

    _PRESETS: list[tuple[str, timedelta | None]] = [
        ("Last hour",   timedelta(hours=1)),
        ("Last 24h",    timedelta(hours=24)),
        ("Last 7 days", timedelta(days=7)),
        ("All time",    None),   # special-cased: clamps to 1970..now
    ]

    def __init__(self, stack: Stack, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export diagnostics bundle")
        self.setMinimumWidth(620)
        self._stack = stack
        self._mt5_terminals: list[Mt5Terminal] = enumerate_mt5_terminals()
        self._worker: _ExportWorker | None = None
        self._build_ui()
        self._apply_preset(timedelta(hours=24))   # default window
        self._refresh_estimate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setSpacing(10)

        intro = QLabel(
            "<span style='color:#787b86;'>Bundles CopyTrades logs, the "
            "stack DB (sanitized), service crash logs, and optionally "
            "MT5 terminal/experts logs into a single ZIP. Secrets are "
            "blanked. Filter by time window to keep the bundle small.</span>"
        )
        intro.setTextFormat(Qt.TextFormat.RichText)
        intro.setWordWrap(True)
        layout.addWidget(intro)

        # Window pickers ---------------------------------------------------
        grid = QGridLayout()
        grid.setColumnStretch(1, 1)
        grid.addWidget(QLabel("From"), 0, 0)
        self._from = QDateTimeEdit()
        self._from.setCalendarPopup(True)
        self._from.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        grid.addWidget(self._from, 0, 1)
        grid.addWidget(QLabel("To"), 1, 0)
        self._to = QDateTimeEdit()
        self._to.setCalendarPopup(True)
        self._to.setDisplayFormat("yyyy-MM-dd HH:mm:ss")
        grid.addWidget(self._to, 1, 1)
        for w in (self._from, self._to):
            w.dateTimeChanged.connect(self._refresh_estimate)
        layout.addLayout(grid)

        # Presets ----------------------------------------------------------
        presets_row = QHBoxLayout()
        presets_row.addWidget(QLabel("Presets:"))
        for label, delta in self._PRESETS:
            btn = QPushButton(label)
            btn.setFlat(True)
            btn.clicked.connect(
                lambda _checked=False, d=delta: self._apply_preset(d)
            )
            presets_row.addWidget(btn)
        presets_row.addStretch()
        layout.addLayout(presets_row)

        # MT5 terminal picker --------------------------------------------
        # Auto-detect by EA presence is brittle — operators sometimes run
        # the bundle BEFORE installing the CopyTrades EA into MT5, or
        # have multiple MT5 installs and want to pick a specific one. So
        # enumerate every terminal under %APPDATA%/MetaQuotes/Terminal/
        # and let the operator choose. Pre-select the best candidate
        # (EA-bearing → most-recently-active → first).
        mt5_row = QGridLayout()
        mt5_row.setColumnStretch(1, 1)
        mt5_row.addWidget(QLabel("MT5 terminal"), 0, 0)
        self._mt5_combo = QComboBox()
        self._mt5_combo.addItem("(skip MT5 logs)", userData=None)
        for term in self._mt5_terminals:
            label = self._format_mt5_label(term)
            self._mt5_combo.addItem(label, userData=term.path)
        # Pre-select: first EA-bearing, else first listed, else "skip".
        preselect_index = 0
        for i, term in enumerate(self._mt5_terminals, start=1):
            if term.has_copytrades_ea:
                preselect_index = i
                break
        if preselect_index == 0 and self._mt5_terminals:
            preselect_index = 1
        self._mt5_combo.setCurrentIndex(preselect_index)
        self._mt5_combo.currentIndexChanged.connect(self._refresh_estimate)
        mt5_row.addWidget(self._mt5_combo, 0, 1)
        if not self._mt5_terminals:
            mt5_row.addWidget(
                QLabel("<span style='color:#787b86;'>"
                       "(no MT5 installs found under %APPDATA%/MetaQuotes/Terminal/)"
                       "</span>"),
                1, 1,
            )
        layout.addLayout(mt5_row)

        self._sanitize_db = QCheckBox(
            "Sanitize DB copy (blank secret rows)"
        )
        self._sanitize_db.setChecked(True)
        self._sanitize_db.setToolTip(
            "Strongly recommended. Leaves API keys / Telegram session "
            "encrypted-ciphertext blank in the shipped DB copy so the "
            "ZIP can be sent without leaking credentials."
        )
        layout.addWidget(self._sanitize_db)

        # Estimate readout ------------------------------------------------
        self._estimate_lbl = QLabel("Estimated size: —")
        self._estimate_lbl.setStyleSheet("color: #787b86;")
        layout.addWidget(self._estimate_lbl)

        # Progress bar (hidden until Export starts) -----------------------
        self._progress = QProgressBar()
        self._progress.setRange(0, 5)   # 5 stages incl. manifest
        self._progress.setVisible(False)
        self._progress_label = QLabel("")
        self._progress_label.setStyleSheet("color: #787b86;")
        self._progress_label.setVisible(False)
        layout.addWidget(self._progress)
        layout.addWidget(self._progress_label)

        # Buttons ---------------------------------------------------------
        self._buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Cancel
        )
        self._export_btn = QPushButton("Export…")
        self._export_btn.setDefault(True)
        self._export_btn.clicked.connect(self._on_export)
        self._buttons.addButton(
            self._export_btn, QDialogButtonBox.ButtonRole.AcceptRole
        )
        self._buttons.rejected.connect(self.reject)
        layout.addWidget(self._buttons)

    # ---- Range helpers --------------------------------------------------

    def _apply_preset(self, delta: timedelta | None) -> None:
        now = datetime.now()
        if delta is None:
            start = datetime(1970, 1, 1)
        else:
            start = now - delta
        self._from.setDateTime(QDateTime(
            start.year, start.month, start.day,
            start.hour, start.minute, start.second,
        ))
        self._to.setDateTime(QDateTime(
            now.year, now.month, now.day,
            now.hour, now.minute, now.second,
        ))
        self._refresh_estimate()

    def _current_range(self) -> tuple[datetime, datetime]:
        start = self._from.dateTime().toPython()
        end = self._to.dateTime().toPython()
        return start, end

    def _refresh_estimate(self) -> None:
        start, end = self._current_range()
        if end <= start:
            self._estimate_lbl.setText(
                "<span style='color:#ff9800;'>End must be after start.</span>"
            )
            self._estimate_lbl.setTextFormat(Qt.TextFormat.RichText)
            self._export_btn.setEnabled(False)
            return
        opts = self._build_opts(start, end, Path("placeholder.zip"))
        try:
            est = estimate_bundle_size(opts)
        except Exception:  # noqa: BLE001 — estimate is informational only
            est = 0
        mb = est / (1024 * 1024)
        self._estimate_lbl.setText(
            f"Estimated uncompressed size: {mb:.1f} MB "
            f"(zip will be much smaller)"
        )
        self._estimate_lbl.setTextFormat(Qt.TextFormat.PlainText)
        self._export_btn.setEnabled(True)

    def _build_opts(
        self, start: datetime, end: datetime, output_zip: Path,
    ) -> ExportOptions:
        mt5_dir: Path | None = self._mt5_combo.currentData()
        return ExportOptions(
            stack=self._stack,
            start=start,
            end=end,
            include_mt5=mt5_dir is not None,
            sanitize_db=self._sanitize_db.isChecked(),
            output_zip=output_zip,
            mt5_terminal_dir=mt5_dir,
        )

    @staticmethod
    def _format_mt5_label(term: Mt5Terminal) -> str:
        """Human-readable combobox label for one MT5 terminal.

        Shows: short hex id · EA presence · last-modified · log count.
        The short id is enough for the operator to recognise a
        specific install at a glance without taking the whole 32-char
        hash. Hover-tooltip would carry the full path; combobox items
        don't support per-entry tooltips in Qt by default, so the
        operator clicks into one and the absolute path appears in the
        manifest when they export.
        """
        short_id = term.terminal_id[:8] + "…"
        ea = "has CopyTrades EA" if term.has_copytrades_ea else "no EA"
        mod = term.last_modified.strftime("%Y-%m-%d %H:%M")
        return (
            f"{short_id}  ·  {ea}  ·  "
            f"{term.log_file_count} log file(s)  ·  last active {mod}"
        )

    # ---- Export flow ----------------------------------------------------

    def _on_export(self) -> None:
        start, end = self._current_range()
        if end <= start:
            return

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        default_name = (
            f"copytrades-diagnostics-{self._stack.name}-{stamp}.zip"
        )
        path_str, _ = QFileDialog.getSaveFileName(
            self,
            "Save diagnostics bundle",
            str(Path.home() / "Desktop" / default_name),
            "ZIP archive (*.zip)",
        )
        if not path_str:
            return

        opts = self._build_opts(start, end, Path(path_str))

        # Disable controls during the export to prevent re-entry.
        self._export_btn.setEnabled(False)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setEnabled(False)
        self._progress.setVisible(True)
        self._progress_label.setVisible(True)
        self._progress.setValue(0)
        self._progress_label.setText("Starting…")

        self._worker = _ExportWorker(opts, parent=self)
        self._worker.progress.connect(self._on_progress)
        self._worker.completed.connect(self._on_completed)
        self._worker.failed.connect(self._on_failed)
        self._worker.start()

    def _on_progress(self, stage: str, current: int, total: int) -> None:
        stage_index = {
            "logs": 1, "crashlogs": 2, "db": 3, "mt5": 4,
        }.get(stage, 0)
        self._progress.setValue(stage_index)
        labels = {
            "logs":      "Collecting CopyTrades logs…",
            "crashlogs": "Collecting service crash logs…",
            "db":        "Sanitizing + dumping DB…",
            "mt5":       "Reading MT5 terminal logs…",
        }
        self._progress_label.setText(labels.get(stage, stage))

    def _on_completed(self, result: ExportResult) -> None:
        self._progress.setValue(self._progress.maximum())
        self._progress_label.setText("Done.")
        mb = result.bytes_written / (1024 * 1024)
        msg = (
            f"Bundle written: {result.output_zip}\n\n"
            f"{result.files_written} entries, {mb:.1f} MB uncompressed."
        )
        if result.skipped_sources:
            msg += (
                f"\n\nSkipped {len(result.skipped_sources)} source(s) — see "
                f"manifest.txt inside the zip for details."
            )
        QMessageBox.information(self, "Diagnostics exported", msg)
        self.accept()

    def _on_failed(self, err: str) -> None:
        self._progress.setVisible(False)
        self._progress_label.setVisible(False)
        self._export_btn.setEnabled(True)
        self._buttons.button(
            QDialogButtonBox.StandardButton.Cancel
        ).setEnabled(True)
        QMessageBox.critical(self, "Export failed", err)
