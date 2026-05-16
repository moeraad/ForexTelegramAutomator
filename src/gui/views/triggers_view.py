"""Triggers tab: manage the structured phrase -> action_type mapping.

Loads triggers from channels/<stack>.json, lets the user add/edit/delete/move
entries, and writes back (which also re-renders the derived prompt fields).
"""
from __future__ import annotations

from collections import defaultdict

from PySide6.QtCore import Qt, QThread, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
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
    QTableWidget,
    QTableWidgetItem,
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
            db = self._stack.db_path
            batch_size = max(1, db_settings.get_int(db, "classifier_batch_size", 10) or 10)
            try:
                concurrency = int(
                    db_settings.get_str(db, "classifier_concurrency", "4") or "4"
                )
            except ValueError:
                concurrency = 4
            concurrency = max(1, min(16, concurrency))
            provider = ai_discovery.build_discovery_provider()

            chunks: list[list[str]] = [
                self._messages[i : i + batch_size]
                for i in range(0, len(self._messages), batch_size)
            ]
            out: list[dict] = []
            done_count = 0

            def run_chunk(chunk: list[str]) -> tuple[list[str], list]:
                try:
                    return chunk, ai_discovery.classify_batch(chunk, provider)
                except Exception as e:  # noqa: BLE001
                    from src.ai_discovery import Classification
                    return chunk, [
                        Classification("UNKNOWN", m[:60], f"batch error: {e}", 0.0)
                        for m in chunk
                    ]

            with ThreadPoolExecutor(max_workers=concurrency) as pool:
                futures = [pool.submit(run_chunk, c) for c in chunks]
                for fut in as_completed(futures):
                    chunk, clss = fut.result()
                    for msg, c in zip(chunk, clss, strict=False):
                        out.append({
                            "action_type": c.action_type,
                            "phrase": c.phrase or msg[:60],
                            "samples": [msg],
                            "note": c.reasoning if c.action_type == "UNKNOWN" else "",
                        })
                    done_count += len(chunk)
                    self.progress.emit(done_count, len(self._messages))
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
        left_layout.addWidget(QLabel("<b>Action types</b>"))
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
        layout.addWidget(splitter, 1)

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

        self._phrase = QLineEdit()
        self._phrase.setPlaceholderText("short trigger phrase (verbatim)")
        self._sample = QPlainTextEdit()
        self._sample.setPlaceholderText("optional: full sample message")
        self._note = QLineEdit()
        self._note.setPlaceholderText("optional note")

        form = QFormLayout()
        form.addRow("Action type", self._type)
        form.addRow("Phrase", self._phrase)
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
        phrase = self._phrase.text().strip()
        if not phrase:
            QMessageBox.warning(self, "Required", "Phrase can't be empty.")
            return
        sample = self._sample.toPlainText().strip()
        self.result = {
            "action_type": self._type.currentText(),
            "phrase": phrase,
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
            "<span style='color:#787b86;'>Paste one message per line "
            "(or separate by blank lines). Each message is classified via "
            "AI; results are appended as triggers. You can move "
            "misclassified entries afterward.</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)

        self._text = QPlainTextEdit()
        self._text.setPlaceholderText("paste messages here…")

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
        raw = self._text.toPlainText()
        if not raw.strip():
            return []
        if "\n\n" in raw:
            chunks = [c.strip() for c in raw.split("\n\n")]
        else:
            chunks = [c.strip() for c in raw.splitlines()]
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
