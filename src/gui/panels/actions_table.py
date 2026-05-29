"""ACTIONS panel: chip-filtered, field-aware-searchable live table.

Redesigned 2026-05-20:

  - Multi-select **chip filters** across four dimensions (Type, Status,
    Side, Range) replacing the single-choice preset dropdown.
  - **Active-filter strip** rendering the current selections below the
    bar so the operator can see what's filtering at a glance.
  - **Field-aware search** (`type:OPEN side:BUY score>=70 reason:tp`)
    via the shared `_search_parser`.
  - **Joined SQL** pulling positions data so the table can show Side,
    Score, Multiplier, Price (entry→exit), realized PnL, and Reason in
    addition to id / age / type / status.
  - **Load more** pagination — default 100 rows, +100 per click.
  - **CSV export** of the currently-filtered view.
  - **Persisted filter state** per stack via stack_state.

Same DBSubscriber-driven update path as before; the rewrite is the
view, the proxy, and the model — the polling layer is unchanged.
"""
from __future__ import annotations

import csv
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QSortFilterProxyModel, Qt, Signal
from PySide6.QtGui import QColor, QKeyEvent
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QTableView,
    QVBoxLayout,
    QWidget,
)

from src.gui.models.actions_model import ActionRow, ActionsModel, COL_TYPE
from src.gui.panels._chip import Chip
from src.gui.panels._collapsible import CollapsibleSection
from src.gui.panels._search_parser import ParsedQuery, apply as apply_search, parse as parse_search
from src.gui.services.db_subscriber import DBSubscriber
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


# Filter dimensions. Each entry is (key, label, chip_color_token).
# Color tokens map to palette attributes (success/warning/danger/accent).
_TYPE_CHIPS: list[tuple[str, str, str]] = [
    ("OPEN",          "OPEN",          "success"),
    ("OPEN_INSTANT",  "OPEN_INSTANT",  "success"),
    ("MOVE_SL_BE",    "MOVE_SL_BE",    "warning"),
    ("MOVE_SL",       "MOVE_SL",       "warning"),
    ("CLOSE_PARTIAL", "CLOSE_PARTIAL", "warning"),
    ("CLOSE_FULL",    "CLOSE_FULL",    "danger"),
    ("REOPEN_LAST",   "REOPEN_LAST",   "success"),
    ("REINFORCE",     "REINFORCE",     "success"),
    ("TIGHTEN_SL",    "TIGHTEN_SL",    "warning"),
    ("MODIFY_TPS",    "MODIFY_TPS",    "warning"),
    ("CANCEL_PENDING","CANCEL_PENDING","danger"),
    ("ATTACH_SIGNAL", "ATTACH_SIGNAL", "accent"),
    ("ALERT",         "ALERT",         "accent"),
]

_STATUS_CHIPS: list[tuple[str, str, str]] = [
    ("pending",   "pending",   "accent"),
    ("sent",      "sent",      "accent"),
    ("claimed",   "claimed",   "accent"),
    ("executed",  "executed",  "success"),
    ("watching",  "watching",  "warning"),
    ("failed",    "failed",    "danger"),
    ("rejected",  "rejected",  "danger"),
    ("cancelled", "cancelled", "danger"),
]

_SIDE_CHIPS: list[tuple[str, str, str]] = [
    ("BUY",  "↑ BUY",  "success"),
    ("SELL", "↓ SELL", "danger"),
]

# Range chips. Value is the lookback in seconds (None = all time).
_RANGE_CHIPS: list[tuple[str, str, str, int | None]] = [
    ("today", "Today", "accent", 0),       # 0 = since midnight UTC
    ("3d",    "3 days", "accent", 3 * 86400),
    ("7d",    "7 days", "accent", 7 * 86400),
    ("30d",   "30 days", "accent", 30 * 86400),
    ("all",   "All time", "accent", None),
]

_DEFAULT_LIMIT = 100
_PAGE_SIZE = 100


class _ActionsProxyModel(QSortFilterProxyModel):
    """Filter proxy driven by chip selections + parsed search.

    The chip selections form sets-per-dimension; an empty set means
    "no filter on this dimension" (everything passes). The search
    query layers on top with AND semantics across all dimensions.
    """

    def __init__(self) -> None:
        super().__init__()
        self._types: set[str] = set()
        self._statuses: set[str] = set()
        self._sides: set[str] = set()
        self._range_since: datetime | None = None
        self._search: ParsedQuery = ParsedQuery()
        # Sort by the model's UserRole values (numeric / temporal) so
        # clicking a header sorts by data type instead of by the
        # rendered display string. Without this, "100" sorts before
        # "20" and the Age column orders by short-form string text.
        self.setSortRole(Qt.ItemDataRole.UserRole)

    def set_types(self, types: set[str]) -> None:
        self._types = set(types)
        self.invalidateFilter()

    def set_statuses(self, statuses: set[str]) -> None:
        self._statuses = set(statuses)
        self.invalidateFilter()

    def set_sides(self, sides: set[str]) -> None:
        self._sides = set(sides)
        self.invalidateFilter()

    def set_range(self, lookback_seconds: int | None) -> None:
        """`None` = all time. `0` = since UTC midnight today. Otherwise
        the cutoff is `now - lookback_seconds`."""
        if lookback_seconds is None:
            self._range_since = None
        elif lookback_seconds == 0:
            now = datetime.now(timezone.utc)
            self._range_since = now.replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
        else:
            self._range_since = (
                datetime.now(timezone.utc)
                - timedelta(seconds=lookback_seconds)
            )
        self.invalidateFilter()

    def set_search(self, text: str) -> None:
        self._search = parse_search(text)
        self.invalidateFilter()

    def filterAcceptsRow(self, source_row: int, _parent) -> bool:  # type: ignore[override]
        model = self.sourceModel()
        if not isinstance(model, ActionsModel):
            return True
        action = model.action_at(source_row)
        if action is None:
            return False
        if self._types and action.action_type not in self._types:
            return False
        if self._statuses and action.status not in self._statuses:
            return False
        if self._sides:
            if action.side not in self._sides:
                return False
        if self._range_since is not None:
            dt = self._parse_created_at(action.created_at)
            if dt is None or dt < self._range_since:
                return False
        if not self._search.is_empty:
            if not apply_search(self._search, action.search_fields()):
                return False
        return True

    @staticmethod
    def _parse_created_at(s: str) -> datetime | None:
        if not s:
            return None
        try:
            iso = s.replace("Z", "+00:00").replace(" ", "T")
            if not (iso.endswith("Z") or "+" in iso[10:] or "-" in iso[10:]):
                iso = iso + "+00:00"
            return datetime.fromisoformat(iso)
        except (TypeError, ValueError):
            return None


class ActionsTable(QWidget):
    """The full Actions panel — toolbar with chip filters + search +
    row-action buttons; active-filter strip; data table; load-more
    footer. Same subscription-driven update path as the previous
    implementation."""

    selection_changed = Signal(object)

    SUBSCRIPTION_KEY = "actions_recent"

    def __init__(self, subscriber: DBSubscriber, stack: Stack | None = None) -> None:
        super().__init__()
        self._subscriber = subscriber
        self._stack = stack
        self._model = ActionsModel()
        self._proxy = _ActionsProxyModel()
        self._proxy.setSourceModel(self._model)
        self._current_action: ActionRow | None = None
        self._limit = _DEFAULT_LIMIT
        self._chip_lookup: dict[tuple[str, str], Chip] = {}
        # Suppress persistence during `_restore_filters` so chip-setup
        # signals don't write the same state back on top of itself.
        self._restoring = False
        self._build_ui()
        self._restore_filters()
        self._resubscribe()
        subscriber.query_changed.connect(self._on_query_changed)

    # ---- SQL / subscription ---------------------------------------------

    def _sql(self) -> str:
        """LEFT JOIN positions so the model can render entry / exit /
        PnL / close_reason for actions that opened a trade. Actions
        without a position get NULLs (LEFT JOIN). LIMIT comes from
        `self._limit` so 'Load more' just bumps it and re-subscribes."""
        return (
            "SELECT a.id, a.action_type, a.status, a.created_at, "
            "       a.payload_json, a.ea_response, a.source_channel_id, "
            "       p.entry_price, p.exit_price, p.realized_pnl, "
            "       p.close_reason "
            "FROM actions a "
            "LEFT JOIN positions p ON p.action_id = a.id "
            f"ORDER BY a.id DESC LIMIT {int(self._limit)}"
        )

    def _resubscribe(self) -> None:
        self._subscriber.subscribe(self.SUBSCRIPTION_KEY, self._sql())

    # ---- UI build ------------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(6)

        # Top toolbar — title + search + row actions + CSV export.
        toolbar = QHBoxLayout()
        toolbar.addWidget(QLabel("<b>ACTIONS</b>"))
        toolbar.addSpacing(10)

        self._search = QLineEdit()
        self._search.setPlaceholderText(
            "search — try type:OPEN side:BUY score>=70 reason:tp"
        )
        self._search.setMinimumWidth(280)
        self._search.textChanged.connect(self._proxy.set_search)
        self._search.textChanged.connect(lambda _t: self._persist_filters())
        toolbar.addWidget(self._search, 1)

        from src.gui._button_helpers import make_icon_button
        # Promote now → green check (semantically: approve the pending
        # action, push it through to the EA).
        self._promote_btn = make_icon_button(
            "ACCEPT",
            "Force-trigger the selected pending action (skips the auto-execute "
            "delay; EA picks it up on the next poll).",
            variant="success", fallback_text="Promote",
        )
        self._promote_btn.setEnabled(False)
        self._promote_btn.clicked.connect(self._on_promote_clicked)
        toolbar.addWidget(self._promote_btn)

        # Cancel → BROOM (clear/sweep). Replaces the previous CLOSE (×)
        # so the verb visually reads as "wipe this pending action" rather
        # than the dismiss-a-window connotation of an X.
        self._cancel_btn = make_icon_button(
            "BROOM",
            "Cancel the selected pending action before it auto-promotes.",
            variant="danger", fallback_text="Cancel",
        )
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._on_cancel_clicked)
        toolbar.addWidget(self._cancel_btn)

        # Export CSV → SHARE icon (matches the export affordance in
        # Journal + Diagnostics — same glyph used app-wide for "send
        # this data out").
        self._export_btn = make_icon_button(
            "SHARE",
            "Export the currently-visible rows (after filters + search) to CSV.",
            variant="primary", fallback_text="Export CSV",
        )
        self._export_btn.clicked.connect(self._on_export_clicked)
        toolbar.addWidget(self._export_btn)
        layout.addLayout(toolbar)

        # Chip-filter rows live inside a collapsible section so they
        # don't dominate vertical space when the operator isn't
        # actively filtering. Collapsed by default; the header carries
        # the active-filter count so a folded panel still announces
        # that filters are in effect.
        self._filters_section = CollapsibleSection("Filters", expanded=False)
        self._filters_section.toggled.connect(
            lambda _expanded: self._persist_filters()
        )
        self._filters_section.add_layout(
            self._build_chip_row("Type",   _TYPE_CHIPS,   "type")
        )
        self._filters_section.add_layout(
            self._build_chip_row("Status", _STATUS_CHIPS, "status")
        )
        self._filters_section.add_layout(
            self._build_chip_row("Side",   _SIDE_CHIPS,   "side")
        )
        self._filters_section.add_layout(self._build_range_row())
        layout.addWidget(self._filters_section)

        # Active-filter strip + "Clear all" — only visible when at least
        # one chip is checked.
        self._active_strip = QFrame()
        self._active_strip.setFrameShape(QFrame.Shape.NoFrame)
        strip_layout = QHBoxLayout(self._active_strip)
        strip_layout.setContentsMargins(0, 0, 0, 0)
        strip_layout.setSpacing(6)
        self._active_label = QLabel("Active:")
        pal = current_palette()
        self._active_label.setStyleSheet(f"color: {pal.text_muted}; font-size: 11px;")
        strip_layout.addWidget(self._active_label)
        # Container for the dynamic chip labels — repopulated whenever
        # filter state changes.
        self._active_chips_holder = QWidget()
        self._active_chips_layout = QHBoxLayout(self._active_chips_holder)
        self._active_chips_layout.setContentsMargins(0, 0, 0, 0)
        self._active_chips_layout.setSpacing(4)
        strip_layout.addWidget(self._active_chips_holder, 1)
        self._clear_all_btn = QPushButton("Clear all")
        self._clear_all_btn.clicked.connect(self._on_clear_all)
        _pal = current_palette()
        self._clear_all_btn.setStyleSheet(
            f"QPushButton {{ color: {_pal.text_muted}; font-size: 11px; "
            f"border: none; padding: 0 8px; }}"
            f"QPushButton:hover {{ color: {_pal.text}; }}"
        )
        strip_layout.addWidget(self._clear_all_btn)
        layout.addWidget(self._active_strip)
        self._active_strip.setVisible(False)

        # Table.
        self._table = QTableView()
        self._table.setModel(self._proxy)
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        # Sortable: header click toggles order. The proxy's lessThan
        # falls back to the source model's data() which is fine for
        # numeric/string string-compare across our columns.
        self._table.setSortingEnabled(True)
        try:
            from src.gui.panels._table_utils import apply_full_width_headers
            ncols = self._model.columnCount()
            content_cols = tuple(c for c in range(ncols) if c != COL_TYPE)
            apply_full_width_headers(
                self._table, content_columns=content_cols, stretch_column=COL_TYPE,
            )
        except Exception:
            pass
        self._table.selectionModel().currentRowChanged.connect(self._on_selection)
        self._table.selectionModel().selectionChanged.connect(
            self._on_selection_changed_for_repaint
        )
        self._apply_selection_style()
        try:
            from src.gui.theme import bus as _theme_bus
            _theme_bus.theme_changed.connect(
                lambda _pal: self._apply_selection_style()
            )
        except Exception:
            pass
        layout.addWidget(self._table, 1)

        # Footer with row count + "Load more".
        footer = QHBoxLayout()
        self._row_count_label = QLabel("0 rows")
        self._row_count_label.setStyleSheet(f"color: {current_palette().text_muted}; font-size: 11px;")
        footer.addWidget(self._row_count_label)
        footer.addStretch()
        self._load_more_btn = QPushButton("Load more (+100)")
        self._load_more_btn.clicked.connect(self._on_load_more)
        footer.addWidget(self._load_more_btn)
        layout.addLayout(footer)

    def _build_chip_row(
        self,
        title: str,
        chips: list[tuple[str, str, str]],
        dimension: str,
    ) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(4)
        label = QLabel(title)
        label.setStyleSheet(
            f"color: {current_palette().text_muted}; font-size: 11px; font-weight: 700; "
            "min-width: 55px;"
        )
        row.addWidget(label)
        for key, label_text, color in chips:
            chip = Chip(label_text, value=key, color_token=color)
            chip.toggled_with_value.connect(
                lambda v, c, d=dimension: self._on_chip_toggled(d, v, c)
            )
            self._chip_lookup[(dimension, key)] = chip
            row.addWidget(chip)
        row.addStretch()
        return row

    def _build_range_row(self) -> QHBoxLayout:
        """Range is single-select even though it uses chips — only one
        time range makes sense at a time."""
        row = QHBoxLayout()
        row.setSpacing(4)
        label = QLabel("Range")
        label.setStyleSheet(
            f"color: {current_palette().text_muted}; font-size: 11px; font-weight: 700; "
            "min-width: 55px;"
        )
        row.addWidget(label)
        for key, label_text, color, lookback in _RANGE_CHIPS:
            chip = Chip(label_text, value=key, color_token=color)
            chip.toggled_with_value.connect(self._on_range_chip_toggled)
            self._chip_lookup[("range", key)] = chip
            row.addWidget(chip)
        row.addStretch()
        return row

    # ---- Chip handlers -------------------------------------------------

    def _on_chip_toggled(self, dimension: str, value: object, checked: bool) -> None:
        # Recompute the dimension's full set from chip states (rather
        # than tracking incremental deltas) so reset / clear-all stay
        # consistent without bespoke add/remove logic per dimension.
        current = self._active_values(dimension)
        if dimension == "type":
            self._proxy.set_types(current)
        elif dimension == "status":
            self._proxy.set_statuses(current)
        elif dimension == "side":
            self._proxy.set_sides(current)
        self._refresh_active_strip()
        self._persist_filters()

    def _on_range_chip_toggled(self, value: object, checked: bool) -> None:
        # Single-select: when one is checked, uncheck the others.
        if checked:
            for (dim, key), chip in self._chip_lookup.items():
                if dim == "range" and key != value and chip.isChecked():
                    chip.set_checked(False)
        # Find the matching lookback.
        if not checked:
            self._proxy.set_range(None)
        else:
            lookback = next(
                (lb for k, _l, _c, lb in _RANGE_CHIPS if k == value), None,
            )
            self._proxy.set_range(lookback)
        self._refresh_active_strip()
        self._persist_filters()

    def _active_values(self, dimension: str) -> set[str]:
        return {
            key for (dim, key), chip in self._chip_lookup.items()
            if dim == dimension and chip.isChecked()
        }

    def _on_clear_all(self) -> None:
        for chip in self._chip_lookup.values():
            chip.set_checked(False)
        self._search.clear()
        self._proxy.set_types(set())
        self._proxy.set_statuses(set())
        self._proxy.set_sides(set())
        self._proxy.set_range(None)
        self._refresh_active_strip()
        self._persist_filters()

    def _refresh_active_strip(self) -> None:
        # Clear the holder.
        while self._active_chips_layout.count():
            item = self._active_chips_layout.takeAt(0)
            w = item.widget()
            if w is not None:
                w.deleteLater()
        any_active = False
        active_count = 0
        for (dim, key), chip in self._chip_lookup.items():
            if not chip.isChecked():
                continue
            any_active = True
            active_count += 1
            text = f"{dim}:{key}"
            tag = QLabel(text)
            # Text over a faint accent tint: use the palette body color so it
            # stays legible in BOTH themes (the 15%-alpha tint reads as the
            # themed background, so hardcoded light-grey text vanished on light).
            _pal = current_palette()
            tag.setStyleSheet(
                f"color: {_pal.text}; font-size: 10px; font-weight: 600; "
                "padding: 2px 8px; border-radius: 8px; "
                "background: rgba(91,141,239,0.15);"
            )
            self._active_chips_layout.addWidget(tag)
        self._active_chips_layout.addStretch()
        self._active_strip.setVisible(any_active)
        # Echo the active-filter count into the collapsible header so a
        # folded Filters section still announces that filters are in
        # effect. Without this, an operator who collapses Filters and
        # comes back later might not realize chips are still on.
        if active_count > 0:
            badge = f"({active_count} active)"
        else:
            badge = ""
        try:
            self._filters_section.set_summary(badge)
        except AttributeError:
            # _filters_section is built later in some construction
            # orders during early init; safe to skip.
            pass
        self._update_row_count_label()

    # ---- Subscription handlers -----------------------------------------

    def _on_query_changed(self, key: str, rows: list[sqlite3.Row]) -> None:
        if key != self.SUBSCRIPTION_KEY:
            return
        self._model.set_rows(rows)
        if self._proxy.rowCount() > 0 and not self._table.selectionModel().hasSelection():
            self._table.selectRow(0)
        self._update_row_count_label()

    def _update_row_count_label(self) -> None:
        visible = self._proxy.rowCount()
        total = self._model.rowCount()
        if visible == total:
            self._row_count_label.setText(f"{visible} rows (limit {self._limit})")
        else:
            self._row_count_label.setText(
                f"{visible} of {total} rows shown (limit {self._limit})"
            )

    def _on_load_more(self) -> None:
        """Bump the SQL limit by a page and re-subscribe so the next
        tick fills in older rows. Selection is preserved because the
        new rowset is a superset of the previous one (LIMIT + DESC)."""
        self._limit += _PAGE_SIZE
        self._resubscribe()
        # Force an immediate refresh tick — the subscriber's hash will
        # change because the result set grew, so this just speeds up
        # the visual update.
        self._update_row_count_label()

    # ---- Row actions ---------------------------------------------------

    def _on_selection(self, current, _previous) -> None:
        if not current.isValid():
            self._current_action = None
            self._update_button_state()
            self.selection_changed.emit(None)
            return
        source_index = self._proxy.mapToSource(current)
        action = self._model.action_at(source_index.row())
        self._current_action = action
        self._update_button_state()
        self.selection_changed.emit(action)

    def _update_button_state(self) -> None:
        action = self._current_action
        is_pending = (
            self._stack is not None
            and action is not None
            and action.status == "pending"
        )
        self._promote_btn.setEnabled(bool(is_pending))
        self._cancel_btn.setEnabled(bool(is_pending))

    def _on_promote_clicked(self) -> None:
        action = self._current_action
        if action is None or self._stack is None:
            return
        if action.status != "pending":
            QMessageBox.information(
                self, "Promote",
                f"Action #{action.id} is {action.status}; only pending "
                "actions can be promoted.",
            )
            return
        self._apply_action_status_change(
            action.id,
            "UPDATE actions SET status='sent' WHERE id=? AND status='pending'",
            success_msg=f"Promoted action #{action.id} to sent.",
            noop_msg=f"Action #{action.id} is no longer pending.",
        )

    def _on_cancel_clicked(self) -> None:
        action = self._current_action
        if action is None or self._stack is None:
            return
        if action.status != "pending":
            QMessageBox.information(
                self, "Cancel",
                f"Action #{action.id} is {action.status}; only pending "
                "actions can be cancelled.",
            )
            return
        self._apply_action_status_change(
            action.id,
            "UPDATE actions SET status='cancelled' WHERE id=? AND status='pending'",
            success_msg=f"Cancelled action #{action.id}.",
            noop_msg=f"Action #{action.id} is no longer pending.",
        )

    def _apply_action_status_change(
        self, action_id: int, sql: str, *, success_msg: str, noop_msg: str,
    ) -> None:
        assert self._stack is not None
        try:
            conn = sqlite3.connect(str(self._stack.db_path))
            try:
                cur = conn.execute(sql, (action_id,))
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as e:
            QMessageBox.warning(self, "DB error", f"Action #{action_id}: {e}")
            return
        QMessageBox.information(
            self, "OK", success_msg if cur.rowcount else noop_msg,
        )

    # ---- CSV export ----------------------------------------------------

    def _on_export_clicked(self) -> None:
        """Export the currently-visible (post-filter, post-search) rows
        to CSV. Iterates the proxy so saved filters round-trip."""
        n = self._proxy.rowCount()
        if n == 0:
            QMessageBox.information(self, "Export", "No rows to export.")
            return
        default = f"copytrades_actions_{self._stack.name if self._stack else 'export'}.csv"
        path_str, _ = QFileDialog.getSaveFileName(
            self, "Export actions CSV", default, "CSV (*.csv)",
        )
        if not path_str:
            return
        rows: list[ActionRow] = []
        for i in range(n):
            proxy_idx = self._proxy.index(i, 0)
            src_idx = self._proxy.mapToSource(proxy_idx)
            action = self._model.action_at(src_idx.row())
            if action is not None:
                rows.append(action)
        try:
            with Path(path_str).open("w", newline="", encoding="utf-8") as f:
                w = csv.writer(f)
                w.writerow([
                    "id", "created_at", "action_type", "side", "status",
                    "score", "multiplier", "entry_price", "exit_price",
                    "realized_pnl", "close_reason", "ea_response",
                ])
                for r in rows:
                    w.writerow([
                        r.id, r.created_at, r.action_type, r.side, r.status,
                        r.quality_score, r.multiplier,
                        r.entry_price, r.exit_price,
                        r.realized_pnl, r.close_reason, r.ea_response,
                    ])
        except OSError as e:
            QMessageBox.critical(self, "Export failed", str(e))
            return
        QMessageBox.information(
            self, "Export", f"Exported {len(rows)} rows to {path_str}",
        )

    # ---- Theming / selection -------------------------------------------

    def _apply_selection_style(self) -> None:
        from src.gui.theme import current_palette
        pal = current_palette()
        accent = QColor(pal.accent)
        bg_rgba = f"rgba({accent.red()}, {accent.green()}, {accent.blue()}, 56)"
        self._table.setStyleSheet(
            "QTableView { selection-color: " + pal.text + "; "
            "selection-background-color: " + bg_rgba + "; }"
            "QTableView::item:selected { color: " + pal.text + "; "
            "background-color: " + bg_rgba + "; }"
        )

    def _on_selection_changed_for_repaint(self, *_args) -> None:
        sel = self._table.selectionModel().selectedRows()
        source_rows: set[int] = set()
        for proxy_idx in sel:
            src_idx = self._proxy.mapToSource(proxy_idx)
            if src_idx.isValid():
                source_rows.add(src_idx.row())
        self._model.set_selected_rows(source_rows)

    # ---- Filter persistence --------------------------------------------

    def _persist_filters(self) -> None:
        """Write current filter selections (chip states + search +
        section-expanded flag) to the AppState dict under this stack's
        name. No-ops while `_restoring` is True so the initial restore
        doesn't ping-pong the file."""
        if self._restoring or self._stack is None:
            return
        from dataclasses import replace
        from src.gui.services.stack_state import load_state, save_state
        try:
            state = load_state()
            af = dict(state.actions_filters or {})
            af[self._stack.name] = {
                "types":    sorted(self._active_values("type")),
                "statuses": sorted(self._active_values("status")),
                "sides":    sorted(self._active_values("side")),
                "range":    next(
                    iter(self._active_values("range")), None
                ),
                "search":   self._search.text(),
                "expanded": self._filters_section.is_expanded(),
            }
            save_state(replace(state, actions_filters=af))
        except Exception:
            # Persistence is best-effort; never break the UI on a
            # write failure (read-only filesystem, locked file, etc.).
            pass

    def _restore_filters(self) -> None:
        """Re-apply saved filter selections to the chips / search /
        section. Called once after _build_ui — chip toggle signals
        during this would re-write the same state, so we gate them
        behind self._restoring."""
        if self._stack is None:
            return
        from src.gui.services.stack_state import load_state
        try:
            state = load_state()
            saved = (state.actions_filters or {}).get(self._stack.name)
        except Exception:
            saved = None
        if not isinstance(saved, dict):
            return
        self._restoring = True
        try:
            for dim, key in (
                ("type", "types"),
                ("status", "statuses"),
                ("side", "sides"),
            ):
                values = saved.get(key) or []
                if not isinstance(values, list):
                    continue
                for v in values:
                    chip = self._chip_lookup.get((dim, str(v)))
                    if chip is not None:
                        chip.set_checked(True)
            range_value = saved.get("range")
            if isinstance(range_value, str):
                chip = self._chip_lookup.get(("range", range_value))
                if chip is not None:
                    chip.set_checked(True)
                    # Apply the range lookback directly because the
                    # toggle signal was suppressed.
                    lookback = next(
                        (lb for k, _l, _c, lb in _RANGE_CHIPS if k == range_value),
                        None,
                    )
                    self._proxy.set_range(lookback)
            text = saved.get("search")
            if isinstance(text, str):
                self._search.setText(text)
            expanded = saved.get("expanded")
            if isinstance(expanded, bool):
                self._filters_section.set_expanded(expanded)
            # Now that all chips are restored, push the dimension sets
            # into the proxy once each — cheaper than relying on the
            # toggle-by-toggle pathway that we suppressed.
            self._proxy.set_types(self._active_values("type"))
            self._proxy.set_statuses(self._active_values("status"))
            self._proxy.set_sides(self._active_values("side"))
            self._refresh_active_strip()
        finally:
            self._restoring = False

    def refresh_ages(self) -> None:
        self._model.refresh_ages()

    def keyPressEvent(self, event: QKeyEvent) -> None:  # type: ignore[override]
        key = event.key()
        if key == Qt.Key.Key_J:
            self._move_selection(+1)
            return
        if key == Qt.Key.Key_K:
            self._move_selection(-1)
            return
        if key == Qt.Key.Key_Slash:
            self._search.setFocus()
            self._search.selectAll()
            return
        if key == Qt.Key.Key_Escape:
            if self._search.hasFocus() and self._search.text():
                self._search.clear()
            return
        super().keyPressEvent(event)

    def _move_selection(self, delta: int) -> None:
        if self._proxy.rowCount() == 0:
            return
        current_idx = self._table.currentIndex()
        new_row = (current_idx.row() if current_idx.isValid() else -1) + delta
        new_row = max(0, min(self._proxy.rowCount() - 1, new_row))
        self._table.selectRow(new_row)
