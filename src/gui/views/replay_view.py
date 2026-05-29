"""REPLAY view: rerun historical messages through current AI, flag drift."""
from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import QAbstractTableModel, QModelIndex, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSpinBox,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.services.replay import HistMessage, ReplayRow, ReplayRunner, load_messages
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


_RANGES: list[tuple[str, int | None]] = [
    ("Today", 0),
    ("Last 3 days", 3),
    ("Last 7 days", 7),
    ("Last 30 days", 30),
]

_AI_OPTIONS: list[tuple[str, str, str]] = [
    ("Default (.env)", "", ""),
    ("Anthropic — Sonnet 4.6", "anthropic", "claude-sonnet-4-6"),
    ("Anthropic — Opus 4.7",   "anthropic", "claude-opus-4-7"),
    ("Anthropic — Haiku 4.5",  "anthropic", "claude-haiku-4-5-20251001"),
    ("OpenAI — gpt-5",       "openai", "gpt-5"),
    ("OpenAI — gpt-5-mini",  "openai", "gpt-5-mini"),
    ("OpenAI — gpt-5-nano",  "openai", "gpt-5-nano"),
]

_DRIFT_COLOR = {
    "none":     QColor("#787b86"),
    "type":     QColor("#ef5350"),
    "side":     QColor("#ef5350"),
    "count":    QColor("#ff7043"),
    "decision": QColor("#ff9800"),
}


def _hex_to_accent(color: str) -> str:
    if not color:
        return ""
    c = color.lower()
    if c in ("#26a69a", "#859900", "#00e676", "#00897b"):
        return "success"
    if c in ("#ef5350", "#dc322f", "#ff5252", "#d32f2f"):
        return "danger"
    if c in ("#ff9800", "#b58900", "#ffd740", "#f57c00"):
        return "warning"
    if c in ("#2962ff", "#268bd2", "#448aff", "#1976d2"):
        return "accent"
    return ""


def _summarize_original(actions: list) -> str:
    if not actions:
        return "(none)"
    parts: list[str] = []
    for a in actions:
        side = getattr(a, "side", "") or ""
        parts.append(f"{a.action_type}{' ' + side if side else ''}")
    return " · ".join(parts)


def _summarize_replayed(actions: list) -> str:
    if not actions:
        return "(none)"
    parts: list[str] = []
    for a in actions:
        side = a.side or ""
        parts.append(f"{a.action_type}{' ' + side if side else ''}")
    return " · ".join(parts)


class _ReplayModel(QAbstractTableModel):
    HEADERS = (
        "When", "Msg", "Channel", "Text", "Original", "Replayed", "Drift",
    )

    def __init__(self) -> None:
        super().__init__()
        self._rows: list[ReplayRow] = []

    def clear(self) -> None:
        self.beginResetModel()
        self._rows = []
        self.endResetModel()

    def append(self, row: ReplayRow) -> None:
        n = len(self._rows)
        self.beginInsertRows(QModelIndex(), n, n)
        self._rows.append(row)
        self.endInsertRows()

    def row_at(self, idx: int) -> ReplayRow | None:
        if 0 <= idx < len(self._rows):
            return self._rows[idx]
        return None

    def rowCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return 0 if parent.isValid() else len(self._rows)

    def columnCount(self, parent: QModelIndex = QModelIndex()) -> int:
        return len(self.HEADERS)

    def headerData(self, section: int, orientation: Qt.Orientation, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if role != Qt.ItemDataRole.DisplayRole:
            return None
        return self.HEADERS[section] if orientation == Qt.Orientation.Horizontal else section + 1

    def data(self, index: QModelIndex, role: int = Qt.ItemDataRole.DisplayRole) -> Any:
        if not index.isValid():
            return None
        r = self._rows[index.row()]
        col = index.column()
        if role == Qt.ItemDataRole.DisplayRole:
            if col == 0:
                return r.msg.received_at[:19].replace("T", " ")
            if col == 1:
                return str(r.msg.id)
            if col == 2:
                from src.gui.models.actions_model import _channel_display_name
                if not r.msg.source_channel_id:
                    return "—"
                return _channel_display_name(r.msg.source_channel_id)
            if col == 3:
                t = r.msg.text.replace("\n", " ").strip()
                return (t[:80] + "…") if len(t) > 80 else t
            if col == 4:
                return _summarize_original(r.original)
            if col == 5:
                if r.error:
                    return f"(error: {r.error[:40]})"
                if r.triage_decision == "ignore":
                    return "(triage=ignore)"
                return _summarize_replayed(r.replayed)
            if col == 6:
                return r.drift
        if role == Qt.ItemDataRole.ForegroundRole and col == 6:
            return _DRIFT_COLOR.get(r.drift, QColor("#787b86"))
        if role == Qt.ItemDataRole.ToolTipRole and col == 3:
            return r.msg.text
        return None


class _DetailDialog(QDialog):
    def __init__(self, parent: QWidget, row: ReplayRow) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Replay detail — msg #{row.msg.id}")
        self.resize(900, 640)
        layout = QVBoxLayout(self)
        info = QLabel(
            f"<b>received_at:</b> {row.msg.received_at}  ·  "
            f"<b>sender:</b> {row.msg.sender or 'unknown'}  ·  "
            f"<b>drift:</b> {row.drift}  ·  <b>triage:</b> {row.triage_decision}  ·  "
            f"<b>est. cost:</b> ${row.cost_estimate:.4f}"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setWordWrap(True)
        layout.addWidget(info)

        layout.addWidget(QLabel("Source message"))
        src = QPlainTextEdit(row.msg.text)
        src.setReadOnly(True)
        src.setMaximumHeight(120)
        layout.addWidget(src)

        body = QHBoxLayout()
        left = QVBoxLayout()
        left.addWidget(QLabel("Original (production)"))
        orig_panel = QPlainTextEdit()
        orig_panel.setReadOnly(True)
        lines: list[str] = []
        for a in row.original:
            lines.append(f"#{a.id}  {a.action_type}  status={a.status}")
            lines.append(json.dumps(a.payload, indent=2, ensure_ascii=False))
            lines.append("")
        orig_panel.setPlainText("\n".join(lines) if lines else "(no actions inserted)")
        left.addWidget(orig_panel)
        body.addLayout(left)

        right = QVBoxLayout()
        right.addWidget(QLabel("Replayed (current AI)"))
        new_panel = QPlainTextEdit()
        new_panel.setReadOnly(True)
        if row.error:
            new_panel.setPlainText(f"ERROR:\n{row.error}")
        else:
            text = row.raw_actions_json or "(no actions)"
            if row.raw_reasoning:
                text += "\n\nREASONING\n" + row.raw_reasoning
            new_panel.setPlainText(text)
        right.addWidget(new_panel)
        body.addLayout(right)
        layout.addLayout(body, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.button(QDialogButtonBox.StandardButton.Close).clicked.connect(self.accept)
        layout.addWidget(buttons)


class ReplayView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._runner: ReplayRunner | None = None
        self._model = _ReplayModel()
        self._planned_messages: list[HistMessage] = []
        self._stat_row_layout: QHBoxLayout | None = None
        self._stat_boxes: dict[str, QWidget] = {}
        self._cum_cost = 0.0
        self._cum_drift = 0
        self._build_ui()
        self._update_estimate()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        if self._runner is not None and self._runner.isRunning():
            self._runner.cancel()
        self._reset()
        self._update_estimate()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        top = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>REPLAY</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        from src.gui.panels._a11y import mark_heading
        mark_heading(title, "Replay")
        top.addWidget(title)
        _tm = current_palette().text_muted
        hint = QLabel(
            f"<span style='color:{_tm};'>rerun historical messages through current AI  ·  "
            "no DB writes  ·  costs real tokens</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        top.addWidget(hint)
        top.addStretch()
        layout.addLayout(top)

        # Ribbon directly under the title — full-width, same visual
        # language as Settings / Risk / Profile / etc. Built before the
        # controls row so the references can be wired up before any
        # logic that touches them. Two groups: "Run" (Start/Cancel/Clear)
        # and "Fixture" (save selected row to the management replay set).
        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        ribbon = RibbonBar([
            RibbonGroup("Run", [
                RibbonAction("PLAY", "Start", "Start replay",
                             variant="success", callback=self._on_run),
                RibbonAction("BROOM", "Cancel", "Cancel in-flight replay",
                             variant="danger", callback=self._on_cancel),
                RibbonAction("DELETE", "Clear", "Clear replay results",
                             callback=self._reset),
            ]),
            RibbonGroup("Fixture", [
                RibbonAction("PIN", "Save",
                             "Append the selected replay row to "
                             "fixtures/management_messages.jsonl. "
                             "Captures message + current DB state + "
                             "the action types the AI returned.",
                             variant="primary",
                             callback=self._on_save_fixture),
            ]),
        ])
        layout.addWidget(ribbon)
        # Recover button refs so existing enable/disable logic keeps
        # working. ribbon.buttons() returns them in declaration order.
        btns = ribbon.buttons()
        self._run_btn, self._cancel_btn, self._clear_btn, self._save_fixture_btn = btns
        self._cancel_btn.setEnabled(False)
        self._save_fixture_btn.setEnabled(False)

        controls = QHBoxLayout()
        controls.addWidget(QLabel("Range:"))
        self._range_combo = QComboBox()
        for label, days in _RANGES:
            self._range_combo.addItem(label, days)
        self._range_combo.setCurrentIndex(1)
        self._range_combo.currentIndexChanged.connect(self._on_range_changed)
        controls.addWidget(self._range_combo)

        controls.addWidget(QLabel("Limit:"))
        self._limit = QSpinBox()
        self._limit.setRange(1, 1000)
        self._limit.setValue(50)
        self._limit.valueChanged.connect(self._update_estimate)
        controls.addWidget(self._limit)

        controls.addWidget(QLabel("AI:"))
        self._ai_combo = QComboBox()
        for label, provider, model in _AI_OPTIONS:
            self._ai_combo.addItem(label, (provider, model))
        self._ai_combo.setMinimumWidth(220)
        controls.addWidget(self._ai_combo)

        self._estimate_lbl = QLabel("")
        self._estimate_lbl.setStyleSheet(f"color: {current_palette().text_muted}; padding-left: 12px;")
        controls.addWidget(self._estimate_lbl)
        controls.addStretch()
        layout.addLayout(controls)

        from src.gui.panels._stat_card import StatCard
        self._stat_row_layout = QHBoxLayout()
        self._stat_row_layout.setSpacing(8)
        for key in ("processed", "drift", "cost", "rate"):
            card = StatCard(label=key, value="—")
            self._stat_boxes[key] = card
            self._stat_row_layout.addWidget(card)
        self._stat_row_layout.addStretch()
        layout.addLayout(self._stat_row_layout)

        self._progress = QProgressBar()
        self._progress.setMaximum(1)
        layout.addWidget(self._progress)

        self._table = QTableView()
        self._table.setModel(self._model)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        apply_full_width_headers(
            self._table,
            content_columns=tuple(range(self._model.columnCount() - 1)),
        )
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.doubleClicked.connect(self._on_double_clicked)
        self._table.selectionModel().currentRowChanged.connect(
            lambda cur, _prev: self._save_fixture_btn.setEnabled(cur.isValid())
        )
        layout.addWidget(self._table, 1)

    def _on_range_changed(self, _idx: int) -> None:
        self._update_estimate()

    def _update_estimate(self) -> None:
        days = self._range_combo.currentData()
        limit = self._limit.value()
        all_msgs = load_messages(self._stack.db_path, days=days, limit=limit)
        self._planned_messages = all_msgs
        rough = len(all_msgs) * 0.005
        self._estimate_lbl.setText(
            f"plan: {len(all_msgs)} message(s)  ·  est cost ~${rough:.2f}"
        )

    def _on_run(self) -> None:
        if not self._planned_messages:
            QMessageBox.information(self, "Nothing to replay", "No messages in this range.")
            return
        if self._runner is not None and self._runner.isRunning():
            return
        confirm = QMessageBox.question(
            self, "Run replay",
            f"This will run the AI on {len(self._planned_messages)} message(s) "
            "and cost real tokens. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        self._reset()
        provider, model = self._ai_combo.currentData()
        self._progress.setMaximum(len(self._planned_messages))
        self._progress.setValue(0)
        self._runner = ReplayRunner(
            self._stack, self._planned_messages, provider, model,
        )
        self._runner.progress.connect(self._on_progress)
        self._runner.row_ready.connect(self._on_row)
        self._runner.completed.connect(self._on_completed)
        self._runner.failed_with.connect(self._on_failed)
        self._runner.finished.connect(self._on_thread_finished)
        self._run_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        from src.gui.services.thread_registry import register
        register(self._runner, stop_fn=self._runner.cancel)
        self._runner.start()

    def _on_cancel(self) -> None:
        if self._runner is not None:
            self._runner.cancel()
        self._cancel_btn.setEnabled(False)

    def _reset(self) -> None:
        self._model.clear()
        self._cum_cost = 0.0
        self._cum_drift = 0
        self._progress.setValue(0)
        self._progress.setMaximum(1)
        self._replace_box("processed", "Processed", "0")
        self._replace_box("drift", "Drift", "0")
        self._replace_box("cost", "Cost (est)", "$0.00")
        self._replace_box("rate", "Drift rate", "0%")

    def _on_progress(self, done: int, total: int) -> None:
        self._progress.setMaximum(max(1, total))
        self._progress.setValue(done)
        rate = (self._cum_drift / done * 100) if done > 0 else 0.0
        self._replace_box("processed", "Processed", f"{done}/{total}")
        self._replace_box("rate", "Drift rate", f"{rate:.0f}%")

    def _on_row(self, row: ReplayRow) -> None:
        self._model.append(row)
        if row.drift != "none":
            self._cum_drift += 1
        self._cum_cost += row.cost_estimate
        self._replace_box(
            "drift", "Drift", str(self._cum_drift),
            "#ef5350" if self._cum_drift else "#d1d4dc",
        )
        self._replace_box("cost", "Cost (est)", f"${self._cum_cost:.4f}")

    def _on_completed(self, drifted: int) -> None:
        QMessageBox.information(
            self, "Replay complete",
            f"Drift: {drifted} / {self._model.rowCount()}  ·  "
            f"Est cost: ${self._cum_cost:.4f}",
        )

    def _on_failed(self, err: str) -> None:
        QMessageBox.critical(self, "Replay failed", err)

    def _on_thread_finished(self) -> None:
        self._run_btn.setEnabled(True)
        self._cancel_btn.setEnabled(False)
        self._runner = None

    def _on_double_clicked(self, index: QModelIndex) -> None:
        row = self._model.row_at(index.row())
        if row is None:
            return
        _DetailDialog(self, row).exec()

    def _on_save_fixture(self) -> None:
        from pathlib import Path
        from PySide6.QtWidgets import QInputDialog
        idx = self._table.currentIndex()
        if not idx.isValid():
            return
        row = self._model.row_at(idx.row())
        if row is None:
            return
        fid, ok = QInputDialog.getText(
            self, "Fixture id",
            "Short kebab-case id (e.g. 'reinforce-after-close'):",
        )
        if not ok or not fid.strip():
            return
        notes, ok = QInputDialog.getText(
            self, "Notes", "One-line note explaining the case:",
        )
        if not ok:
            return
        state = self._snapshot_fixture_state()
        fixture = {
            "id": fid.strip(),
            "message": row.msg.text,
            "state": state,
            "expected_action_types": [a.action_type for a in row.replayed],
            "expected_category": "signal" if row.replayed else "context",
            "notes": notes.strip(),
        }
        fixtures_path = (
            Path(__file__).resolve().parents[3] / "fixtures" / "management_messages.jsonl"
        )
        try:
            fixtures_path.parent.mkdir(parents=True, exist_ok=True)
            with fixtures_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(fixture, ensure_ascii=False) + "\n")
        except OSError as e:
            QMessageBox.warning(self, "Save failed", f"Could not write fixture: {e}")
            return
        QMessageBox.information(
            self, "Saved",
            f"Appended fixture '{fid.strip()}' to {fixtures_path.name}. "
            "Re-run tests/test_management_replay.py to include it.",
        )

    def _snapshot_fixture_state(self) -> dict:
        import sqlite3
        out: dict = {"open_position": None, "last_closed": None, "market": None}
        try:
            conn = sqlite3.connect(str(self._stack.db_path))
            conn.row_factory = sqlite3.Row
            try:
                op = conn.execute(
                    "SELECT side, entry_price, volume, original_volume, "
                    "       partial_close_count, sl, tp, sl_moved_at "
                    "FROM positions WHERE status='open' "
                    "ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if op is not None:
                    out["open_position"] = {
                        "side": op["side"],
                        "entry_price": op["entry_price"],
                        "volume": op["volume"],
                        "original_volume": op["original_volume"],
                        "partials_taken": op["partial_close_count"] or 0,
                        "sl": op["sl"],
                        "tp": op["tp"],
                        "sl_at_be": (
                            op["sl"] is not None
                            and op["entry_price"] is not None
                            and abs(op["sl"] - op["entry_price"]) < 0.5
                        ),
                        "sl_moved": op["sl_moved_at"] is not None,
                    }
                lc = conn.execute(
                    "SELECT p.side, p.entry_price, p.volume, p.sl, p.tp, "
                    "       p.closed_at, a.payload_json "
                    "FROM positions p "
                    "LEFT JOIN actions a ON a.id = p.action_id "
                    "WHERE p.status='closed' "
                    "ORDER BY p.id DESC LIMIT 1"
                ).fetchone()
                if lc is not None:
                    out["last_closed"] = {
                        "side": lc["side"],
                        "entry_price": lc["entry_price"],
                        "sl": lc["sl"],
                        "tp": lc["tp"],
                        "closed_at": lc["closed_at"],
                    }
                bid_row = conn.execute(
                    "SELECT value FROM settings WHERE key='market_XAUUSD_bid'"
                ).fetchone()
                ask_row = conn.execute(
                    "SELECT value FROM settings WHERE key='market_XAUUSD_ask'"
                ).fetchone()
                if bid_row and ask_row:
                    try:
                        out["market"] = {
                            "bid": float(bid_row[0]),
                            "ask": float(ask_row[0]),
                        }
                    except (TypeError, ValueError):
                        pass
            finally:
                conn.close()
        except sqlite3.Error:
            pass
        return out

    def _replace_box(self, key: str, label: str, value: str, color: str = "#d1d4dc") -> None:
        assert self._stat_row_layout is not None
        card = self._stat_boxes[key]
        card.set_value(value, _hex_to_accent(color))
        if hasattr(card, "_label") and label:
            card._label.setText(label.upper())
