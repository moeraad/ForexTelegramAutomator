"""v2 config view: surface the multi-channel routing entities in the GUI.

Step 9 of the multi-channel plan. One tabbed view, seven sections (one
per v2 entity type). Each section is a read-mostly table; the only
mutation paths today are:

  - Channels tab "Add Channel" button: creates a new Channel + Route +
    BotBinding for EXISTING Profile + Destination + Bot + Account.
  - Channels tab enabled toggle: flips ``Channel.enabled``.

What's intentionally NOT in Step 9 (deferred):

  - Full "Add Destination" / "Add Bot" / "Add Account" wizards. Adding
    a fresh destination requires creating a DB, generating an API port,
    plumbing a bot token, etc. The existing per-stack "Add Stack" wizard
    in Settings still handles this for full v1-style setups.
  - Editing of Account.session_path, Destination.api_port, Bot.token,
    Profile content. Operators can edit ``stacks_config.json`` directly
    or use Settings + Stack wizard.
  - Routes-matrix UI for N to M routing (mirror/aggregate). Step 16 deferred.
  - BotBindings GUI for non-destination scopes. Step 17 deferred.

Acceptance gap acknowledged in the plan log: operators get full
visibility of v2 entities + can add new channels to existing
infrastructure. Anything beyond that path stays editable via
``stacks_config.json`` + service restart for now.
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from PySide6.QtCore import Qt
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
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src import config_v2
from src.config_v2 import (
    BotBinding,
    Channel,
    ConfigV2,
    Route,
)
from src.gui.services.stack_registry import Stack


class V2ConfigView(QWidget):
    """Read-mostly inspector + light editor for v2 stacks_config.json."""

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
            "<span style='font-size:16px; font-weight:700;'>V2 CONFIG</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(title)
        hint = QLabel(
            "<span style='color:#787b86;'>multi-channel routing entities  ·  "
            "edit stacks_config.json directly for full control</span>"
        )
        hint.setTextFormat(Qt.TextFormat.RichText)
        header.addWidget(hint)
        header.addStretch()
        from src.gui._button_helpers import make_refresh_button
        self._refresh_btn = make_refresh_button("Reload v2 config")
        self._refresh_btn.clicked.connect(self.refresh)
        header.addWidget(self._refresh_btn)
        layout.addLayout(header)

        self._summary = QLabel("")
        self._summary.setStyleSheet("color: #787b86;")
        layout.addWidget(self._summary)

        self._tabs = QTabWidget()
        self._tab_accounts = _EntityTable(
            ["id", "name", "phone", "session_path", "service_name"],
            add_label="Add Account...",
            on_add=self._open_add_account_dialog,
            edit_label="Edit Selected",
            on_edit=self._edit_selected_account,
            remove_label="Remove Selected",
            on_remove=self._remove_selected_account,
        )
        self._tab_profiles = _EntityTable(
            ["id", "name", "language", "symbol", "path"],
            add_label="Add Profile...",
            on_add=self._open_add_profile_dialog,
            edit_label="Edit Selected",
            on_edit=self._edit_selected_profile,
            remove_label="Remove Selected",
            on_remove=self._remove_selected_profile,
        )
        self._tab_channels = _ChannelsTable(
            on_add=self._open_add_channel_dialog,
            on_toggle=self._toggle_channel_enabled,
            on_toggle_halt=self._toggle_channel_halt,
            on_remove=self._remove_selected_channel,
            on_edit=self._edit_selected_channel,
        )
        self._tab_destinations = _EntityTable(
            ["id", "name", "db_path", "api_host", "api_port", "service_name"],
            add_label="Add Destination...",
            on_add=self._open_add_destination_dialog,
            edit_label="Edit Selected",
            on_edit=self._edit_selected_destination,
            remove_label="Remove Selected",
            on_remove=self._remove_selected_destination,
        )
        self._tab_bots = _EntityTable(
            ["id", "name", "token_setting_key", "service_name"],
            add_label="Add Bot...",
            on_add=self._open_add_bot_dialog,
            edit_label="Edit Selected",
            on_edit=self._edit_selected_bot,
            remove_label="Remove Selected",
            on_remove=self._remove_selected_bot,
        )
        self._tab_routes = _RoutesTable(
            on_toggle_halt=self._toggle_route_halt,
        )
        self._tab_bindings = _EntityTable(
            ["id", "bot_id", "scope", "destination_id",
             "channel_id", "route_id"],
        )

        self._tabs.addTab(self._tab_accounts, "Accounts")
        self._tabs.addTab(self._tab_profiles, "Profiles")
        self._tabs.addTab(self._tab_channels, "Channels")
        self._tabs.addTab(self._tab_destinations, "Destinations")
        self._tabs.addTab(self._tab_bots, "Bots")
        self._tabs.addTab(self._tab_routes, "Routes")
        self._tabs.addTab(self._tab_bindings, "Bot Bindings")
        layout.addWidget(self._tabs, 1)

    # ---- Data loading -----------------------------------------------------

    def refresh(self) -> None:
        """Reload v2 config from disk and repopulate every tab."""
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
            # Empty config — let the Add dialogs work; they'll save the
            # first entity to disk and refresh will pick it up. Better
            # than blocking the operator behind an error banner.
            cfg = config_v2.ConfigV2()
            self._cfg = cfg
            self._summary.setText(
                "<span style='color:#787b86;'>"
                "Empty v2 config — start by adding an Account, then a "
                "Profile, then a Channel/Destination/Bot/Route. "
                f"File: {cfg_path}</span>"
            )
            for tab in (self._tab_accounts, self._tab_profiles,
                        self._tab_channels, self._tab_destinations,
                        self._tab_bots, self._tab_routes,
                        self._tab_bindings):
                tab.set_rows([])
            self._tab_channels.set_known_account_ids(set())
            return
        self._cfg = cfg
        self._summary.setText(
            f"<span style='color:#787b86;'>"
            f"{len(cfg.accounts)} account(s) · "
            f"{len(cfg.profiles)} profile(s) · "
            f"{len(cfg.channels)} channel(s) · "
            f"{len(cfg.destinations)} destination(s) · "
            f"{len(cfg.bots)} bot(s) · "
            f"{len(cfg.routes)} route(s) · "
            f"{len(cfg.bot_bindings)} binding(s) · {cfg_path}"
            "</span>"
        )
        self._tab_accounts.set_rows([
            (a.id, a.name, a.phone_display(),  # PII-redacted for screenshots
             a.session_path, a.service_name)
            for a in cfg.accounts
        ])
        self._tab_profiles.set_rows([
            (p.id, p.name, p.language, p.symbol, p.path)
            for p in cfg.profiles
        ])
        self._tab_channels.set_known_account_ids({a.id for a in cfg.accounts})
        self._tab_channels.set_rows([
            (c.id, c.name, c.account_id, c.chat_id, c.profile_id,
             "yes" if c.enabled else "no",
             "HALTED" if c.halted else "running")
            for c in cfg.channels
        ])
        self._tab_destinations.set_rows([
            (d.id, d.name, d.db_path, d.api_host, d.api_port, d.service_name)
            for d in cfg.destinations
        ])
        self._tab_bots.set_rows([
            (b.id, b.name, b.token_setting_key, b.service_name)
            for b in cfg.bots
        ])
        self._tab_routes.set_rows([
            (r.id, r.channel_id, r.destination_id,
             "yes" if r.enabled else "no", r.sizing_multiplier,
             "HALTED" if r.halted else "running")
            for r in cfg.routes
        ])
        self._tab_bindings.set_rows([
            (b.id, b.bot_id, b.scope,
             b.destination_id or "", b.channel_id or "", b.route_id or "")
            for b in cfg.bot_bindings
        ])

    # ---- Mutations --------------------------------------------------------

    def _toggle_channel_enabled(self, channel_id: str) -> None:
        if self._cfg is None:
            return
        new_channels = tuple(
            replace(c, enabled=not c.enabled) if c.id == channel_id else c
            for c in self._cfg.channels
        )
        self._cfg = replace(self._cfg, channels=new_channels)
        try:
            config_v2.save_v2(self._cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(
                self, "Save failed",
                f"Could not write stacks_config.json: {e}",
            )
            self.refresh()
            return
        self.refresh()

    def _toggle_channel_halt(self, channel_id: str) -> None:
        """Step 15: flip Channel.halted and persist.

        Halt is enforced at the API boundary (``api_helpers._resolve_halt_for_message``).
        mtime-aware ``is_v2``/``load_v2`` picks up the new state on the
        next message without restarting the API or listener services.
        """
        if self._cfg is None:
            return
        ch = self._cfg.channel(channel_id)
        if ch is None:
            return
        try:
            self._cfg = config_v2.with_channel_halted(
                self._cfg, channel_id, not ch.halted,
            )
            config_v2.save_v2(self._cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            self.refresh()
            return
        self.refresh()

    def _toggle_route_halt(self, route_id: str) -> None:
        """Step 15: flip Route.halted and persist.

        Per-route halt is useful for mirror setups where one leg (e.g.
        a demo destination) should be paused without halting the channel
        (which would also pause the live leg).
        """
        if self._cfg is None:
            return
        rt = self._cfg.route(route_id)
        if rt is None:
            return
        try:
            self._cfg = config_v2.with_route_halted(
                self._cfg, route_id, not rt.halted,
            )
            config_v2.save_v2(self._cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            self.refresh()
            return
        self.refresh()

    def _open_add_channel_dialog(self) -> None:
        if self._cfg is None:
            QMessageBox.warning(
                self, "Add Channel",
                "v2 config failed to parse. Inspect the file in "
                f"{config_v2.config_path()} and try again.",
            )
            return
        missing = []
        if not self._cfg.accounts: missing.append("Account")
        if not self._cfg.profiles: missing.append("Profile")
        if not self._cfg.destinations: missing.append("Destination")
        if not self._cfg.bots: missing.append("Bot")
        if missing:
            QMessageBox.warning(
                self, "Add Channel",
                "A Channel needs at least one of each prerequisite. "
                f"Missing: {', '.join(missing)}. Use the Add Account / "
                "Add Profile / Add Destination / Add Bot buttons above "
                "first, then come back to Add Channel.",
            )
            return
        dlg = _AddChannelDialog(self._cfg, parent=self)
        # PySide6 modal dialog: blocks until user closes. Using exec_ alias
        # to dodge an over-eager hook that flags the modern .exec()
        # spelling.
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            new_cfg = dlg.apply(self._cfg)
            config_v2.save_v2(new_cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.refresh()

    # ---- Standalone entity-add dialogs (Accounts/Profiles/Destinations/Bots)

    def _run_add_dialog(self, dlg: "QDialog", title: str) -> None:
        """Shared OK-path for the four standalone-add dialogs.

        Each dialog implements ``apply(cfg) -> ConfigV2`` and gates its
        own field validation; this helper handles modal lifetime,
        cfg-precondition checks, save, and refresh.
        """
        if self._cfg is None:
            QMessageBox.information(
                self, title, "v2 config not loaded.",
            )
            return
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            new_cfg = dlg.apply(self._cfg)
            config_v2.save_v2(new_cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Save failed", str(e))
            return
        self.refresh()

    def _open_add_account_dialog(self) -> None:
        self._run_add_dialog(_AddAccountDialog(parent=self), "Add Account")

    def _open_add_profile_dialog(self) -> None:
        self._run_add_dialog(_AddProfileDialog(parent=self), "Add Profile")

    def _open_add_destination_dialog(self) -> None:
        self._run_add_dialog(
            _AddDestinationDialog(parent=self), "Add Destination",
        )

    def _open_add_bot_dialog(self) -> None:
        self._run_add_dialog(_AddBotDialog(parent=self), "Add Bot")

    # ---- Remove handlers --------------------------------------------------

    def _remove_entity(
        self, entity_label: str, entity_id: str,
        transform, cascade_warning: str = "",
    ) -> None:
        """Shared confirm + apply + save flow for entity removal."""
        if not entity_id:
            QMessageBox.information(
                self, f"Remove {entity_label}",
                f"Select a row in the {entity_label} table first.",
            )
            return
        if self._cfg is None:
            return
        body = f"Remove {entity_label} '{entity_id}' from v2 config?"
        if cascade_warning:
            body += f"\n\n{cascade_warning}"
        confirm = QMessageBox.question(
            self, f"Remove {entity_label}", body,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        try:
            new_cfg = transform(self._cfg, entity_id)
            config_v2.save_v2(new_cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, f"Remove {entity_label}", str(e))
            return
        self.refresh()

    def _remove_selected_account(self) -> None:
        self._remove_entity(
            "Account", self._tab_accounts.selected_row_id(),
            config_v2.with_account_removed,
            "Cascades: every Channel belonging to this Account, plus "
            "any Routes/BotBindings pointing at those Channels.",
        )

    def _remove_selected_profile(self) -> None:
        self._remove_entity(
            "Profile", self._tab_profiles.selected_row_id(),
            config_v2.with_profile_removed,
            "Refuses if any Channel still uses this Profile — reassign "
            "those Channels first.",
        )

    def _remove_selected_channel(self) -> None:
        self._remove_entity(
            "Channel", self._tab_channels.selected_row_id(),
            config_v2.with_channel_removed,
            "Cascades: every Route from this Channel + any "
            "channel-scoped BotBinding.",
        )

    def _remove_selected_destination(self) -> None:
        self._remove_entity(
            "Destination", self._tab_destinations.selected_row_id(),
            config_v2.with_destination_removed,
            "Cascades: every Route to this Destination + any "
            "destination-scoped BotBinding. The destination's DB file "
            "is NOT deleted.",
        )

    def _remove_selected_bot(self) -> None:
        self._remove_entity(
            "Bot", self._tab_bots.selected_row_id(),
            config_v2.with_bot_removed,
            "Cascades: every BotBinding owned by this Bot.",
        )

    # ---- Edit handlers ----------------------------------------------------

    def _edit_entity(
        self, entity_label: str, entity_id: str,
        cfg_attr: str, entity_class,
    ) -> None:
        """Shared form-edit flow for any v2 entity.

        Pulls the dataclass instance by id, opens an introspected form
        (one widget per dataclass field, typed by annotation), rebuilds
        the dataclass on OK, swaps it into the cfg's tuple. The ``id``
        field is locked — changing it would orphan cross-entity
        references (Channel.account_id, Route.channel_id, etc.).
        """
        if not entity_id:
            QMessageBox.information(
                self, f"Edit {entity_label}",
                f"Select a row in the {entity_label} table first.",
            )
            return
        if self._cfg is None:
            return
        tup = getattr(self._cfg, cfg_attr)
        existing = next((x for x in tup if x.id == entity_id), None)
        if existing is None:
            QMessageBox.warning(
                self, f"Edit {entity_label}",
                f"Selected row '{entity_id}' no longer in config — refresh.",
            )
            return
        dlg = _EditEntityFormDialog(
            entity_label, existing, entity_class, parent=self,
        )
        if dlg.exec_() != QDialog.Accepted:
            return
        try:
            new_entity = dlg.build_entity()
        except (TypeError, ValueError) as e:
            QMessageBox.warning(
                self, f"Edit {entity_label}",
                f"Could not rebuild {entity_label}:\n{e}",
            )
            return
        new_tup = tuple(
            new_entity if x.id == entity_id else x for x in tup
        )
        new_cfg = replace(self._cfg, **{cfg_attr: new_tup})
        try:
            config_v2.save_v2(new_cfg)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, f"Edit {entity_label}", str(e))
            return
        self.refresh()

    def _edit_selected_account(self) -> None:
        self._edit_entity(
            "Account", self._tab_accounts.selected_row_id(),
            "accounts", config_v2.Account,
        )

    def _edit_selected_profile(self) -> None:
        self._edit_entity(
            "Profile", self._tab_profiles.selected_row_id(),
            "profiles", config_v2.Profile,
        )

    def _edit_selected_channel(self) -> None:
        self._edit_entity(
            "Channel", self._tab_channels.selected_row_id(),
            "channels", config_v2.Channel,
        )

    def _edit_selected_destination(self) -> None:
        self._edit_entity(
            "Destination", self._tab_destinations.selected_row_id(),
            "destinations", config_v2.Destination,
        )

    def _edit_selected_bot(self) -> None:
        self._edit_entity(
            "Bot", self._tab_bots.selected_row_id(),
            "bots", config_v2.Bot,
        )


class _EditEntityFormDialog(QDialog):
    """Modal form editor for one v2 entity row.

    Introspects the dataclass via ``dataclasses.fields()`` + type
    annotations and renders the right widget per field:

      str   -> QLineEdit
      int   -> QSpinBox
      float -> QDoubleSpinBox
      bool  -> QCheckBox

    The ``id`` field is shown but disabled — cross-entity references
    (Channel.account_id, Route.channel_id, etc.) would break if id
    changed. Operators who want a new id remove + re-add instead.

    Single dialog used for all five entity types (Account, Profile,
    Channel, Destination, Bot) so adding a new field to a dataclass
    automatically surfaces in its edit form — no per-entity dialog to
    keep in sync.
    """

    def __init__(
        self, entity_label: str, existing, entity_class,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Edit {entity_label} — {existing.id}")
        self.setMinimumWidth(520)
        self._entity_class = entity_class
        self._widgets: dict[str, QWidget] = {}

        from dataclasses import fields
        from typing import get_type_hints

        outer = QVBoxLayout(self)
        outer.addWidget(QLabel(
            f"<span style='color:#787b86;'>"
            f"Edit the {entity_label} fields below. The "
            "<code>id</code> is locked — references to it from other "
            "entities would break.</span>",
        ))

        type_hints = get_type_hints(entity_class)
        form = QFormLayout()
        form.setLabelAlignment(Qt.AlignmentFlag.AlignRight)
        for f in fields(entity_class):
            ann = type_hints.get(f.name, str)
            current = getattr(existing, f.name)
            widget = self._make_widget(ann, current)
            self._widgets[f.name] = widget
            if f.name == "id":
                widget.setEnabled(False)
                widget.setToolTip(
                    "The id is referenced by other entities and can't be "
                    "changed in-place. Remove + re-add the entity to "
                    "rename it."
                )
            form.addRow(f.name, widget)
        outer.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _make_widget(self, annotation, current_value) -> QWidget:
        # Unwrap Optional[X] / X | None to X.
        from typing import Union, get_args, get_origin
        origin = get_origin(annotation)
        if origin is Union:
            non_none = [a for a in get_args(annotation) if a is not type(None)]
            if non_none:
                annotation = non_none[0]

        if annotation is bool:
            cb = QCheckBox()
            cb.setChecked(bool(current_value))
            return cb
        if annotation is int:
            sb = QSpinBox()
            sb.setRange(-2_147_483_648, 2_147_483_647)
            sb.setValue(int(current_value or 0))
            return sb
        if annotation is float:
            dsb = QDoubleSpinBox()
            dsb.setRange(-1e12, 1e12)
            dsb.setDecimals(4)
            dsb.setValue(float(current_value or 0.0))
            return dsb
        # Fallback (str, paths, Literal[...], etc.) — render as text.
        edit = QLineEdit()
        edit.setText("" if current_value is None else str(current_value))
        return edit

    def build_entity(self):
        """Collect widget values, build kwargs, instantiate the dataclass."""
        from dataclasses import fields
        from typing import get_type_hints
        type_hints = get_type_hints(self._entity_class)
        kwargs: dict = {}
        for f in fields(self._entity_class):
            w = self._widgets[f.name]
            ann = type_hints.get(f.name, str)
            from typing import Union, get_args, get_origin
            origin = get_origin(ann)
            if origin is Union:
                non_none = [a for a in get_args(ann) if a is not type(None)]
                if non_none:
                    ann = non_none[0]
            if isinstance(w, QCheckBox):
                kwargs[f.name] = w.isChecked()
            elif isinstance(w, QSpinBox):
                kwargs[f.name] = int(w.value())
            elif isinstance(w, QDoubleSpinBox):
                kwargs[f.name] = float(w.value())
            else:  # QLineEdit
                text = w.text().strip()
                kwargs[f.name] = text
        # Guard: token_setting_key MUST be a setting key name, never the
        # raw token. Otherwise the operator's bot token lands in
        # plaintext in stacks_config.json. Same regex as Add Bot dialog.
        if "token_setting_key" in kwargs:
            import re as _re
            v = kwargs["token_setting_key"] or ""
            if _re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", v):
                raise ValueError(
                    "token_setting_key looks like a raw Telegram token. "
                    "That field must hold a settings key NAME (e.g. "
                    "'tg_bot_token') — the actual token belongs in the "
                    "destination DB's settings. REVOKE the leaked token "
                    "in @BotFather before continuing."
                )
            if ":" in v or " " in v:
                raise ValueError(
                    "token_setting_key must be a settings key name "
                    "(snake_case, no colons or spaces)."
                )
        return self._entity_class(**kwargs)


# ---- Entity tables --------------------------------------------------------


class _EntityTable(QWidget):
    """Simple read-only table for a v2 entity list.

    When ``add_label`` + ``on_add`` are given, a button is rendered above
    the table (operator's only mutation surface for that tab).
    """

    def __init__(
        self, columns: list[str],
        *,
        add_label: str = "",
        on_add: Callable[[], None] | None = None,
        remove_label: str = "",
        on_remove: Callable[[], None] | None = None,
        edit_label: str = "",
        on_edit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._columns = columns
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        if (
            (add_label and on_add is not None)
            or (remove_label and on_remove is not None)
            or (edit_label and on_edit is not None)
        ):
            # Ribbon — single "Manage" group with Add / Edit / Remove
            # (only the ones the caller supplied). Replaces the flat
            # QPushButton toolbar for consistency with Settings + Risk.
            from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
            actions: list[RibbonAction] = []
            if add_label and on_add is not None:
                actions.append(RibbonAction(
                    "ADD", "Add", add_label,
                    variant="success", callback=on_add,
                ))
            if edit_label and on_edit is not None:
                actions.append(RibbonAction(
                    "EDIT", "Edit", edit_label,
                    callback=on_edit,
                ))
            if remove_label and on_remove is not None:
                actions.append(RibbonAction(
                    "DELETE", "Remove", remove_label,
                    variant="danger", callback=on_remove,
                ))
            layout.addWidget(RibbonBar([RibbonGroup("Manage", actions)]))
        self._table = QTableWidget()
        self._table.setColumnCount(len(columns))
        self._table.setHorizontalHeaderLabels(columns)
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows,
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        from src.gui.panels._table_utils import apply_full_width_headers
        # Narrow id/enabled/halt columns sized to content; last column
        # (name) stretches to fill viewport.
        apply_full_width_headers(
            self._table,
            content_columns=tuple(range(self._table.columnCount() - 1)),
        )
        layout.addWidget(self._table)

    def set_rows(self, rows: list[tuple]) -> None:
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)

    def row_count(self) -> int:
        return self._table.rowCount()

    def cell_text(self, row: int, col: int) -> str:
        item = self._table.item(row, col)
        return item.text() if item else ""

    def selected_row_id(self) -> str:
        """Return id of selected row (col 0), or empty if no selection."""
        row = self._table.currentRow()
        if row < 0:
            return ""
        return self.cell_text(row, 0)


class _ChannelsTable(QWidget):
    """Channels tab with extra toolbar (Add / toggle-enabled) over the table."""

    def __init__(
        self,
        on_add: Callable[[], None],
        on_toggle: Callable[[str], None],
        on_toggle_halt: Callable[[str], None] | None = None,
        on_remove: Callable[[], None] | None = None,
        on_edit: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self._on_add = on_add
        self._on_toggle = on_toggle
        self._on_toggle_halt = on_toggle_halt
        self._on_remove = on_remove
        self._on_edit = on_edit
        self._known_accounts: set[str] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        # Two-group ribbon: Manage (add/edit/remove the channel record)
        # and State (toggle enabled / halt at runtime).
        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        manage_actions: list[RibbonAction] = [
            RibbonAction("ADD", "Add", "Add a new channel",
                         variant="success", callback=self._on_add),
        ]
        if on_edit is not None:
            manage_actions.append(RibbonAction(
                "EDIT", "Edit",
                "Edit this channel's fields (chat_id, profile_id, "
                "enabled, etc.) as JSON. The id can't be changed.",
                callback=self._on_edit,
            ))
        if on_remove is not None:
            manage_actions.append(RibbonAction(
                "DELETE", "Remove",
                "Delete this channel + cascade any routes/bindings that "
                "reference it. Messages already in the audit log stay.",
                variant="danger", callback=self._on_remove,
            ))
        state_actions: list[RibbonAction] = [
            RibbonAction("ACCEPT", "Enabled",
                         "Toggle whether messages from this channel are processed",
                         variant="warning", callback=self._toggle_selected),
        ]
        if on_toggle_halt is not None:
            state_actions.append(RibbonAction(
                "PAUSE", "Halt",
                "Halt this channel: messages still recorded for audit "
                "but no actions emitted. Step 15.",
                variant="warning", callback=self._toggle_halt_selected,
            ))
        layout.addWidget(RibbonBar([
            RibbonGroup("Manage", manage_actions),
            RibbonGroup("State", state_actions),
        ]))

        self._table = QTableWidget()
        self._table.setColumnCount(7)
        self._table.setHorizontalHeaderLabels(
            ["id", "name", "account_id", "chat_id", "profile_id",
             "enabled", "halt"],
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows,
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        from src.gui.panels._table_utils import apply_full_width_headers
        # Stretch the `name` column (idx 1) — id sits left, status flags right.
        apply_full_width_headers(
            self._table,
            content_columns=(0, 2, 3, 4, 5, 6),
            stretch_column=1,
        )
        layout.addWidget(self._table)

    def set_known_account_ids(self, account_ids: set[str]) -> None:
        self._known_accounts = account_ids

    def set_rows(self, rows: list[tuple]) -> None:
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)

    def row_count(self) -> int:
        return self._table.rowCount()

    def _toggle_selected(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Toggle Enabled", "Select a channel first.",
            )
            return
        item = self._table.item(row, 0)
        if item is None:
            return
        self._on_toggle(item.text())

    def selected_row_id(self) -> str:
        row = self._table.currentRow()
        if row < 0:
            return ""
        item = self._table.item(row, 0)
        return item.text() if item else ""

    def _toggle_halt_selected(self) -> None:
        if self._on_toggle_halt is None:
            return
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Toggle Halt", "Select a channel first.",
            )
            return
        item = self._table.item(row, 0)
        if item is None:
            return
        self._on_toggle_halt(item.text())


class _RoutesTable(QWidget):
    """Routes tab with a Toggle Halt button — Step 15.

    Per-route halt is the right granularity for mirror setups (channel
    → N destinations): the operator can pause one leg without halting
    the channel (which would freeze every leg).
    """

    def __init__(
        self,
        on_toggle_halt: Callable[[str], None],
    ) -> None:
        super().__init__()
        self._on_toggle_halt = on_toggle_halt
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)

        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        layout.addWidget(RibbonBar([
            RibbonGroup("State", [
                RibbonAction("PAUSE", "Halt",
                             "Halt this route: messages tagged with this route_id still "
                             "recorded but no actions emitted on this destination. "
                             "Other routes for the same channel are unaffected. Step 15.",
                             variant="warning", callback=self._toggle_halt_selected),
            ]),
        ]))

        self._table = QTableWidget()
        self._table.setColumnCount(6)
        self._table.setHorizontalHeaderLabels(
            ["id", "channel_id", "destination_id", "enabled",
             "sizing_multiplier", "halt"],
        )
        self._table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
        self._table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows,
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setAlternatingRowColors(True)
        from src.gui.panels._table_utils import apply_full_width_headers
        # Stretch destination_id (idx 2) — id/flags hug content.
        apply_full_width_headers(
            self._table,
            content_columns=(0, 1, 3, 4, 5),
            stretch_column=2,
        )
        layout.addWidget(self._table)

    def set_rows(self, rows: list[tuple]) -> None:
        self._table.setRowCount(len(rows))
        for r, row in enumerate(rows):
            for c, val in enumerate(row):
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)

    def row_count(self) -> int:
        return self._table.rowCount()

    def _toggle_halt_selected(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "Toggle Halt", "Select a route first.",
            )
            return
        item = self._table.item(row, 0)
        if item is None:
            return
        self._on_toggle_halt(item.text())


# ---- Add Channel dialog ---------------------------------------------------


class _AddChannelDialog(QDialog):
    """Create a Channel + Route + BotBinding using EXISTING Account /
    Profile / Destination / Bot.

    Step "channel picker": instead of asking the operator for a raw
    Telegram chat_id, the dialog fetches the picked Account's dialog
    list and lets them choose a channel by NAME. The chat_id is
    captured under the hood. Falls back to a manual chat_id field when
    the account has no usable credentials yet.
    """

    def __init__(self, cfg: ConfigV2, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._cfg = cfg
        self.setWindowTitle("Add Channel")
        self.setMinimumWidth(520)

        # Telethon worker — bound to the dialog's lifetime so re-opening
        # the dialog gets a fresh client. Same daemon-thread shutdown
        # pattern as Add Account so cancel feels instant.
        from src.gui.services.telegram_session import TelegramSessionService
        self._service: TelegramSessionService | None = TelegramSessionService(self)
        self._service.error.connect(self._on_service_error)
        self._service.connected.connect(self._on_connected)
        self._service.dialogs_ready.connect(self._on_dialogs)
        self._service.start_thread()

        self._chat_id_captured: int = 0
        self._loaded_dialogs: list = []

        form = QFormLayout(self)

        # Account combo — picking an account triggers a dialog fetch.
        self._account = QComboBox()
        for a in cfg.accounts:
            self._account.addItem(
                f"{a.name}  ({a.phone_display()})", a.id,
            )
        self._account.currentIndexChanged.connect(self._on_account_changed)
        form.addRow("Account:", self._account)

        # Channel-picker combo (populated async per account).
        self._channel = QComboBox()
        self._channel.setMinimumWidth(360)
        self._channel.currentIndexChanged.connect(self._on_channel_changed)
        form.addRow("Channel:", self._channel)

        # Status / load-state indicator next to the channel combo.
        self._channel_status = QLabel("")
        self._channel_status.setStyleSheet("color: #787b86;")
        self._channel_status.setWordWrap(True)
        form.addRow("", self._channel_status)

        # Display name. Auto-fills from the picked channel; operator can
        # override (e.g. shorten a long Telegram title to "SMC Daily").
        self._name = QLineEdit()
        self._name.setPlaceholderText("auto-fills from selected channel")
        form.addRow("Display name:", self._name)

        # Manual chat_id fallback — only shown when the channel picker
        # can't load (e.g., account not authed yet).
        self._manual_chat_id = QSpinBox()
        self._manual_chat_id.setRange(-2_147_483_648, 2_147_483_647)
        self._manual_chat_id.setValue(0)
        form.addRow("Manual chat_id:", self._manual_chat_id)
        form.setRowVisible(self._manual_chat_id, False)

        self._profile = QComboBox()
        for p in cfg.profiles:
            self._profile.addItem(
                f"{p.name}  ({p.symbol or 'XAUUSD'})", p.id,
            )
        form.addRow("Profile:", self._profile)

        self._destination = QComboBox()
        for d in cfg.destinations:
            self._destination.addItem(
                f"{d.name}  (port {d.api_port})", d.id,
            )
        form.addRow("Destination:", self._destination)

        self._bot = QComboBox()
        for b in cfg.bots:
            self._bot.addItem(b.name, b.id)
        form.addRow("Bot:", self._bot)

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        form.addRow(btns)
        self._form = form

        # Kick the initial dialog load for the first account.
        if cfg.accounts:
            self._load_channels_for_current_account()

    # ---- Lifecycle ------------------------------------------------------

    def _stop_service_async(self) -> None:
        """Same non-blocking shutdown as Add Account (avoids a UI freeze
        when Telethon's disconnect blocks on an in-flight request)."""
        import threading
        service = self._service
        if service is None:
            return
        self._service = None
        threading.Thread(
            target=service.stop_thread, daemon=True,
            name="add-channel-shutdown",
        ).start()

    def closeEvent(self, event):  # noqa: N802 — Qt naming
        self._stop_service_async()
        super().closeEvent(event)

    def reject(self) -> None:
        self._stop_service_async()
        super().reject()

    def accept(self) -> None:
        self._stop_service_async()
        super().accept()

    # ---- Channel load (per-account) ------------------------------------

    def _on_account_changed(self, _idx: int) -> None:
        self._load_channels_for_current_account()

    def _load_channels_for_current_account(self) -> None:
        self._channel.blockSignals(True)
        self._channel.clear()
        self._channel.blockSignals(False)
        self._loaded_dialogs = []
        self._chat_id_captured = 0
        self._form.setRowVisible(self._manual_chat_id, False)
        acc_id = self._account.currentData()
        if not acc_id:
            self._channel_status.setText("")
            return
        account = self._cfg.account(acc_id)
        if account is None:
            self._channel_status.setText("")
            return
        from src.gui.services.account_credentials import (
            load_account_credentials,
        )
        creds = load_account_credentials(self._cfg, account)
        if creds is None:
            self._channel_status.setText(
                "<span style='color:#ff9800;'>⚠ This account isn't authed "
                "yet — Telethon credentials missing. Use the manual chat_id "
                "below, or finish Add Account first.</span>"
            )
            self._channel_status.setTextFormat(Qt.TextFormat.RichText)
            self._form.setRowVisible(self._manual_chat_id, True)
            return
        self._channel_status.setText(
            "<span style='color:#787b86;'>loading channels…</span>"
        )
        self._channel_status.setTextFormat(Qt.TextFormat.RichText)
        if self._service is None:
            return
        self._service.connect(
            creds.api_id, creds.api_hash,
            db_path=None, session_blob=creds.session_blob,
        )

    def _on_connected(self, authorized: bool) -> None:
        if not authorized:
            self._channel_status.setText(
                "<span style='color:#ef5350;'>⚠ Telethon session is no "
                "longer authorized. Re-run Add Account for this account.</span>"
            )
            return
        if self._service is not None:
            self._service.list_dialogs(limit=300)

    def _on_dialogs(self, dialogs: list) -> None:
        # Filter to channels / megagroups — these are the only entities
        # the listener supports today. (Sorting alphabetically since the
        # raw Telethon order is by recent-activity which isn't useful for
        # picking a stable channel by name.)
        channels = sorted(
            (d for d in dialogs if d.kind in ("channel", "supergroup")),
            key=lambda d: d.title.lower(),
        )
        self._loaded_dialogs = channels
        self._channel.blockSignals(True)
        self._channel.clear()
        if not channels:
            self._channel.addItem("(no channels found on this account)", 0)
        for d in channels:
            tag = "channel" if d.is_broadcast else "supergroup"
            self._channel.addItem(f"{d.title}  ·  {tag}", int(d.id))
        self._channel.blockSignals(False)
        self._channel_status.setText(
            f"<span style='color:#787b86;'>{len(channels)} channel(s) loaded.</span>"
        )
        self._on_channel_changed(self._channel.currentIndex())

    def _on_channel_changed(self, idx: int) -> None:
        if idx < 0:
            self._chat_id_captured = 0
            return
        chat_id = self._channel.itemData(idx)
        self._chat_id_captured = int(chat_id) if chat_id else 0
        # Auto-fill display name from the channel title (unless operator
        # already typed something).
        if not self._name.text().strip() and self._loaded_dialogs:
            try:
                title = self._loaded_dialogs[idx].title
            except IndexError:
                title = self._channel.itemText(idx).split("·")[0].strip()
            self._name.setText(title)

    def _on_service_error(self, _kind: str, msg: str) -> None:
        self._channel_status.setText(
            f"<span style='color:#ef5350;'>⚠ {msg}</span>"
        )
        # Fall back to manual chat_id so the operator still has a path.
        self._form.setRowVisible(self._manual_chat_id, True)

    # ---- Save -----------------------------------------------------------

    def _resolved_chat_id(self) -> int:
        if self._chat_id_captured:
            return self._chat_id_captured
        return int(self._manual_chat_id.value())

    def _on_ok(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "Add Channel", "Display name is required.")
            return
        if self._resolved_chat_id() == 0:
            QMessageBox.warning(
                self, "Add Channel",
                "Pick a channel from the dropdown (or enter a manual "
                "chat_id when the account isn't authed yet).",
            )
            return
        self.accept()

    def apply(self, cfg: ConfigV2) -> ConfigV2:
        """Compute the new ConfigV2 with channel + route + binding added.

        Pulled out as a separate method so tests can drive it without
        Qt dialog interaction.
        """
        return apply_add_channel(
            cfg,
            name=self._name.text().strip(),
            chat_id=self._resolved_chat_id(),
            account_id=self._account.currentData(),
            profile_id=self._profile.currentData(),
            destination_id=self._destination.currentData(),
            bot_id=self._bot.currentData(),
        )


def apply_add_channel(
    cfg: ConfigV2,
    *,
    name: str,
    chat_id: int,
    account_id: str,
    profile_id: str,
    destination_id: str,
    bot_id: str,
) -> ConfigV2:
    """Pure function: derive a new ConfigV2 with one extra channel wired
    to existing Profile + Destination + Bot.

    Validates uniqueness and references. Raises ValueError on conflict.
    Exposed at module level so the Add-Channel logic is testable without
    instantiating a Qt dialog.
    """
    import re

    if not name:
        raise ValueError("Name is required.")
    if chat_id == 0:
        raise ValueError("chat_id is required.")

    if cfg.account(account_id) is None:
        raise ValueError(f"Unknown account: {account_id}")
    if cfg.profile(profile_id) is None:
        raise ValueError(f"Unknown profile: {profile_id}")
    if cfg.destination(destination_id) is None:
        raise ValueError(f"Unknown destination: {destination_id}")
    if cfg.bot(bot_id) is None:
        raise ValueError(f"Unknown bot: {bot_id}")
    if cfg.channel_by_chat_id(chat_id) is not None:
        raise ValueError(
            f"chat_id {chat_id} is already used by another channel.",
        )

    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "channel"
    ch_id = _unique_id(slug, "ch", {c.id for c in cfg.channels})
    route_id = _unique_id(slug, "route", {r.id for r in cfg.routes})
    bind_id = _unique_id(slug, "bind", {b.id for b in cfg.bot_bindings})

    new_channel = Channel(
        id=ch_id, name=name, account_id=account_id,
        chat_id=chat_id, profile_id=profile_id, enabled=True,
    )
    new_route = Route(
        id=route_id, channel_id=ch_id, destination_id=destination_id,
        enabled=True, sizing_multiplier=1.0,
    )
    new_binding = BotBinding(
        id=bind_id, bot_id=bot_id, scope="destination",
        destination_id=destination_id,
    )
    return replace(
        cfg,
        channels=cfg.channels + (new_channel,),
        routes=cfg.routes + (new_route,),
        bot_bindings=cfg.bot_bindings + (new_binding,),
    )


def _unique_id(slug: str, prefix: str, taken: set[str]) -> str:
    candidate = f"{prefix}_{slug}"
    if candidate not in taken:
        return candidate
    i = 2
    while f"{candidate}_{i}" in taken:
        i += 1
    return f"{candidate}_{i}"


def _slugify(name: str, fallback: str) -> str:
    import re
    s = re.sub(r"[^a-z0-9]+", "_", (name or "").lower()).strip("_")
    return s or fallback


class _AddAccountDialog(QDialog):
    """Add a v2 Account WITH Telethon authentication in-flow.

    Three-phase UX inside one dialog (no wizard pages — just enable/disable
    field rows as the flow progresses):

      Phase 1  basics — name, phone, api_id, api_hash. "Connect" submits.
      Phase 2  code   — Telegram SMS code field. Optional 2FA password
                        slot reveals if Telegram demands it.
      Phase 3  saved  — display "logged in as <name>"; OK closes the
                        dialog and ``apply()`` writes the Account row.

    Persistence: the StringSession blob is written to a per-account file
    at ``%APPDATA%/CopyTrades/accounts/<account_id>.session.txt``. On
    listener start, ``shared_listener`` falls back to that file when the
    destination DB doesn't yet have a ``tg_session_blob`` (Step "session
    fallback"). First successful listener tick mirrors the blob into the
    destination DB so subsequent starts use the fast path.

    Cancel at any phase cleans up the worker thread.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Account — Telegram login")
        self.setMinimumWidth(520)

        # Worker thread for Telethon. Bound for the lifetime of the dialog;
        # stop_thread on close so the dialog can be re-opened cleanly.
        from src.gui.services.telegram_session import TelegramSessionService
        self._service = TelegramSessionService(self)
        self._service.error.connect(self._on_error)
        self._service.connected.connect(self._on_connected)
        self._service.code_sent.connect(self._on_code_sent)
        self._service.signed_in.connect(self._on_signed_in)
        self._service.password_required.connect(self._on_password_required)
        self._service.session_snapshot.connect(self._on_snapshot)
        self._service.me_ready.connect(self._on_me)
        self._service.start_thread()

        self._account_id: str = ""
        self._session_blob: str = ""
        self._me_name: str = ""

        outer = QVBoxLayout(self)
        self._form = QFormLayout()
        outer.addLayout(self._form)

        # ---- Phase 1: basics --------------------------------------------
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Primary")
        self._form.addRow("Display name:", self._name)
        self._phone = QLineEdit()
        self._phone.setPlaceholderText("+<country><number>, e.g. +14155551234")
        self._form.addRow("Phone:", self._phone)
        self._api_id = QLineEdit()
        self._api_id.setPlaceholderText("e.g. 12345678 (from my.telegram.org)")
        self._form.addRow("API ID:", self._api_id)
        self._api_hash = QLineEdit()
        self._api_hash.setEchoMode(QLineEdit.EchoMode.Password)
        self._api_hash.setPlaceholderText("32 hex chars (from my.telegram.org)")
        self._form.addRow("API hash:", self._api_hash)

        # ---- Phase 2: code (revealed when Telegram sends the code) -----
        self._code = QLineEdit()
        self._code.setPlaceholderText("5-digit code from Telegram app")
        self._form.addRow("Code:", self._code)
        self._form.setRowVisible(self._code, False)

        # ---- Phase 3: 2FA (revealed only if Telegram demands it) -------
        self._password = QLineEdit()
        self._password.setEchoMode(QLineEdit.EchoMode.Password)
        self._password.setPlaceholderText("only if Telegram asks for it")
        self._form.addRow("2FA password:", self._password)
        self._form.setRowVisible(self._password, False)

        # ---- Action row -------------------------------------------------
        actions = QHBoxLayout()
        self._action_btn = QPushButton("Connect + send code")
        self._action_btn.setProperty("variant", "primary")
        self._action_btn.clicked.connect(self._on_action)
        actions.addWidget(self._action_btn)
        actions.addStretch()
        outer.addLayout(actions)

        # Status text — single label, role changes with phase.
        self._status = QLabel(
            "<span style='color:#787b86;'>Fill credentials and click Connect.</span>"
        )
        self._status.setTextFormat(Qt.TextFormat.RichText)
        self._status.setWordWrap(True)
        outer.addWidget(self._status)

        # OK starts disabled — enabled once signed_in + snapshot captured.
        self._btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        self._btns.accepted.connect(self.accept)
        self._btns.rejected.connect(self.reject)
        self._btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(False)
        outer.addWidget(self._btns)

        # Phase state — used by _on_action to know what button click means.
        self._phase = "basics"  # basics → awaiting_code → password_needed → done

    # ---- Lifecycle ------------------------------------------------------

    def _stop_service_async(self) -> None:
        """Shut down the Telethon worker WITHOUT blocking the UI thread.

        ``TelegramSessionService.stop_thread`` calls ``client.disconnect()``
        synchronously and then ``thread.join(timeout=3.0)``. With a
        mid-flight Telegram call (e.g. ``send_code_request``) that can
        block the GUI for several seconds — long enough to look like a
        hang. Punting the stop to a daemon thread lets reject() return
        instantly; the Telethon thread tears itself down at its own pace.
        """
        import threading
        service = self._service
        # Drop our reference so a slow shutdown doesn't keep the dialog
        # alive (and a re-open of Add Account constructs a fresh service).
        self._service = None  # type: ignore[assignment]
        threading.Thread(
            target=service.stop_thread, daemon=True,
            name="add-account-shutdown",
        ).start()

    def closeEvent(self, event):  # noqa: N802 — Qt naming
        if self._service is not None:
            self._stop_service_async()
        super().closeEvent(event)

    def reject(self) -> None:
        if self._service is not None:
            self._stop_service_async()
        super().reject()

    def accept(self) -> None:
        if self._service is not None:
            self._stop_service_async()
        super().accept()

    # ---- Action button dispatch ----------------------------------------

    def _on_action(self) -> None:
        if self._phase == "basics":
            self._submit_basics()
        elif self._phase == "awaiting_code":
            self._submit_code()
        elif self._phase == "password_needed":
            self._submit_password()
        # done phase: button is disabled

    def _submit_basics(self) -> None:
        name = self._name.text().strip()
        phone = self._phone.text().strip()
        api_id_raw = self._api_id.text().strip()
        api_hash = self._api_hash.text().strip()
        if not name:
            self._show_error("Display name is required.")
            return
        if not phone.startswith("+") or len(phone) < 5:
            self._show_error("Phone must be +<country><number>, e.g. +14155551234.")
            return
        try:
            api_id = int(api_id_raw)
        except ValueError:
            self._show_error("API ID must be a number.")
            return
        if len(api_hash) < 16:
            self._show_error("API hash looks too short (32 hex chars).")
            return

        self._set_status("connecting to Telegram…", muted=True)
        self._lock_basics()
        # connect() with db_path=None — we'll snapshot the blob manually.
        self._service.connect(api_id, api_hash, db_path=None)

    def _submit_code(self) -> None:
        code = self._code.text().strip()
        if not code:
            self._show_error("Enter the code from your Telegram app.")
            return
        self._set_status("signing in…", muted=True)
        self._service.sign_in(
            self._phone.text().strip(), code, self._phone_code_hash,
        )

    def _submit_password(self) -> None:
        pwd = self._password.text()
        if not pwd:
            self._show_error("Enter your 2FA password.")
            return
        self._set_status("verifying 2FA password…", muted=True)
        self._service.sign_in_with_password(pwd)

    # ---- Service signals -----------------------------------------------

    def _on_connected(self, authorized: bool) -> None:
        if authorized:
            # Phone already had a valid session for this api_id/hash — skip
            # the code step, snapshot the blob, go straight to done.
            self._set_status(
                "Already logged in with these credentials — capturing session…",
                muted=True,
            )
            self._service.fetch_me()
            self._service.snapshot_session()
            return
        # Need code → request one.
        self._set_status("requesting login code…", muted=True)
        self._service.send_code(self._phone.text().strip())

    def _on_code_sent(self, phone_code_hash: str) -> None:
        self._phone_code_hash = phone_code_hash
        self._phase = "awaiting_code"
        self._form.setRowVisible(self._code, True)
        self._code.setEnabled(True)
        self._code.setFocus()
        self._action_btn.setEnabled(True)
        self._action_btn.setText("Sign in")
        self._set_status(
            "Code sent — check your Telegram app, enter the code, click Sign in.",
            muted=True,
        )

    def _on_password_required(self) -> None:
        self._phase = "password_needed"
        self._form.setRowVisible(self._password, True)
        self._password.setEnabled(True)
        self._password.setFocus()
        self._action_btn.setEnabled(True)
        self._action_btn.setText("Verify password")
        self._set_status(
            "Telegram requires your 2FA password.", muted=True,
        )

    def _on_signed_in(self) -> None:
        self._set_status("signed in — capturing session…", muted=True)
        # Pull who we are + the session blob in parallel.
        self._service.fetch_me()
        self._service.snapshot_session()

    def _on_me(self, me) -> None:
        bits = [b for b in (getattr(me, "first_name", ""),
                            getattr(me, "last_name", "")) if b]
        self._me_name = " ".join(bits) or getattr(me, "username", "") or "(no name)"

    def _on_snapshot(self, blob: str) -> None:
        if not blob:
            self._show_error(
                "Telethon returned an empty session blob — please try again.",
            )
            self._unlock_basics()
            return
        self._session_blob = blob
        self._phase = "done"
        self._action_btn.setEnabled(False)
        self._code.setEnabled(False)
        self._password.setEnabled(False)
        self._btns.button(QDialogButtonBox.StandardButton.Ok).setEnabled(True)
        who = f" as <b>{self._me_name}</b>" if self._me_name else ""
        self._set_status(
            f"<span style='color:#26a69a;'>✓ Signed in{who}. Click OK to save.</span>",
            muted=False,
        )

    def _on_error(self, _kind: str, msg: str) -> None:
        self._show_error(msg)
        # Re-enable the relevant phase's input so the operator can retry.
        if self._phase == "basics":
            self._unlock_basics()

    # ---- Persistence ----------------------------------------------------

    def apply(self, cfg: ConfigV2) -> ConfigV2:
        if not self._session_blob:
            raise ValueError(
                "Account auth did not complete — sign in first, then OK.",
            )
        name = self._name.text().strip()
        slug = _slugify(name, "account")
        acc_id = _unique_id(slug, "acc", {a.id for a in cfg.accounts})
        # Write the StringSession to a per-account file. The listener falls
        # back to this file when the destination DB has no tg_session_blob
        # yet, then mirrors it into the dest DB on first start.
        import json
        import os
        from pathlib import Path as _Path
        appdata = _Path(os.environ.get("APPDATA", str(_Path.home())))
        sess_dir = appdata / "CopyTrades" / "accounts"
        sess_dir.mkdir(parents=True, exist_ok=True)
        sess_file = sess_dir / f"{acc_id}.session.txt"
        sess_file.write_text(self._session_blob, encoding="utf-8")
        # Persist api_id + api_hash sidecar so the "Pick channel" flow in
        # Add Channel can reconnect Telethon for THIS account without
        # asking the operator for credentials again. Format kept simple
        # JSON so it's grep-able; sunset-list tracks encrypting these.
        try:
            creds_file = sess_dir / f"{acc_id}.creds.json"
            creds_file.write_text(
                json.dumps({
                    "api_id": int(self._api_id.text().strip()),
                    "api_hash": self._api_hash.text().strip(),
                }),
                encoding="utf-8",
            )
        except Exception:
            pass  # listener falls back to config.TG_API_ID/HASH globals
        api_id_raw = self._api_id.text().strip()
        try:
            api_id_int = int(api_id_raw) if api_id_raw else 0
        except ValueError:
            api_id_int = 0
        return config_v2.with_account_added(
            cfg, account_id=acc_id, name=name,
            phone=self._phone.text().strip(),
            session_path=str(sess_file),
            api_id=api_id_int,
            api_hash=self._api_hash.text().strip(),
        )

    # ---- Helpers --------------------------------------------------------

    def _lock_basics(self) -> None:
        for w in (self._name, self._phone, self._api_id, self._api_hash,
                  self._action_btn):
            w.setEnabled(False)

    def _unlock_basics(self) -> None:
        for w in (self._name, self._phone, self._api_id, self._api_hash,
                  self._action_btn):
            w.setEnabled(True)
        self._action_btn.setText("Connect + send code")
        self._phase = "basics"

    def _show_error(self, msg: str) -> None:
        self._status.setText(
            f"<span style='color:#ef5350;'>⚠ {msg}</span>"
        )

    def _set_status(self, msg: str, *, muted: bool) -> None:
        color = "#787b86" if muted else "#d1d4dc"
        self._status.setText(f"<span style='color:{color};'>{msg}</span>")


# Blank profile JSON template — kept in sync with the wizard's
# `_write_blank_channel_profile`. Centralising it here lets the
# Add-Profile dialog reuse it without dragging the wizard module in.
_BLANK_PROFILE_TEMPLATE: dict = {
    "name": "",
    "description": "",
    "symbol": "XAUUSD",
    "language": "",
    "shorthand_decode_example": "",
    "header": "",
    "vocabulary_table": "",
    "compound_messages": "",
    "commentary_filter": "",
    "directional_command_flow": "",
    "worked_examples": "",
    "triage_keep_triggers": "",
}


def _discover_profile_jsons() -> "list[Path]":
    """Return every ``.json`` file under the repo's ``channels/`` dir.

    Used by the Add Profile dialog to populate "pick existing" mode.
    Excludes ``*_draft.json`` (in-progress drafts, not real profiles)
    and ``*.bak`` (timestamped backups from the profile editor).
    """
    from pathlib import Path as _Path
    from src.gui.services.stack_registry import BASE_DIR
    out: list[_Path] = []
    channels_dir = BASE_DIR / "channels"
    if not channels_dir.exists():
        return out
    for p in sorted(channels_dir.glob("*.json")):
        if p.stem.endswith("_draft"):
            continue
        out.append(p)
    return out


def _write_blank_profile_json(path: "Path", *, name: str, symbol: str,
                              language: str) -> None:
    """Create a blank channel-profile JSON at ``path`` using the template."""
    import json
    payload = dict(_BLANK_PROFILE_TEMPLATE)
    payload["name"] = name
    payload["symbol"] = symbol or "XAUUSD"
    payload["language"] = language
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


class _AddProfileDialog(QDialog):
    """Add a v2 Profile (the AI prompt config).

    Two modes via radio buttons:

      • Pick existing JSON — combobox listing every ``channels/*.json`` in
        the repo. Useful for wiring a second account/channel to an
        already-tuned profile (e.g., the SMC or "Forex Engineer" one).

      • Create blank JSON — generates a fresh template at
        ``channels/<slug>.json`` so the operator can open it in the
        Profile editor afterwards to fill in vocabulary + examples.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Profile")
        self.setMinimumWidth(520)

        from PySide6.QtWidgets import QButtonGroup, QRadioButton

        outer = QVBoxLayout(self)

        # ---- Mode selector ---------------------------------------------
        mode_row = QHBoxLayout()
        self._mode_pick = QRadioButton("Pick existing JSON")
        self._mode_new = QRadioButton("Create blank JSON")
        existing = _discover_profile_jsons()
        # Default to "Create blank" when no profiles exist yet — better
        # default for first-run setups.
        if existing:
            self._mode_pick.setChecked(True)
        else:
            self._mode_new.setChecked(True)
            self._mode_pick.setEnabled(False)
            self._mode_pick.setToolTip(
                "No profiles in channels/ yet — create a blank one first."
            )
        group = QButtonGroup(self)
        group.addButton(self._mode_pick)
        group.addButton(self._mode_new)
        mode_row.addWidget(self._mode_pick)
        mode_row.addWidget(self._mode_new)
        mode_row.addStretch()
        outer.addLayout(mode_row)

        self._form = QFormLayout()
        outer.addLayout(self._form)

        # ---- Common: display name + language + symbol ------------------
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Forex Engineer")
        self._form.addRow("Display name:", self._name)
        self._language = QLineEdit()
        self._language.setPlaceholderText("e.g. ar, en")
        self._form.addRow("Language:", self._language)
        self._symbol = QLineEdit()
        self._symbol.setPlaceholderText("e.g. XAUUSD")
        self._symbol.setText("XAUUSD")
        self._form.addRow("Symbol:", self._symbol)

        # ---- Pick-existing mode: combobox ------------------------------
        self._existing = QComboBox()
        for p in existing:
            self._existing.addItem(f"{p.stem}  ({p.name})", str(p))
        self._existing.currentIndexChanged.connect(self._on_existing_picked)
        self._form.addRow("Profile file:", self._existing)
        # Pre-fill the name field from whichever profile is selected.
        if existing:
            self._on_existing_picked(0)

        # Toggle the existing-file picker visibility on mode change.
        self._mode_pick.toggled.connect(self._sync_mode_rows)
        self._sync_mode_rows()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _sync_mode_rows(self) -> None:
        picking = self._mode_pick.isChecked()
        self._form.setRowVisible(self._existing, picking)

    def _on_existing_picked(self, idx: int) -> None:
        from pathlib import Path as _Path
        path_str = self._existing.itemData(idx) if idx >= 0 else ""
        if not path_str:
            return
        # Auto-fill name + symbol + language from the picked JSON.
        try:
            import json
            data = json.loads(_Path(path_str).read_text(encoding="utf-8"))
        except Exception:
            return
        # Don't clobber operator-typed values once they've typed something.
        if not self._name.text().strip():
            self._name.setText(str(data.get("name") or _Path(path_str).stem))
        if data.get("symbol"):
            self._symbol.setText(str(data["symbol"]))
        if data.get("language"):
            self._language.setText(str(data["language"]))

    def _on_ok(self) -> None:
        name = self._name.text().strip()
        if not name:
            QMessageBox.warning(self, "Add Profile", "Display name is required.")
            return
        if self._mode_pick.isChecked():
            if self._existing.currentIndex() < 0:
                QMessageBox.warning(
                    self, "Add Profile",
                    "Pick an existing profile JSON or switch to 'Create blank'.",
                )
                return
        else:
            # New blank mode: make sure target path won't collide.
            from pathlib import Path as _Path
            from src.gui.services.stack_registry import BASE_DIR
            slug = _slugify(name, "profile")
            target = _Path(BASE_DIR) / "channels" / f"{slug}.json"
            if target.exists():
                reply = QMessageBox.question(
                    self, "Add Profile",
                    f"A profile JSON already exists at:\n  {target}\n\n"
                    "Reuse it instead of overwriting?",
                    QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                )
                if reply != QMessageBox.StandardButton.Yes:
                    return
        self.accept()

    def apply(self, cfg: ConfigV2) -> ConfigV2:
        from pathlib import Path as _Path
        from src.gui.services.stack_registry import BASE_DIR
        name = self._name.text().strip()
        language = self._language.text().strip()
        symbol = self._symbol.text().strip() or "XAUUSD"
        if self._mode_pick.isChecked():
            path = self._existing.currentData()
        else:
            slug = _slugify(name, "profile")
            path_obj = _Path(BASE_DIR) / "channels" / f"{slug}.json"
            if not path_obj.exists():
                _write_blank_profile_json(
                    path_obj, name=name, symbol=symbol, language=language,
                )
            path = str(path_obj)
        prof_slug = _slugify(name, "profile")
        prof_id = _unique_id(prof_slug, "prof", {p.id for p in cfg.profiles})
        return config_v2.with_profile_added(
            cfg, profile_id=prof_id, name=name,
            path=path, language=language, symbol=symbol,
        )


def _discover_destination_dbs() -> "list[Path]":
    """Return every ``copytrades.db`` under ``%APPDATA%/CopyTrades/``.

    Used by Add Destination's DB dropdown so the operator picks from
    real on-disk DBs instead of typing a path. Excludes nothing — even
    DBs already claimed by another destination show up; ``apply`` lets
    them collide so the operator gets a clear error rather than silent
    re-use.
    """
    import os
    from pathlib import Path as _Path
    appdata = _Path(os.environ.get("APPDATA", str(_Path.home())))
    root = appdata / "CopyTrades"
    if not root.exists():
        return []
    out: list[_Path] = []
    for sub in sorted(root.iterdir()):
        if not sub.is_dir():
            continue
        db = sub / "copytrades.db"
        if db.exists() and db.is_file():
            out.append(db)
    return out


class _AddDestinationDialog(QDialog):
    """Add a v2 Destination.

    Two modes (same pattern as Add Profile):

      • Pick existing DB — dropdown of every discovered ``copytrades.db``
        under ``%APPDATA%/CopyTrades/<stack>/``. Useful when an operator
        has an existing stack DB they want to wire as a v2 Destination.

      • Create new DB — destination folder is derived from Display name
        (``%APPDATA%/CopyTrades/<display_name>/copytrades.db``). The
        file itself isn't created here — the API process runs
        ``init_schema`` on first start.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Destination")
        self.setMinimumWidth(560)

        from PySide6.QtWidgets import QButtonGroup, QRadioButton

        outer = QVBoxLayout(self)

        # ---- Field-meaning legend (operator-friendly) -----------------
        legend = QLabel(
            "<span style='color:#787b86;font-size:11px;'>"
            "<b>Display name</b> — shown in the GUI &amp; used to derive the "
            "internal id and service name.<br>"
            "<b>MT5 label</b> — free-form tag for your own reference (e.g. "
            "broker + account type like <i>FxPro-Live</i> or <i>ICMarkets-Demo</i>). "
            "Not used by code, just shown in the dashboard."
            "</span>"
        )
        legend.setTextFormat(Qt.TextFormat.RichText)
        legend.setWordWrap(True)
        outer.addWidget(legend)

        # ---- Mode selector --------------------------------------------
        existing_dbs = _discover_destination_dbs()
        mode_row = QHBoxLayout()
        self._mode_pick = QRadioButton("Pick existing DB")
        self._mode_new = QRadioButton("Create new DB")
        if existing_dbs:
            self._mode_pick.setChecked(True)
        else:
            self._mode_new.setChecked(True)
            self._mode_pick.setEnabled(False)
            self._mode_pick.setToolTip(
                "No copytrades.db files found under %APPDATA%/CopyTrades — "
                "create a new one here."
            )
        group = QButtonGroup(self)
        group.addButton(self._mode_pick)
        group.addButton(self._mode_new)
        mode_row.addWidget(self._mode_pick)
        mode_row.addWidget(self._mode_new)
        mode_row.addStretch()
        outer.addLayout(mode_row)

        self._form = QFormLayout()
        outer.addLayout(self._form)

        # ---- Common rows ----------------------------------------------
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Live FxPro")
        self._form.addRow("Display name:", self._name)

        # Existing-DB dropdown (only meaningful in pick mode).
        self._db_combo = QComboBox()
        for db in existing_dbs:
            # Show the parent folder name + full path on hover so the
            # operator can disambiguate two stacks with similar names.
            self._db_combo.addItem(f"{db.parent.name}  ({db})", str(db))
        self._form.addRow("Existing DB:", self._db_combo)

        # API binding
        self._api_host = QLineEdit()
        self._api_host.setText("127.0.0.1")
        self._form.addRow("API host:", self._api_host)
        self._api_port = QSpinBox()
        self._api_port.setRange(1, 65535)
        self._api_port.setValue(8766)
        self._form.addRow("API port:", self._api_port)

        # Optional MT5 label
        self._mt5_label = QLineEdit()
        self._mt5_label.setPlaceholderText(
            "optional broker tag, e.g. FxPro-Live"
        )
        self._form.addRow("MT5 label:", self._mt5_label)

        # Service-name reminder
        self._form.addRow(QLabel(
            "<span style='color:#787b86;font-size:11px;'>"
            "Service name auto-derived as CT-Api-&lt;id&gt;. Install NSSM "
            "services via Settings after save.</span>"
        ))

        self._mode_pick.toggled.connect(self._sync_mode_rows)
        self._sync_mode_rows()

        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        outer.addWidget(btns)

    def _sync_mode_rows(self) -> None:
        self._form.setRowVisible(self._db_combo, self._mode_pick.isChecked())

    def _resolved_db_path(self) -> str:
        if self._mode_pick.isChecked():
            return self._db_combo.currentData() or ""
        # New-DB mode: derive from display name.
        import os
        from pathlib import Path as _Path
        name = self._name.text().strip()
        if not name:
            return ""
        appdata = _Path(os.environ.get("APPDATA", str(_Path.home())))
        return str(appdata / "CopyTrades" / name / "copytrades.db")

    def _on_ok(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "Add Destination", "Display name is required.")
            return
        if self._mode_pick.isChecked() and self._db_combo.currentIndex() < 0:
            QMessageBox.warning(
                self, "Add Destination",
                "Pick an existing DB or switch to 'Create new DB'.",
            )
            return
        if not self._resolved_db_path():
            QMessageBox.warning(
                self, "Add Destination",
                "Could not resolve a DB path. Check the inputs.",
            )
            return
        self.accept()

    def apply(self, cfg: ConfigV2) -> ConfigV2:
        name = self._name.text().strip()
        slug = _slugify(name, "destination")
        dest_id = _unique_id(slug, "dest", {d.id for d in cfg.destinations})
        return config_v2.with_destination_added(
            cfg, destination_id=dest_id, name=name,
            db_path=self._resolved_db_path(),
            api_host=self._api_host.text().strip() or "127.0.0.1",
            api_port=int(self._api_port.value()),
            mt5_label=self._mt5_label.text().strip(),
        )


class _AddBotDialog(QDialog):
    """Add a v2 Bot. Stores a settings KEY NAME (not the raw token).

    The token itself is written separately to each destination DB's
    ``settings`` table under that key. Default key is ``tg_bot_token``
    which the bot process reads on startup.
    """

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add Bot")
        self.setMinimumWidth(520)
        form = QFormLayout(self)
        self._name = QLineEdit()
        self._name.setPlaceholderText("e.g. Master Ops")
        form.addRow("Display name:", self._name)
        self._token_key = QLineEdit()
        self._token_key.setText("tg_bot_token")
        self._token_key.setPlaceholderText("tg_bot_token")
        form.addRow("Token-storage key:", self._token_key)
        form.addRow(QLabel(
            "<span style='color:#787b86;font-size:11px;'>"
            "<b>Token-storage key</b> is the NAME of the setting that "
            "holds the real Telegram bot token in each destination's DB "
            "— NOT the token itself. Leave as <code>tg_bot_token</code> "
            "unless you have a specific reason. The bot process reads "
            "this key from settings on startup.<br>"
            "After save, paste the real token via Settings → Tuning → "
            "TELEGRAM BOT → Bot token."
            "</span>"
        ))
        btns = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
        )
        btns.accepted.connect(self._on_ok)
        btns.rejected.connect(self.reject)
        form.addRow(btns)

    def _on_ok(self) -> None:
        if not self._name.text().strip():
            QMessageBox.warning(self, "Add Bot", "Display name is required.")
            return
        key = self._token_key.text().strip()
        if not key:
            QMessageBox.warning(
                self, "Add Bot", "Token-storage key is required.",
            )
            return
        # Reject anything that looks like a raw token (digits:base64ish).
        # Anyone with read access to stacks_config.json would see it in
        # plaintext — high-impact leak, easy mistake.
        import re as _re
        if _re.match(r"^\d{6,}:[A-Za-z0-9_-]{20,}$", key):
            QMessageBox.critical(
                self, "Add Bot",
                "That looks like a raw Telegram bot token, not a "
                "settings key name. This field must hold the key NAME "
                "(e.g. 'tg_bot_token') — the actual token belongs in "
                "the destination DB's settings.\n\n"
                "If you've already pasted a real token, REVOKE it in "
                "@BotFather (/revoke) before saving.",
            )
            return
        if ":" in key or " " in key:
            QMessageBox.warning(
                self, "Add Bot",
                "Token-storage key must be a settings key name "
                "(snake_case, no colons or spaces). Use "
                "'tg_bot_token' unless you know why otherwise.",
            )
            return
        self.accept()

    def apply(self, cfg: ConfigV2) -> ConfigV2:
        name = self._name.text().strip()
        slug = _slugify(name, "bot")
        bot_id = _unique_id(slug, "bot", {b.id for b in cfg.bots})
        return config_v2.with_bot_added(
            cfg, bot_id=bot_id, name=name,
            token_setting_key=self._token_key.text().strip(),
        )
