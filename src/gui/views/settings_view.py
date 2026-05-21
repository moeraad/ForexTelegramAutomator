"""SETTINGS view: tabs for Channels (stacks_config), .env, and Services."""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from src import db_settings
from src.gui.services import nssm_client
from src.gui.services.env_io import EnvLine, is_secret, parse_env, write_env
from src.gui.services.stack_registry import Stack
from src.gui.services.stacks_config_io import StackEntry, load_entries, save_entries, stacks_config_path
from src.gui.services.telegram_session import session_path
from src.gui.windows.telegram_wizard import TelegramWizard


class SettingsView(QWidget):
    new_stack_requested = Signal()

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>SETTINGS</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        from src.gui.panels._a11y import mark_heading
        mark_heading(title, "Settings")
        title_row.addWidget(title)
        self._status_label = QLabel("")
        # Use the muted-role token so light/dark palettes pick up the
        # right shade automatically (REVIEW.md §3 Theming).
        self._status_label.setProperty("role", "muted")
        self._status_label.setStyleSheet("padding-left: 16px;")
        title_row.addWidget(self._status_label)
        title_row.addStretch()
        self._wizard_btn = QPushButton("Setup wizard")
        self._wizard_btn.setToolTip("Re-configure the active stack (AI keys, bot token, channel, services).")
        self._wizard_btn.clicked.connect(self._open_telegram_wizard)
        title_row.addWidget(self._wizard_btn)
        self._new_stack_btn = QPushButton("+ New stack")
        self._new_stack_btn.setToolTip("Create a new stack from scratch (full setup wizard).")
        self._new_stack_btn.clicked.connect(self._open_new_stack_wizard)
        title_row.addWidget(self._new_stack_btn)
        # Service start/stop/restart lives in the top-right services bar
        # (one canonical surface). The Services tab below still owns
        # lifecycle (Install / Uninstall) and a read-only state view.
        layout.addLayout(title_row)

        self._tabs = QTabWidget()
        self._channels_tab = _ChannelsTab()
        self._tuning_tab = _TuningTab(stack)
        self._services_tab = _ServicesTab(stack)
        from src.gui.views._backup_tab import BackupTab
        self._backup_tab = BackupTab()
        self._tabs.addTab(self._channels_tab, "Stacks")
        self._tabs.addTab(self._tuning_tab, "Tuning")
        self._tabs.addTab(self._services_tab, "Services")
        self._tabs.addTab(self._backup_tab, "Backup")
        layout.addWidget(self._tabs, 1)

        self._refresh_status()

        self._status_timer = QTimer(self)
        self._status_timer.setInterval(3000)
        self._status_timer.timeout.connect(self._refresh_status)
        self._status_timer.start()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._tuning_tab.rebind(stack)
        self._services_tab.rebind(stack)
        self._refresh_status()

    def _open_telegram_wizard(self) -> None:
        chat_id_before = db_settings.get_int(
            self._stack.db_path, "tg_watched_chat_id", 0
        )
        TelegramWizard(self._stack, self).exec()
        chat_id_after = db_settings.get_int(
            self._stack.db_path, "tg_watched_chat_id", 0
        )
        self._tuning_tab.rebind(self._stack)
        self._refresh_status()
        if chat_id_before and chat_id_after and chat_id_before != chat_id_after:
            listener_svc = self._stack.service_names[2]
            if nssm_client.service_running(listener_svc):
                QMessageBox.warning(
                    self,
                    "Listener restart required",
                    f"Watched channel changed "
                    f"({chat_id_before} -> {chat_id_after}).\n\n"
                    f"The listener service '{listener_svc}' is still "
                    f"running with the previous channel id and will not "
                    f"see signals from the new channel until restarted. "
                    f"This is because Telethon binds the channel filter "
                    f"at handler-attach time (REVIEW.md P1).\n\n"
                    f"Stop and start services from this page to apply "
                    f"the change.",
                )

    def _open_new_stack_wizard(self) -> None:
        # Defer to MainWindow — it owns the stack list, services bar,
        # crash watcher, and all views that need to rebind to the new
        # stack after the wizard finishes.
        self.new_stack_requested.emit()

    def _refresh_status(self) -> None:
        missing = db_settings.missing_critical_keys(self._stack.db_path)
        if missing:
            self._status_label.setText(
                f"<span style='color:#ef5350;'>setup incomplete · "
                f"{len(missing)} critical key(s) missing</span>"
            )
        else:
            running = sum(
                1 for svc in self._stack.service_names if nssm_client.service_running(svc)
            )
            total = len(self._stack.service_names)
            self._status_label.setText(
                f"<span style='color:#787b86;'>services: {running}/{total} running</span>"
            )
        self._status_label.setTextFormat(Qt.TextFormat.RichText)


class _ChannelsTab(QWidget):
    HEADERS = ("Name", "Profile", "Project path", "DB override", "Services")

    def __init__(self) -> None:
        super().__init__()
        self._entries: list[StackEntry] = []
        self._build_ui()
        self._load()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)

        info = QLabel(
            f"<span style='color:#787b86;'>file: {stacks_config_path()}  ·  "
            "edits require an app restart to take effect</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(list(self.HEADERS))
        self._table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        apply_full_width_headers(
            self._table,
            content_columns=tuple(range(self._table.columnCount() - 1)),
        )
        self._table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        layout.addWidget(self._table, 1)

        actions = QHBoxLayout()
        for label, slot in (
            ("Add",    self._on_add),
            ("Edit",   self._on_edit),
            ("Remove", self._on_remove),
            ("Reload", self._load),
            ("Save",   self._save),
        ):
            btn = QPushButton(label)
            btn.clicked.connect(slot)
            actions.addWidget(btn)
        actions.addStretch()
        layout.addLayout(actions)

    def _load(self) -> None:
        self._entries = load_entries()
        self._refresh_table()

    def _refresh_table(self) -> None:
        self._table.setRowCount(len(self._entries))
        for r, e in enumerate(self._entries):
            for c, val in enumerate((
                e.name,
                e.profile_path,
                e.project_path,
                e.db_path or "—",
                ", ".join(e.service_names) if e.service_names else "(auto)",
            )):
                item = QTableWidgetItem(val)
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                self._table.setItem(r, c, item)

    def _on_add(self) -> None:
        dlg = _StackEntryDialog(self, None)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.entry is not None:
            self._entries.append(dlg.entry)
            self._refresh_table()

    def _on_edit(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        dlg = _StackEntryDialog(self, self._entries[row])
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.entry is not None:
            self._entries[row] = dlg.entry
            self._refresh_table()

    def _on_remove(self) -> None:
        row = self._table.currentRow()
        if row < 0:
            return
        ans = QMessageBox.question(
            self, "Remove stack",
            f"Remove '{self._entries[row].name}' from stacks_config.json?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        )
        if ans == QMessageBox.StandardButton.Yes:
            del self._entries[row]
            self._refresh_table()

    def _save(self) -> None:
        save_entries(self._entries)
        QMessageBox.information(
            self, "Saved",
            "Stacks saved. Restart the app for changes to take full effect.",
        )


class _StackEntryDialog(QDialog):
    def __init__(self, parent: QWidget, existing: StackEntry | None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Stack entry")
        self.entry: StackEntry | None = None
        self.resize(560, 320)

        form = QFormLayout()
        self._name = QLineEdit(existing.name if existing else "")
        self._profile = _PathLine(existing.profile_path if existing else "", file_filter="JSON (*.json)")
        self._project = _PathLine(existing.project_path if existing else "", dir_only=True)
        self._db = _PathLine(existing.db_path if existing else "", file_filter="SQLite (*.db)")
        prev = (existing.service_names if existing else None) or ["", "", ""]
        self._svc_api = QLineEdit(prev[0])
        self._svc_bot = QLineEdit(prev[1])
        self._svc_list = QLineEdit(prev[2])
        form.addRow("Name", self._name)
        form.addRow("Profile JSON", self._profile)
        form.addRow("Project path", self._project)
        form.addRow("DB override (optional)", self._db)
        form.addRow("API service name", self._svc_api)
        form.addRow("Bot service name", self._svc_bot)
        form.addRow("Listener service name", self._svc_list)

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        name = self._name.text().strip()
        profile = self._profile.text().strip()
        project = self._project.text().strip()
        if not name or not profile or not project:
            QMessageBox.warning(self, "Required",
                                "Name, profile path, and project path are required.")
            return
        svc = [self._svc_api.text().strip(), self._svc_bot.text().strip(), self._svc_list.text().strip()]
        service_names = svc if all(svc) else []
        self.entry = StackEntry(
            name=name,
            profile_path=profile,
            project_path=project,
            db_path=self._db.text().strip(),
            service_names=service_names,
        )
        self.accept()


class _PathLine(QWidget):
    def __init__(self, initial: str, file_filter: str | None = None, dir_only: bool = False) -> None:
        super().__init__()
        self._line = QLineEdit(initial)
        self._file_filter = file_filter
        self._dir_only = dir_only
        btn = QPushButton("…")
        btn.setFixedWidth(32)
        btn.clicked.connect(self._pick)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._line, 1)
        layout.addWidget(btn)

    def text(self) -> str:
        return self._line.text()

    def setText(self, value: str) -> None:
        self._line.setText(value)

    def _pick(self) -> None:
        if self._dir_only:
            path = QFileDialog.getExistingDirectory(self, "Pick directory", self._line.text())
        else:
            path, _ = QFileDialog.getOpenFileName(self, "Pick file", self._line.text(), self._file_filter or "")
        if path:
            self._line.setText(path)


@dataclass(frozen=True)
class _Field:
    key: str
    label: str
    kind: str  # "str" | "int" | "float" | "bool" | "choice" | "secret"
    tooltip: str = ""
    opts: tuple = dc_field(default_factory=tuple)
    editable: bool = False  # for "choice": user can type a value not in the list


_LABEL_COL_WIDTH = 260


class _TuningTab(QWidget):
    """Structured editor for non-critical DB settings.

    Labels align across all sections via a fixed-width first column.
    Each field gets an ⓘ icon to its right; hover shows a tooltip
    explaining the value.
    """

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._widgets: dict[str, QWidget] = {}
        self._row_widgets: dict[str, tuple[QWidget, QWidget, QWidget]] = {}
        self._build_ui()
        self._load()
        self._wire_visibility()
        self._update_visibility()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._load()
        self._update_visibility()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)

        self._meta = QLabel()
        self._meta.setStyleSheet("color: #787b86;")
        layout.addWidget(self._meta)

        actions = QHBoxLayout()
        # Promote primary affordance — Save is the page's primary action.
        # Falls back to QPushButton if qfluentwidgets isn't importable.
        try:
            from qfluentwidgets import PrimaryPushButton
            save_btn = PrimaryPushButton("Save")
        except Exception:
            save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._load)
        actions.addWidget(save_btn)
        actions.addWidget(reload_btn)
        actions.addStretch()
        note = QLabel(
            "<span style='color:#787b86;'>changes apply on the next service restart</span>"
        )
        note.setTextFormat(Qt.TextFormat.RichText)
        actions.addWidget(note)
        layout.addLayout(actions)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        content = QWidget()
        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(8, 8, 8, 8)
        content_layout.setSpacing(14)

        for title, fields in _TUNING_SECTIONS:
            content_layout.addWidget(self._make_section_card_group(title, fields))

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def _make_section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #787b86; font-size: 11px; font-weight: 700; "
            "letter-spacing: 1.5px; padding-top: 4px; border-bottom: 1px solid #2a2e39;"
        )
        return lbl

    def _make_section_card_group(self, title: str, fields: list[_Field]) -> QWidget:
        """Build a Fluent SettingCardGroup with one SettingCard per field.

        Delegates the per-card construction to the shared
        ``_setting_cards.make_setting_card`` helper so this stays in
        lockstep with Profile → Editor (REVIEW.md follow-up).
        """
        from qfluentwidgets import FluentIcon, SettingCardGroup
        from src.gui.panels._setting_cards import (
            make_setting_card,
            truncate_subtitle,
        )
        group = SettingCardGroup(title)
        for f in fields:
            widget = self._make_widget(f)
            self._widgets[f.key] = widget
            subtitle = truncate_subtitle(f.tooltip)
            card = make_setting_card(
                icon=FluentIcon.EDIT if f.kind == "text" else FluentIcon.SETTING,
                title=f.label,
                subtitle=subtitle,
                widget=widget,
                kind="expand" if f.kind == "text" else "compact",
            )
            group.addSettingCard(card)
            # Visibility plumbing: just the card now (instead of the
            # old label/value/info triple).
            self._row_widgets[f.key] = (card, card, card)
        return group

    def _make_section_grid(self, fields: list[_Field]) -> QGridLayout:
        grid = QGridLayout()
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)
        grid.setColumnMinimumWidth(0, _LABEL_COL_WIDTH)
        grid.setColumnStretch(0, 0)
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(2, 0)
        for row, f in enumerate(fields):
            label = QLabel(f.label)
            label.setStyleSheet("padding: 4px 0;")  # color inherits from global QSS
            label.setFixedWidth(_LABEL_COL_WIDTH)
            widget = self._make_widget(f)
            self._widgets[f.key] = widget
            icon = self._info_icon(f.tooltip)
            grid.addWidget(label, row, 0)
            grid.addWidget(widget, row, 1)
            grid.addWidget(icon, row, 2)
            self._row_widgets[f.key] = (label, widget, icon)
        return grid

    def _wire_visibility(self) -> None:
        """Connect provider combos to visibility refresh."""
        w = self._widgets.get("ai_provider")
        if isinstance(w, QComboBox):
            w.currentTextChanged.connect(lambda _t: self._update_visibility())

    def _resolved_provider(self, key: str) -> str:
        w = self._widgets.get(key)
        if isinstance(w, QComboBox):
            return (w.currentText() or w.currentData() or "").strip().lower()
        return ""

    def _update_visibility(self) -> None:
        """Show only the model fields relevant to the selected provider."""
        if not self._row_widgets:
            return
        main_provider = self._resolved_provider("ai_provider") or "anthropic"

        rules = {
            "anthropic_model": main_provider == "anthropic",
            "openai_model": main_provider == "openai",
            "ai_triage_model": main_provider == "anthropic",
            "openai_triage_model": main_provider == "openai",
        }
        for key, visible in rules.items():
            row = self._row_widgets.get(key)
            if row is None:
                continue
            for w in row:
                w.setVisible(visible)

    def _info_icon(self, tooltip: str) -> QLabel:
        icon = QLabel("ⓘ")
        icon.setStyleSheet(
            "color: #787b86; padding: 0 6px; font-size: 14px;"
        )
        icon.setCursor(Qt.CursorShape.WhatsThisCursor)
        icon.setToolTip(tooltip or "(no description yet)")
        return icon

    def _make_widget(self, f: _Field) -> QWidget:
        if f.kind == "bool":
            from qfluentwidgets import SwitchButton
            sw = SwitchButton()
            sw.setToolTip(f.tooltip)
            return sw
        if f.kind == "int":
            sb = QSpinBox()
            lo, hi = f.opts if f.opts else (0, 1_000_000)
            sb.setRange(lo, hi)
            sb.setToolTip(f.tooltip)
            return sb
        if f.kind == "float":
            dsb = QDoubleSpinBox()
            lo, hi, step = f.opts if f.opts else (0.0, 1_000_000.0, 0.1)
            dsb.setRange(lo, hi)
            dsb.setSingleStep(step)
            dsb.setDecimals(2)
            dsb.setToolTip(f.tooltip)
            return dsb
        if f.kind == "choice":
            # Opts can be either:
            #   - a list of bare values: ["scalp", "intraday", ...] —
            #     display text = value (legacy form)
            #   - a list of (label, value) tuples: [("Signal zone",
            #     "signal_zone"), ...] — display the friendly label,
            #     persist the value
            # Both forms coexist because most existing fields only need
            # the bare-value form; the BE settings introduce labels.
            cb = QComboBox()
            items = list(f.opts[0]) if f.opts else []
            for entry in items:
                if isinstance(entry, tuple) and len(entry) == 2:
                    label, value = entry
                    cb.addItem(str(label), value)
                else:
                    cb.addItem(str(entry), entry)
            cb.setEditable(f.editable)
            if f.editable:
                cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            cb.setToolTip(f.tooltip)
            # Size to the longest display label so values like
            # "gpt-5-nano-2025-08-07" or "Low end of zone (entry_low)"
            # don't get clipped behind the dropdown arrow.
            fm = cb.fontMetrics()
            display_strings = [
                str(e[0]) if isinstance(e, tuple) and len(e) == 2 else str(e)
                for e in items
            ]
            longest = max(
                (fm.horizontalAdvance(s) for s in display_strings),
                default=0,
            )
            cb.setMinimumWidth(max(220, longest + 50))
            return cb
        if f.kind == "secret":
            container = QWidget()
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            row.setSpacing(6)
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setObjectName("secret_input")
            edit.setToolTip(f.tooltip)
            edit.setMinimumWidth(260)
            row.addWidget(edit, 1)
            # Eye-icon toggle — qfluentwidgets TransparentToolButton renders
            # cleanly on both light/dark palettes (the previous textual
            # "show" QPushButton was invisible against the SettingCard
            # background on some themes).
            try:
                from qfluentwidgets import (
                    FluentIcon as _FIF,
                    TransparentToggleToolButton,
                )
                btn = TransparentToggleToolButton(_FIF.VIEW)
                btn.setFixedSize(32, 28)
                btn.setToolTip("Show / hide secret")

                def _on_toggle(checked: bool, e=edit, b=btn) -> None:
                    e.setEchoMode(
                        QLineEdit.EchoMode.Normal if checked
                        else QLineEdit.EchoMode.Password
                    )
                    try:
                        b.setIcon(_FIF.HIDE if checked else _FIF.VIEW)
                    except Exception:
                        pass

                btn.toggled.connect(_on_toggle)
            except Exception:
                btn = QPushButton("Show")
                btn.setCheckable(True)
                btn.setMinimumWidth(72)
                btn.toggled.connect(
                    lambda on, e=edit, b=btn: (
                        e.setEchoMode(
                            QLineEdit.EchoMode.Normal if on
                            else QLineEdit.EchoMode.Password
                        ),
                        b.setText("Hide" if on else "Show"),
                    )
                )
            row.addWidget(btn)
            return container
        if f.kind == "text":
            pte = QPlainTextEdit()
            pte.setToolTip(f.tooltip)
            pte.setMinimumHeight(160)
            pte.setMaximumHeight(260)
            return pte
        edit = QLineEdit()
        edit.setToolTip(f.tooltip)
        return edit

    def _load(self) -> None:
        self._meta.setText(f"DB: {self._stack.db_path}")
        db_path = self._stack.db_path
        for _title, fields in _TUNING_SECTIONS:
            for f in fields:
                w = self._widgets.get(f.key)
                if w is None:
                    continue
                if f.kind == "bool":
                    # bool widgets are SwitchButton (or QCheckBox fallback);
                    # both expose setChecked/isChecked the same way.
                    w.setChecked(db_settings.get_bool(db_path, f.key, False))
                elif f.kind == "int":
                    assert isinstance(w, QSpinBox)
                    w.setValue(db_settings.get_int(db_path, f.key, 0))
                elif f.kind == "float":
                    assert isinstance(w, QDoubleSpinBox)
                    w.setValue(db_settings.get_float(db_path, f.key, 0.0))
                elif f.kind == "choice":
                    assert isinstance(w, QComboBox)
                    val = db_settings.get_str(db_path, f.key, "")
                    idx = w.findData(val)
                    if idx >= 0:
                        w.setCurrentIndex(idx)
                    elif f.editable and val:
                        w.setEditText(val)
                    elif w.count() > 0:
                        w.setCurrentIndex(0)
                elif f.kind == "secret":
                    edit = w.findChild(QLineEdit, "secret_input")
                    if edit is not None:
                        # Defensive decrypt: DPAPI rejects ciphertext that
                        # was saved under a different security context
                        # (password reset, machine restore, etc.) — we
                        # must not crash the whole Tuning tab when that
                        # happens. Show the field blank instead; the
                        # placeholder hints at the recovery action.
                        try:
                            edit.setText(db_settings.get_str(db_path, f.key, ""))
                        except OSError:
                            edit.setText("")
                            edit.setPlaceholderText(
                                "(saved value unreadable in this session — "
                                "paste key to re-encrypt)"
                            )
                elif f.kind == "text":
                    assert isinstance(w, QPlainTextEdit)
                    w.setPlainText(db_settings.get_str(db_path, f.key, ""))
                else:
                    assert isinstance(w, QLineEdit)
                    w.setText(db_settings.get_str(db_path, f.key, ""))

    def _save(self) -> None:
        db_path = self._stack.db_path
        try:
            for _title, fields in _TUNING_SECTIONS:
                for f in fields:
                    w = self._widgets.get(f.key)
                    if w is None:
                        continue
                    if f.kind == "bool":
                        db_settings.set_bool(db_path, f.key, w.isChecked())
                    elif f.kind == "int":
                        db_settings.set_int(db_path, f.key, w.value())
                    elif f.kind == "float":
                        db_settings.set_str(db_path, f.key, str(w.value()))
                    elif f.kind == "choice":
                        if f.editable:
                            db_settings.set_str(db_path, f.key, w.currentText().strip())
                        else:
                            val = w.currentData()
                            db_settings.set_str(db_path, f.key, str(val) if val is not None else "")
                    elif f.kind == "secret":
                        edit = w.findChild(QLineEdit, "secret_input")
                        if edit is not None:
                            new_val = edit.text()
                            # Empty field = "keep the existing saved value".
                            # An operator scanning the Tuning tab without
                            # typing must not silently wipe their API key.
                            # To explicitly clear a secret, they need to
                            # remove the row via SQL or delete the stack.
                            if new_val:
                                db_settings.set_str(db_path, f.key, new_val)
                    elif f.kind == "text":
                        db_settings.set_str(db_path, f.key, w.toPlainText())
                    else:
                        db_settings.set_str(db_path, f.key, w.text())
        except Exception as e:
            QMessageBox.critical(self, "Save failed", f"{type(e).__name__}: {e}")
            return
        QMessageBox.information(
            self, "Saved", "Settings saved. Restart services to apply runtime changes."
        )


_TUNING_SECTIONS: list[tuple[str, list[_Field]]] = [
    ("NETWORK", [
        _Field("api_host", "API host", "str",
               tooltip=(
                   "Network interface the API server binds to. 127.0.0.1 means "
                   "localhost only (the EA on this machine can reach it; nothing "
                   "external can). Use 0.0.0.0 to expose on the LAN — only do "
                   "that with a non-empty EA shared token."
               )),
        _Field("api_port", "API port", "int",
               tooltip=(
                   "TCP port the API listens on. Default 8765. The EA's "
                   "ApiBaseUrl input must match (e.g. http://127.0.0.1:8765). "
                   "Pick a different port per stack if running multiple."
               ),
               opts=(1, 65535)),
        _Field("ea_shared_token", "EA shared token", "secret",
               tooltip=(
                   "Optional secret. When set, every EA HTTP request must "
                   "include X-EA-Token: <this value> or the API returns 401. "
                   "Leave blank for unauthenticated dev mode (localhost-only)."
               )),
    ]),
    ("AI MODELS", [
        _Field("ai_provider", "Provider", "choice",
               tooltip=(
                   "Which AI service the interpreter calls. Anthropic = Claude "
                   "(higher accuracy, pricier). OpenAI = GPT (cheaper, faster "
                   "for simple channels)."
               ),
               opts=(["anthropic", "openai"],)),
        _Field("anthropic_api_key", "Anthropic API key", "secret",
               tooltip=(
                   "Claude API key (sk-ant-…). Stored DPAPI-encrypted in the "
                   "stack's settings table. Leave blank to keep the existing "
                   "saved key — typing a new value replaces it. If decryption "
                   "fails (e.g. after a Windows password reset), the field "
                   "shows blank; paste the key again to re-encrypt under the "
                   "current session."
               )),
        _Field("openai_api_key", "OpenAI API key", "secret",
               tooltip=(
                   "OpenAI API key (sk-…). Same storage + recovery semantics "
                   "as the Anthropic key above. Required for the embedding "
                   "matcher's Layer 2 even when AI_PROVIDER is set to "
                   "anthropic — the matcher uses OpenAI text-embedding-3-small "
                   "regardless of which interpreter you've chosen."
               )),
        _Field("anthropic_model", "Anthropic interpreter", "choice",
               tooltip=(
                   "Claude model used as the main interpreter — the step that "
                   "parses each kept message into action JSON. Sonnet 4.6 is "
                   "the default; Opus 4.7 is more accurate but ~5x pricier; "
                   "Haiku is too small for ambiguous interpretation."
               ),
               opts=([
                   "claude-opus-4-7",
                   "claude-sonnet-4-6",
                   "claude-haiku-4-5-20251001",
               ],),
               editable=False),
        _Field("openai_model", "OpenAI interpreter", "choice",
               tooltip=(
                   "OpenAI model used as the main interpreter. gpt-5 is the "
                   "default; gpt-5-mini cuts cost ~6x at a small accuracy hit; "
                   "gpt-5-nano is too small for compound message interpretation."
               ),
               opts=(["gpt-5", "gpt-5-mini", "gpt-5-nano"],),
               editable=False),
    ]),
    ("AI TRIAGE", [
        _Field("ai_triage_enabled", "Triage enabled", "bool",
               tooltip=(
                   "Two-stage pipeline: a cheap model first decides ignore-vs-keep "
                   "for each message before the expensive interpreter runs. Cuts "
                   "interpreter call volume ~70% on noisy channels."
               )),
        _Field("ai_triage_model", "Anthropic triage model", "choice",
               tooltip=(
                   "Claude model used for the triage pre-filter when provider = "
                   "Anthropic. Haiku 4.5 is ~30x cheaper than Sonnet and still "
                   "accurate enough for the binary ignore/keep decision."
               ),
               opts=([
                   "claude-haiku-4-5-20251001",
                   "claude-sonnet-4-6",
                   "claude-opus-4-7",
               ],),
               editable=False),
        _Field("openai_triage_model", "OpenAI triage model", "choice",
               tooltip=(
                   "OpenAI model used for triage when provider = OpenAI. "
                   "gpt-5-nano is the cheapest tier."
               ),
               opts=(["gpt-5-nano", "gpt-5-mini", "gpt-5"],),
               editable=False),
    ]),
    ("AI THINKING", [
        _Field("ai_thinking_enabled", "Extended thinking", "bool",
               tooltip=(
                   "Lets the model reason step-by-step before producing JSON. "
                   "Materially improves parsing of ambiguous shorthand signals "
                   "(e.g. 'ستوبك 26' decoded against MARKET mid). Costs "
                   "thinking tokens — see budget below."
               )),
        _Field("ai_thinking_budget_tokens", "Thinking budget", "int",
               tooltip=(
                   "Max tokens the model can spend on hidden reasoning before "
                   "answering. 4000 is a safe default. Higher = more careful "
                   "but slower and pricier. You only pay for tokens actually used."
               ),
               opts=(256, 64000)),
    ]),
    ("SIGNAL MEMORY", [
        _Field("signal_memory_enabled", "Enabled", "bool",
               tooltip=(
                   "Distilled AI summaries of prior non-ignore messages are "
                   "accumulated and fed back into every call instead of the raw "
                   "20-message chat window. Cleared when an OPEN action lands. "
                   "Disable to use raw recent-chat."
               )),
        _Field("signal_memory_max_entries", "Max entries", "int",
               tooltip=(
                   "How many distilled summaries to keep in the rolling buffer. "
                   "10 is enough for most channels."
               ),
               opts=(1, 100)),
        _Field("signal_memory_max_age_hours", "Max age (hours)", "int",
               tooltip=(
                   "Entries older than this are dropped from the buffer. "
                   "Prevents stale context bleeding into fresh signals."
               ),
               opts=(1, 168)),
    ]),
    ("DEDUP / BACKFILL", [
        _Field("fingerprint_band_price", "Fingerprint band (price)", "float",
               tooltip=(
                   "Resent/quoted signals within this price band collapse into "
                   "one fingerprint bucket — prevents executing the same trade "
                   "twice when a channel re-posts the same setup."
               ),
               opts=(0.0, 100.0, 0.5)),
        _Field("fingerprint_window_hours", "Fingerprint window (hours)", "int",
               tooltip=(
                   "How far back to look for a duplicate fingerprint. "
                   "Newer-than-this with matching params = skip."
               ),
               opts=(1, 168)),
        _Field("backfill_max_age_min", "Backfill max age (min)", "int",
               tooltip=(
                   "On listener reconnect, missed Telegram messages older than "
                   "this are archived (is_backfill=1) but NOT processed. Price "
                   "has likely moved too far for the signal to be safe."
               ),
               opts=(0, 1440)),
    ]),
    ("CHANNEL PROFILE", [
        _Field("channel_profile", "Active profile", "str",
               tooltip=(
                   "Which channels/<name>.json profile to load at listener "
                   "startup. Determines the AI prompt's vocabulary table, "
                   "worked examples, and language. Must match a file in channels/."
               )),
    ]),
    ("RISK MANAGEMENT", [
        _Field("max_sl_loss_percent", "Max SL loss (% of balance)", "float",
               tooltip=(
                   "Hard cap on the dollar loss a single trade can take "
                   "if its stop-loss hits. Computed as "
                   "(SL distance × tick value × lot size) vs account "
                   "balance × this percent. If the baseline lot would "
                   "exceed the cap, lot size is shrunk to fit. If even "
                   "minLot would exceed the cap (signal's SL is too "
                   "wide for this account), the trade is rejected with "
                   "reason 'sl_too_wide_for_max_risk_pct'. Default 1.0%. "
                   "Set 0 to disable the cap entirely (lot sizing then "
                   "uses LotsPer100Balance + MaxLotsPerSignal only)."
               ),
               opts=(0.0, 20.0, 0.1)),
        _Field("be_target", "Break-even base price", "choice",
               tooltip=(
                   "Which price to use as the BE anchor when MOVE_SL_BE "
                   "fires.\n\n"
                   "• Signal zone — a position within the signal's "
                   "entry_low/entry_high zone (see next setting). "
                   "Default.\n"
                   "• Entry fill price — the position's actual broker "
                   "fill price (legacy behavior).\n\n"
                   "The cost offset (commission + swap) is added on "
                   "top of whichever base is chosen, so 'true "
                   "break-even' is always honored. Falls back to entry "
                   "fill when signal_zone has no zone data (e.g. naked "
                   "OPEN_INSTANT before ATTACH_SIGNAL)."
               ),
               opts=([
                   ("Signal zone", "signal_zone"),
                   ("Entry fill price", "entry_fill"),
               ],),
               editable=False),
        _Field("be_signal_zone_position", "BE position in signal zone", "choice",
               tooltip=(
                   "When 'Break-even base price' = Signal zone, this "
                   "picks where in the entry_low/entry_high zone the "
                   "BE anchor sits.\n\n"
                   "• Low — entry_low (for a BUY: more room / wider "
                   "SL; for a SELL: tighter SL / more profit locked).\n"
                   "• Middle — midpoint of the zone (neutral default).\n"
                   "• High — entry_high (mirror of Low).\n\n"
                   "Ignored when base price = Entry fill price."
               ),
               opts=([
                   ("Low end of zone (entry_low)", "low"),
                   ("Middle of zone", "mid"),
                   ("High end of zone (entry_high)", "high"),
               ],),
               editable=False),
    ]),
    ("INSTANT OPEN FALLBACK", [
        _Field("instant_risk_percent", "Emergency SL risk (%)", "float",
               tooltip=(
                   "When an OPEN_INSTANT signal fires (channel sent a "
                   "bare 'BUY' / 'SELL' before the structured signal), "
                   "the EA opens at market with an emergency SL placed "
                   "so hitting it loses this percent of balance. The "
                   "real SL/TPs swap in via ATTACH_SIGNAL within a few "
                   "minutes — this only governs the naked period. "
                   "Default 1.0%."
               ),
               opts=(0.1, 10.0, 0.1)),
        _Field("instant_timeout_minutes", "Wait for ATTACH_SIGNAL (min)", "int",
               tooltip=(
                   "How long the EA waits for ATTACH_SIGNAL after an "
                   "OPEN_INSTANT before giving up and installing the "
                   "fallback TP + trailing SL. If you set 0 the "
                   "fallback never engages — naked positions ride on "
                   "the emergency SL until the structured signal "
                   "arrives or the operator closes manually. Default 2."
               ),
               opts=(0, 120)),
        _Field("instant_tp_multiplier", "Fallback TP × SL distance", "float",
               tooltip=(
                   "When the wait timeout fires, the fallback TP is set "
                   "at this multiple of the emergency-SL distance from "
                   "entry. 2.0 = 2:1 reward-to-risk. Higher = bigger "
                   "expected move per trade, fewer hits. Default 2.0."
               ),
               opts=(0.5, 10.0, 0.1)),
        _Field("instant_trail_points", "Fallback trail (MT5 points)", "int",
               tooltip=(
                   "Once the fallback is armed, the SL ratchets along "
                   "behind price at this distance in MT5 points. On "
                   "XAUUSD with the standard tick scheme, 1000 points "
                   "≈ $10.00 — roughly 1× H1 ATR in calm regimes. "
                   "Larger = more room for normal pullbacks; smaller "
                   "= locks profit faster but stops out more often on "
                   "noise. Default 1000."
               ),
               opts=(10, 10000)),
    ]),
    ("TRADING STYLE", [
        _Field("trading_style", "Default style", "choice",
               tooltip=(
                   "Holding-period intent for this channel. Drives the AI "
                   "evaluator's rubric weights, the probability horizons it "
                   "scores (15m/1h/4h for scalp, up to 7d for position), and "
                   "which data feeds need to be fresh. Scalp leans on session "
                   "phase and level proximity; intraday balances trend and "
                   "macro; swing leans on real yields, DXY trend, and COT; "
                   "position is macro-thesis-dominant. Pick the style that "
                   "matches this channel's typical hold."
               ),
               opts=(["scalp", "intraday", "swing", "position"],),
               editable=False),
        _Field("trading_style_ai_override", "Allow AI per-signal override", "bool",
               tooltip=(
                   "When ON, the AI interpreter may override the default style "
                   "for a single signal if the message text declares one "
                   "(\"اسكالب فقط\" → scalp, \"صفقة طويلة\" → swing). When OFF, "
                   "every signal from this channel uses the default style "
                   "regardless of what the message says. Recommended: ON — "
                   "channels that mix styles signal it explicitly."
               )),
    ]),
    ("MISC", [
        _Field("default_auto_execute_delay_sec", "Auto-execute delay (sec)", "int",
               tooltip=(
                   "Grace window between action insertion and promotion to "
                   "'sent'. 0 = promote on the next tick (no human approval "
                   "gate). The system was designed for 0; raise only if you "
                   "want manual approval before every trade."
               ),
               opts=(0, 3600)),
        _Field("recent_chat_window", "Recent chat window (msgs)", "int",
               tooltip=(
                   "Number of prior messages sent to the AI as context when "
                   "signal_memory_enabled is off. 20 is the default."
               ),
               opts=(1, 200)),
    ]),
]


class _ServicesTab(QWidget):
    # Per-service start/stop/restart lives in the top-right services bar
    # (the canonical "one place" for that). This tab keeps the read-only
    # state view + Install/Uninstall lifecycle buttons.
    HEADERS = ("Service", "State")

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._build_ui()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        info = QLabel(
            "<span style='color:#787b86;'>NSSM service lifecycle for the active stack  ·  "
            "stop/start may need admin privileges</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        # Install/Uninstall actions affect every service in this stack at
        # once. Both require admin (SCM modifications), so they shell out
        # via run_elevated_python and the user gets one UAC prompt per
        # click. Kept in the Services tab so all lifecycle controls live
        # next to the per-service table they affect.
        actions_row = QHBoxLayout()
        actions_row.setContentsMargins(0, 4, 0, 4)
        self._install_btn = QPushButton("Install services")
        self._install_btn.setToolTip(
            "Register the three NSSM services for this stack (api, bot, "
            "listener). Triggers a UAC prompt."
        )
        self._install_btn.clicked.connect(self._on_install_services)
        self._uninstall_btn = QPushButton("Uninstall services")
        self._uninstall_btn.setToolTip(
            "Stop and unregister all three NSSM services for this stack. "
            "Triggers a UAC prompt. Logs and the database are not touched."
        )
        self._uninstall_btn.clicked.connect(self._on_uninstall_services)
        self._export_diag_btn = QPushButton("Export diagnostics…")
        self._export_diag_btn.setToolTip(
            "Bundle logs, sanitized DB, service crash logs, and optionally "
            "MT5 terminal/experts logs into a ZIP for incident triage. "
            "Filter by time window so the bundle stays small. Secrets are "
            "blanked before zipping."
        )
        self._export_diag_btn.clicked.connect(self._on_export_diagnostics)
        actions_row.addWidget(self._install_btn)
        actions_row.addWidget(self._uninstall_btn)
        actions_row.addWidget(self._export_diag_btn)
        actions_row.addStretch()
        layout.addLayout(actions_row)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(list(self.HEADERS))
        self._table.verticalHeader().setVisible(False)
        from src.gui.panels._table_utils import apply_full_width_headers
        apply_full_width_headers(
            self._table,
            content_columns=tuple(range(self._table.columnCount() - 1)),
        )
        self._table.setSelectionMode(QAbstractItemView.SelectionMode.NoSelection)
        self._table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self._table.setAlternatingRowColors(True)
        self._table.setShowGrid(False)
        layout.addWidget(self._table, 1)

    def _refresh(self) -> None:
        services = self._stack.service_names
        self._table.setRowCount(len(services))
        for i, svc in enumerate(services):
            self._table.setItem(i, 0, QTableWidgetItem(svc))
            running = nssm_client.service_running(svc)
            exists = nssm_client.service_exists(svc)
            state = "RUNNING" if running else ("STOPPED" if exists else "NOT INSTALLED")
            self._table.setItem(i, 1, QTableWidgetItem(state))

    def _on_export_diagnostics(self) -> None:
        """Open the diagnostics-bundle dialog scoped to the active stack."""
        from src.gui.windows.diagnostics_export_dialog import DiagnosticsExportDialog
        dlg = DiagnosticsExportDialog(self._stack, self)
        dlg.exec()

    def _on_install_services(self) -> None:
        """(Re-)register the three NSSM services for the active stack.

        Mirrors the bootstrap path used at GUI startup but is triggered
        on demand from Settings so the operator can fix a partial
        install (or re-register after editing the db path) without
        relaunching the GUI.
        """
        from src.gui.services.elevation import run_elevated_python
        stack = self._stack
        ok = run_elevated_python(
            "src.gui.helpers.bootstrap_services_install",
            [
                stack.name,
                str(stack.project_path),
                *stack.service_names,
                str(stack.db_path),
            ],
        )
        if not ok:
            QMessageBox.warning(
                self,
                "Install services",
                "Elevation was cancelled or the helper failed to launch. "
                "No changes were made.",
            )
            return
        # The helper runs out-of-process; nudge the table a couple of
        # times so the operator sees the new state without waiting for
        # the 5s timer.
        QTimer.singleShot(1500, self._refresh)
        QTimer.singleShot(4000, self._refresh)
        QMessageBox.information(
            self,
            "Install services",
            "Service registration requested. The table will refresh shortly.",
        )

    def _on_uninstall_services(self) -> None:
        """Stop + unregister the three NSSM services for the active stack."""
        stack = self._stack
        confirm = QMessageBox.question(
            self,
            "Uninstall services",
            "This will stop and unregister the following Windows services "
            f"for stack '{stack.name}':\n\n  • "
            + "\n  • ".join(stack.service_names)
            + "\n\nLogs and the database are not touched. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        from src.gui.services.elevation import run_elevated_python
        ok = run_elevated_python(
            "src.gui.helpers.bootstrap_services_uninstall",
            list(stack.service_names),
        )
        if not ok:
            QMessageBox.warning(
                self,
                "Uninstall services",
                "Elevation was cancelled or the helper failed to launch. "
                "No changes were made.",
            )
            return
        QTimer.singleShot(1500, self._refresh)
        QTimer.singleShot(4000, self._refresh)
        QMessageBox.information(
            self,
            "Uninstall services",
            "Service removal requested. The table will refresh shortly.",
        )
