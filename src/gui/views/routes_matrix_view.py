"""Routes Matrix: channel × destination grid for v2 routing topology.

Step 16 of the multi-channel plan. The matrix is THE operator-friendly way
to see and edit the routing graph: rows are Channels, columns are
Destinations, cell = exists-and-enabled Route (with editable
sizing_multiplier).

Why a matrix and not a list:
  - Mirror topology (one channel → N destinations) reads at-a-glance as
    a row with multiple cells filled.
  - Aggregate topology (N channels → one destination) reads as a
    column with multiple cells filled.
  - Mesh + 1:1 are visually obvious.
  - The text-based Routes tab in ``v2_config_view`` requires the operator
    to mentally cross-reference rows; the matrix shows the topology
    directly.

Mutations are pure transforms in ``config_v2`` (``with_route_added``,
``with_route_removed``, ``with_route_sizing``) — this view is the UI
on top of them. Save flushes to ``stacks_config.json``; the API/listener
processes pick up the change via mtime-cached ``load_v2`` on the next
message (no restart needed).

What's NOT here (deferred):
  - Per-cell halt toggle. Halt lives on the Channels/Routes tabs in
    V2 Config (Step 15). Adding a third element per cell would crowd the
    grid; operators wanting per-route halt jump to V2 Config → Routes.
  - BotBinding hookup at route creation. The matrix only manages the
    Routes table; bindings stay in the v2 config view.
  - Export / sort / filter. Plan flags these as nice-to-haves; not
    needed for the operator-rare config-edit flow.
"""
from __future__ import annotations

from typing import Any

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src import config_v2
from src.config_v2 import ConfigV2
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


class RoutesMatrixView(QWidget):
    """Channel × destination matrix backed by ``stacks_config.json``."""

    cfg_changed = Signal()

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._cfg: ConfigV2 | None = None
        # When True, cell signals fired by ``refresh()`` must be ignored —
        # they're echoes of the operator's previous change, not new intent.
        self._suspend_signals = False
        self._build_ui()
        self.refresh()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self.refresh()

    # ---- UI scaffolding ---------------------------------------------------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        header = QHBoxLayout()
        title = QLabel(
            "<span style='font-size:16px; font-weight:700;'>ROUTES MATRIX</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        _tm = current_palette().text_muted
        hint = QLabel(
            f"<span style='color:{_tm};'>rows = channels  ·  "
            "columns = destinations  ·  check cell = create route  ·  "
            "edit number = sizing multiplier (1.0 = 1:1 lot size)</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(hint)
        header.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload routes matrix")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color: {current_palette().text_muted};")
        layout.addWidget(self._summary)

        self._table = QTableWidget()
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        self._table.verticalHeader().setVisible(True)
        self._table.setAlternatingRowColors(True)
        # Each cell embeds an _RouteCell (multi-line widget). Default row
        # height clips it — bump to ~2x so the sizing + Rules row both
        # show without cropping.
        self._table.verticalHeader().setDefaultSectionSize(72)
        # Columns stay at their natural ~160px width (Interactive, drag-
        # resizable). NOT Stretch — the matrix shouldn't span the whole
        # viewport on wide screens; trailing whitespace is intentional.
        self._table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Interactive,
        )
        self._table.horizontalHeader().setStretchLastSection(False)
        layout.addWidget(self._table, 1)

    # ---- Data loading -----------------------------------------------------

    def refresh(self) -> None:
        cfg_path = config_v2.config_path()
        cfg = None
        if config_v2.is_v2(cfg_path):
            try:
                cfg = config_v2.load_v2(cfg_path)
            except Exception as e:  # noqa: BLE001
                self._summary.setText(
                    f"<span style='color:#ef5350;'>"
                    f"Failed to load v2 config: {e}</span>"
                )
        if cfg is None:
            self._cfg = None
            self._summary.setText(
                "<span style='color:#ff9800;'>No v2 config found at "
                f"{cfg_path}. Open Settings to migrate.</span>"
            )
            self._table.clear()
            self._table.setRowCount(0)
            self._table.setColumnCount(0)
            return
        self._cfg = cfg
        _tm2 = current_palette().text_muted
        self._summary.setText(
            f"<span style='color:{_tm2};'>"
            f"{len(cfg.channels)} channel(s) × "
            f"{len(cfg.destinations)} destination(s) = "
            f"{len(cfg.routes)}/{len(cfg.channels) * len(cfg.destinations)} "
            f"cells filled  ·  {cfg_path}"
            "</span>"
        )
        self._populate_grid(cfg)

    def _populate_grid(self, cfg: ConfigV2) -> None:
        self._suspend_signals = True
        try:
            channels = list(cfg.channels)
            destinations = list(cfg.destinations)
            self._table.clear()
            self._table.setRowCount(len(channels))
            self._table.setColumnCount(len(destinations))
            self._table.setHorizontalHeaderLabels([
                f"{d.name}\n({d.id})" for d in destinations
            ])
            self._table.setVerticalHeaderLabels([
                f"{c.name}\n({c.id})" for c in channels
            ])
            # Index routes by (channel_id, destination_id) → Route
            route_lookup = {
                (r.channel_id, r.destination_id): r for r in cfg.routes
            }
            for row, ch in enumerate(channels):
                for col, dest in enumerate(destinations):
                    existing = route_lookup.get((ch.id, dest.id))
                    cell = _RouteCell(
                        channel_id=ch.id,
                        destination_id=dest.id,
                        route=existing,
                        on_check=self._on_cell_checked,
                        on_sizing=self._on_cell_sizing_changed,
                        on_edit_rules=self._on_cell_edit_rules,
                    )
                    self._table.setCellWidget(row, col, cell)
            for c in range(self._table.columnCount()):
                self._table.setColumnWidth(c, 160)
        finally:
            self._suspend_signals = False

    # ---- Mutations --------------------------------------------------------

    def _on_cell_checked(
        self, channel_id: str, destination_id: str, checked: bool,
    ) -> None:
        if self._suspend_signals or self._cfg is None:
            return
        existing = next(
            (r for r in self._cfg.routes
             if r.channel_id == channel_id and r.destination_id == destination_id),
            None,
        )
        try:
            if checked and existing is None:
                new_cfg = config_v2.with_route_added(
                    self._cfg,
                    channel_id=channel_id, destination_id=destination_id,
                )
            elif not checked and existing is not None:
                new_cfg = config_v2.with_route_removed(self._cfg, existing.id)
            else:
                return  # already in the requested state
            config_v2.save_v2(new_cfg)
            self._cfg = new_cfg
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            self.refresh()  # fall back to full rebuild on error
            return
        # Don't refresh() on every tick — destroying the cell widget the
        # user just clicked causes a transient focus/tooltip flash on
        # Windows. The cell already self-updates (spinbox enabled state +
        # rules button visibility via _handle_check). Only update the
        # summary counter so it stays in sync with the new route count.
        self._update_summary()
        self.cfg_changed.emit()

    def _update_summary(self) -> None:
        if self._cfg is None:
            return
        cfg = self._cfg
        from src import config_v2 as _cv2
        cfg_path = _cv2.config_path()
        _tm3 = current_palette().text_muted
        self._summary.setText(
            f"<span style='color:{_tm3};'>"
            f"{len(cfg.channels)} channel(s) × "
            f"{len(cfg.destinations)} destination(s) = "
            f"{len(cfg.routes)}/{len(cfg.channels) * len(cfg.destinations)} "
            f"cells filled  ·  {cfg_path}"
            "</span>"
        )

    def _on_cell_sizing_changed(
        self, channel_id: str, destination_id: str, value: float,
    ) -> None:
        if self._suspend_signals or self._cfg is None:
            return
        existing = next(
            (r for r in self._cfg.routes
             if r.channel_id == channel_id and r.destination_id == destination_id),
            None,
        )
        if existing is None:
            return  # cell isn't checked; spinbox should be disabled
        # Avoid redundant disk writes when the value didn't actually change
        # (the spinbox emits valueChanged on programmatic set during refresh
        # too, but _suspend_signals already handles the bulk case; this
        # guards against floating-point round-trip noise).
        if abs(existing.sizing_multiplier - value) < 1e-9:
            return
        try:
            new_cfg = config_v2.with_route_sizing(
                self._cfg, existing.id, value,
            )
            config_v2.save_v2(new_cfg)
            self._cfg = new_cfg
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            self.refresh()

    def _on_cell_edit_rules(
        self, channel_id: str, destination_id: str,
    ) -> None:
        """Open the Step-20/21 rules editor for the route at this cell."""
        if self._cfg is None:
            return
        existing = next(
            (r for r in self._cfg.routes
             if r.channel_id == channel_id and r.destination_id == destination_id),
            None,
        )
        if existing is None:
            return  # cell isn't checked
        dlg = _EditRouteRulesDialog(
            self._cfg, existing.id, parent=self,
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            new_cfg = dlg.apply(self._cfg)
            config_v2.save_v2(new_cfg)
            self._cfg = new_cfg
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
        self.refresh()
        self.cfg_changed.emit()


class _RouteCell(QWidget):
    """Single matrix cell: checkbox + sizing-multiplier spinbox.

    Layout: ☑  [1.00 ▲▼]
    Sizing spinbox is disabled (and shows the route's current multiplier
    OR the default 1.0 when the cell is unchecked) until the cell is
    checked.
    """

    def __init__(
        self,
        *,
        channel_id: str,
        destination_id: str,
        route: Any | None,
        on_check,
        on_sizing,
        on_edit_rules=None,
    ) -> None:
        super().__init__()
        self._channel_id = channel_id
        self._destination_id = destination_id
        self._on_check = on_check
        self._on_sizing = on_sizing
        self._on_edit_rules = on_edit_rules

        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(6)

        self._check = QCheckBox()
        self._check.setChecked(route is not None)
        self._check.stateChanged.connect(self._handle_check)
        layout.addWidget(self._check)

        self._sizing = QDoubleSpinBox()
        self._sizing.setRange(0.0, 100.0)
        self._sizing.setSingleStep(0.05)
        self._sizing.setDecimals(2)
        self._sizing.setValue(
            float(route.sizing_multiplier) if route is not None else 1.0,
        )
        self._sizing.setEnabled(route is not None)
        self._sizing.setSuffix("×")
        self._sizing.setToolTip(
            "Sizing multiplier applied to MT5 lot size on this route. "
            "1.0 = 1:1 with signal. 0.5 = half. 2.0 = double.",
        )
        self._sizing.valueChanged.connect(self._handle_sizing)
        layout.addWidget(self._sizing)

        # Step 20/21: per-cell "edit rules" button. Visible only when the
        # route exists (no rules to edit on an empty cell). Tooltip
        # signals which rules are non-default — operator can see at a
        # glance which routes have customised behavior.
        self._rules_btn = QPushButton("⚙")
        self._rules_btn.setMaximumWidth(28)
        self._rules_btn.setVisible(route is not None and on_edit_rules is not None)
        self._rules_btn.setToolTip(_rules_tooltip(route))
        self._rules_btn.clicked.connect(self._handle_edit_rules)
        layout.addWidget(self._rules_btn)

    def _handle_check(self, state: int) -> None:
        checked = state == Qt.CheckState.Checked.value
        self._sizing.setEnabled(checked)
        # The rules button is meaningless on an unchecked cell; hide it.
        if hasattr(self, "_rules_btn"):
            self._rules_btn.setVisible(checked and self._on_edit_rules is not None)
        self._on_check(self._channel_id, self._destination_id, checked)

    def _handle_edit_rules(self) -> None:
        if self._on_edit_rules is None:
            return
        self._on_edit_rules(self._channel_id, self._destination_id)

    def _handle_sizing(self, value: float) -> None:
        if not self._check.isChecked():
            return
        self._on_sizing(self._channel_id, self._destination_id, value)


# ---- Edit Rules dialog (Step 20 + 21) ------------------------------------


# Action types the orchestrator can persist (matches schema.sql CHECK).
# Kept in sync manually — adding a new action type to the schema means
# adding it here so it shows up in the per-route allowlist editor.
_ACTION_TYPES: tuple[str, ...] = (
    "OPEN", "OPEN_INSTANT", "ATTACH_SIGNAL", "CANCEL_PENDING",
    "MODIFY", "MODIFY_TPS",
    "CLOSE", "CLOSE_PARTIAL", "CLOSE_FULL", "CLOSE_ALL",
    "MOVE_SL", "MOVE_SL_BE", "TIGHTEN_SL",
    "REOPEN_LAST", "REINFORCE",
    "ALERT",
)


def _rules_tooltip(route) -> str:
    """One-line summary of which rules are non-default for the cell tooltip."""
    if route is None:
        return ""
    bits: list[str] = []
    if route.max_lots > 0:
        bits.append(f"max_lots={route.max_lots:g}")
    if route.min_account_balance > 0:
        bits.append(f"min_balance=${route.min_account_balance:g}")
    if route.skip_if_drawdown_pct > 0:
        bits.append(f"skip_dd>{route.skip_if_drawdown_pct:g}%")
    if route.allowed_action_types:
        bits.append(f"types={','.join(route.allowed_action_types)}")
    if route.time_of_day_filter:
        bits.append(f"window={route.time_of_day_filter}")
    if route.fallback_destination_id:
        bits.append(f"fallback={route.fallback_destination_id}")
    if not bits:
        return "Edit per-route rules (none set)"
    return "Edit per-route rules:\n  • " + "\n  • ".join(bits)


class _EditRouteRulesDialog(QDialog):
    """Edit Step-20 (sizing rules) + Step-21 (failover) for one Route.

    All fields use the "0 / empty = disabled" convention. The save path
    routes through ``with_route_rules`` + ``with_route_failover``, so
    the same validation that protects ``stacks_config.json`` hand-edits
    also protects GUI edits.
    """

    def __init__(
        self,
        cfg: "ConfigV2",
        route_id: str,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        route = cfg.route(route_id)
        if route is None:
            raise ValueError(f"Unknown route: {route_id}")
        self._route_id = route_id

        self.setWindowTitle(f"Edit rules — {route_id}")
        self.setMinimumWidth(520)

        outer = QVBoxLayout(self)
        _tm4 = current_palette().text_muted
        header = QLabel(
            f"<span style='font-weight:600;'>{route_id}</span>  "
            f"<span style='color:{_tm4};'>"
            f"{route.channel_id} → {route.destination_id}</span>"
        )
        header.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(header)
        hint = QLabel(
            f"<span style='color:{_tm4};font-size:11px;'>"
            "0 / empty = disabled. EA-side rules (max_lots, balance, "
            "drawdown) propagate via OPEN payload — see Decision Log."
            "</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        hint.setWordWrap(True)
        outer.addWidget(hint)

        form = QFormLayout()
        outer.addLayout(form)

        # max_lots
        self._max_lots = QDoubleSpinBox()
        self._max_lots.setRange(0.0, 1000.0)
        self._max_lots.setDecimals(2)
        self._max_lots.setSingleStep(0.05)
        self._max_lots.setValue(float(route.max_lots))
        self._max_lots.setSuffix(" lots")
        form.addRow("Max lots cap:", self._max_lots)

        # min_account_balance
        self._min_balance = QDoubleSpinBox()
        self._min_balance.setRange(0.0, 10_000_000.0)
        self._min_balance.setDecimals(2)
        self._min_balance.setSingleStep(50.0)
        self._min_balance.setValue(float(route.min_account_balance))
        self._min_balance.setPrefix("$ ")
        form.addRow("Min account balance:", self._min_balance)

        # skip_if_drawdown_pct
        self._max_drawdown = QDoubleSpinBox()
        self._max_drawdown.setRange(0.0, 100.0)
        self._max_drawdown.setDecimals(1)
        self._max_drawdown.setSingleStep(1.0)
        self._max_drawdown.setValue(float(route.skip_if_drawdown_pct))
        self._max_drawdown.setSuffix(" %")
        form.addRow("Skip if drawdown >", self._max_drawdown)

        # allowed_action_types — checkbox list
        self._action_list = QListWidget()
        self._action_list.setSelectionMode(
            QListWidget.SelectionMode.NoSelection,
        )
        for at in _ACTION_TYPES:
            item = QListWidgetItem(at)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = at in route.allowed_action_types
            item.setCheckState(
                Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked,
            )
            self._action_list.addItem(item)
        self._action_list.setMaximumHeight(160)
        form.addRow("Allowed action types\n(none checked = ALL):", self._action_list)

        # time_of_day_filter
        self._time_window = QLineEdit()
        self._time_window.setText(route.time_of_day_filter)
        self._time_window.setPlaceholderText("e.g. 08:00-20:00 (UTC, blank = always)")
        form.addRow("Time-of-day filter:", self._time_window)

        # fallback_destination_id (Step 21)
        self._fallback = QComboBox()
        self._fallback.addItem("(none)", "")
        for d in cfg.destinations:
            if d.id == route.destination_id:
                continue  # circular — silently omit
            self._fallback.addItem(f"{d.id}  ({d.name})", d.id)
        # Preselect current value if present.
        current_idx = self._fallback.findData(route.fallback_destination_id)
        if current_idx >= 0:
            self._fallback.setCurrentIndex(current_idx)
        form.addRow("Failover destination:", self._fallback)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _checked_action_types(self) -> tuple[str, ...]:
        out: list[str] = []
        for i in range(self._action_list.count()):
            item = self._action_list.item(i)
            if item.checkState() == Qt.CheckState.Checked:
                out.append(item.text())
        # All-checked is semantically the same as none-checked (no filter).
        # Collapsing to () avoids saving a redundant "every type" allowlist
        # that the operator can't tell apart from "I haven't decided yet."
        if len(out) == len(_ACTION_TYPES):
            return ()
        return tuple(out)

    def apply(self, cfg: "ConfigV2") -> "ConfigV2":
        """Apply both Step-20 and Step-21 transforms in one save."""
        new_cfg = config_v2.with_route_rules(
            cfg, self._route_id,
            max_lots=float(self._max_lots.value()),
            min_account_balance=float(self._min_balance.value()),
            skip_if_drawdown_pct=float(self._max_drawdown.value()),
            allowed_action_types=self._checked_action_types(),
            time_of_day_filter=self._time_window.text().strip(),
        )
        new_cfg = config_v2.with_route_failover(
            new_cfg, self._route_id,
            self._fallback.currentData() or "",
        )
        return new_cfg
