"""Bot Bindings view (Step 17 of multi-channel plan).

Operator-facing UI for the four bot binding scopes that Step 14 wired
into the dispatcher: global / destination / channel / route.

Layout:
  - Top: per-bot summary tree
      Bot A
        ↳ scope=global
        ↳ scope=destination dest_x
      Bot B
        ↳ scope=channel ch_a
  - Right: overlap warnings (when two bots both DM about the same destination)
  - Below: Add Binding... and Remove Binding buttons

Operator workflows enabled by this view:
  - "I want this bot to DM about every event everywhere"
    → Add Binding, scope=global.
  - "I want a dedicated alert bot for one chatty channel"
    → Add Binding, scope=channel, channel_id=<that channel>.
  - "I want a paranoid bot watching just the live destination's leg of a mirror"
    → Add Binding, scope=route, route_id=<live leg>.

All mutations go through pure transforms in ``config_v2`` so they're
testable without Qt and easy to reason about.
"""
from __future__ import annotations

from typing import Callable

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from src import config_v2
from src.config_v2 import ConfigV2
from src.gui.services.stack_registry import Stack
from src.gui.theme import current_palette


_SCOPES = ("global", "destination", "channel", "route")


class BotBindingsView(QWidget):
    """Per-bot binding inspector + add/remove editor."""

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._cfg: ConfigV2 | None = None
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
            "<span style='font-size:16px; font-weight:700;'>BOT BINDINGS</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        _tm = current_palette().text_muted
        hint = QLabel(
            f"<span style='color:{_tm};'>which bots DM about which events  ·  "
            "scope=global/destination/channel/route</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(hint)
        header.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload bindings")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        # Ribbon sits below the section title (per the operator's spec)
        # and hosts the add/remove actions. Refresh stays inline next to
        # the title as a small icon-only affordance (already there).
        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        ribbon = RibbonBar([
            RibbonGroup("Bindings", [
                RibbonAction("ADD", "Add", "Open the add-binding dialog",
                             variant="success", callback=self._open_add_dialog),
                RibbonAction("DELETE", "Remove", "Remove the selected binding(s)",
                             variant="danger", callback=self._remove_selected),
            ]),
        ])
        layout.addWidget(ribbon)

        self._summary = QLabel("")
        self._summary.setStyleSheet(f"color: {current_palette().text_muted};")
        layout.addWidget(self._summary)

        self._warnings = QLabel("")
        self._warnings.setTextFormat(Qt.TextFormat.RichText)
        self._warnings.setWordWrap(True)
        layout.addWidget(self._warnings)

        self._tree = QTreeWidget()
        self._tree.setHeaderLabels(["bot / binding", "scope", "target"])
        self._tree.setAlternatingRowColors(True)
        from PySide6.QtWidgets import QHeaderView as _HV
        # Each column drag-resizable; `target` (last col) stretches so
        # the tree fills the viewport width without right-hand whitespace.
        hdr = self._tree.header()
        hdr.setSectionResizeMode(0, _HV.ResizeMode.Interactive)
        hdr.setSectionResizeMode(1, _HV.ResizeMode.Interactive)
        hdr.setSectionResizeMode(2, _HV.ResizeMode.Stretch)
        hdr.setStretchLastSection(True)
        self._tree.setColumnWidth(0, 280)
        self._tree.setColumnWidth(1, 120)
        layout.addWidget(self._tree, 1)

        # Add / Remove moved to the top ribbon above. Nothing here.

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
            self._warnings.setText("")
            self._tree.clear()
            return
        self._cfg = cfg
        _tm2 = current_palette().text_muted
        self._summary.setText(
            f"<span style='color:{_tm2};'>"
            f"{len(cfg.bots)} bot(s) · "
            f"{len(cfg.bot_bindings)} binding(s) · {cfg_path}"
            "</span>"
        )
        self._render_tree(cfg)
        self._render_warnings(cfg)

    def _render_tree(self, cfg: ConfigV2) -> None:
        self._tree.clear()
        # One top-level row per bot. Bots with no bindings show a child
        # placeholder so the operator can spot them.
        bindings_by_bot: dict[str, list] = {b.id: [] for b in cfg.bots}
        for binding in cfg.bot_bindings:
            bindings_by_bot.setdefault(binding.bot_id, []).append(binding)
        for bot in cfg.bots:
            bindings = bindings_by_bot.get(bot.id, [])
            top = QTreeWidgetItem([
                f"🤖 {bot.name}  ({bot.id})",
                f"{len(bindings)} binding(s)",
                bot.token_setting_key,
            ])
            self._tree.addTopLevelItem(top)
            if not bindings:
                top.addChild(QTreeWidgetItem(["(no bindings)", "", ""]))
            for binding in bindings:
                target = (
                    binding.destination_id
                    or binding.channel_id
                    or binding.route_id
                    or "(none)"
                )
                child = QTreeWidgetItem([
                    f"  ↳ {binding.id}",
                    binding.scope,
                    target,
                ])
                # Store the binding id on the item so _remove_selected can
                # round-trip it without re-parsing the label.
                child.setData(0, Qt.ItemDataRole.UserRole, binding.id)
                top.addChild(child)
            top.setExpanded(True)
        # Also surface any orphan bindings (bot_id no longer in config).
        unknown = [
            b for b in cfg.bot_bindings
            if b.bot_id not in {bot.id for bot in cfg.bots}
        ]
        if unknown:
            top = QTreeWidgetItem([
                "⚠ orphan bindings", f"{len(unknown)}",
                "bot_id not in config — clean up",
            ])
            self._tree.addTopLevelItem(top)
            for binding in unknown:
                child = QTreeWidgetItem([
                    f"  ↳ {binding.id}", binding.scope, binding.bot_id,
                ])
                child.setData(0, Qt.ItemDataRole.UserRole, binding.id)
                top.addChild(child)

    def _render_warnings(self, cfg: ConfigV2) -> None:
        overlaps = config_v2.detect_binding_overlaps(cfg)
        if not overlaps:
            self._warnings.setText(
                "<span style='color:#26a69a;'>"
                "✓ No overlapping bindings detected.</span>"
            )
            return
        rows = "<br>".join(
            f"&nbsp;&nbsp;• <b>{dest_id}</b>: both <code>{a}</code> "
            f"and <code>{b}</code> will DM"
            for (dest_id, a, b) in overlaps
        )
        self._warnings.setText(
            f"<span style='color:#ff9800;'>"
            f"⚠ {len(overlaps)} overlap(s) — these destinations are "
            f"covered by multiple distinct bots (each fires its own DM):"
            f"<br>{rows}"
            "</span>"
        )

    # ---- Mutations --------------------------------------------------------

    def _open_add_dialog(self) -> None:
        if self._cfg is None:
            QMessageBox.information(
                self, "Add Binding",
                "v2 config not loaded.",
            )
            return
        if not self._cfg.bots:
            QMessageBox.warning(
                self, "Add Binding",
                "No bots configured. Create a Bot in V2 Config first.",
            )
            return
        dlg = _AddBindingDialog(self._cfg, parent=self)
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            new_cfg = dlg.apply(self._cfg)
            config_v2.save_v2(new_cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.refresh()

    def _remove_selected(self) -> None:
        if self._cfg is None:
            return
        item = self._tree.currentItem()
        if item is None:
            QMessageBox.information(
                self, "Remove Binding",
                "Select an indented binding row (the lines starting "
                "with ↳) and click Remove Selected.\n\n"
                "Bot header rows (🤖) are containers — select the "
                "binding child row underneath instead.",
            )
            return
        binding_id = item.data(0, Qt.ItemDataRole.UserRole)
        if not binding_id:
            QMessageBox.information(
                self, "Remove Binding",
                "That's a bot header row. Click the indented '↳' "
                "binding row underneath it, then Remove Selected.",
            )
            return
        reply = QMessageBox.question(
            self, "Remove Binding",
            f"Remove binding '{binding_id}'?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if reply != QMessageBox.StandardButton.Yes:
            return
        try:
            new_cfg = config_v2.with_binding_removed(self._cfg, binding_id)
            config_v2.save_v2(new_cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.refresh()


# ---- Add Binding dialog ---------------------------------------------------


class _AddBindingDialog(QDialog):
    """Pick a bot + scope; the right "target" combo is enabled based on scope."""

    def __init__(self, cfg: ConfigV2, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle("Add Binding")
        self.setMinimumWidth(420)

        form = QFormLayout(self)
        self._bot = QComboBox()
        for b in cfg.bots:
            self._bot.addItem(f"{b.id}  ({b.name})", b.id)
        form.addRow("Bot:", self._bot)

        self._scope = QComboBox()
        for s in _SCOPES:
            self._scope.addItem(s, s)
        self._scope.currentIndexChanged.connect(self._on_scope_changed)
        form.addRow("Scope:", self._scope)

        # Three target combos — only one is enabled at a time based on scope.
        self._destination = QComboBox()
        for d in cfg.destinations:
            self._destination.addItem(f"{d.id}  ({d.name})", d.id)
        form.addRow("Destination:", self._destination)

        self._channel = QComboBox()
        for c in cfg.channels:
            self._channel.addItem(f"{c.id}  ({c.name})", c.id)
        form.addRow("Channel:", self._channel)

        self._route = QComboBox()
        for r in cfg.routes:
            self._route.addItem(
                f"{r.id}  ({r.channel_id}→{r.destination_id})", r.id,
            )
        form.addRow("Route:", self._route)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self.accept)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

        # Initialize the target-enabledness for the default scope.
        self._on_scope_changed()

    def _on_scope_changed(self) -> None:
        scope = self._scope.currentData()
        self._destination.setEnabled(scope == "destination")
        self._channel.setEnabled(scope == "channel")
        self._route.setEnabled(scope == "route")

    def apply(self, cfg: ConfigV2) -> ConfigV2:
        scope = self._scope.currentData()
        kwargs: dict = {}
        if scope == "destination":
            kwargs["destination_id"] = self._destination.currentData()
        elif scope == "channel":
            kwargs["channel_id"] = self._channel.currentData()
        elif scope == "route":
            kwargs["route_id"] = self._route.currentData()
        return config_v2.with_binding_added(
            cfg, bot_id=self._bot.currentData(), scope=scope, **kwargs,
        )
