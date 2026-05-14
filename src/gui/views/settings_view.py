"""SETTINGS view: tabs for Channels (stacks_config), .env, and Services."""
from __future__ import annotations

from dataclasses import dataclass, field as dc_field

from PySide6.QtCore import Qt, QTimer
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
from src.gui.services.bootstrap import BootstrapManager
from src.gui.services.env_io import EnvLine, is_secret, parse_env, write_env
from src.gui.services.stack_registry import Stack
from src.gui.services.stacks_config_io import StackEntry, load_entries, save_entries, stacks_config_path
from src.gui.services.telegram_session import session_path
from src.gui.windows.telegram_wizard import TelegramWizard


class SettingsView(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._bootstrap: BootstrapManager | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(10)

        title_row = QHBoxLayout()
        title = QLabel("<span style='font-size:16px; font-weight:700;'>SETTINGS</span>")
        title.setTextFormat(Qt.TextFormat.RichText)
        title_row.addWidget(title)
        self._status_label = QLabel("")
        self._status_label.setStyleSheet("color: #586e75; padding-left: 16px;")
        title_row.addWidget(self._status_label)
        title_row.addStretch()
        self._wizard_btn = QPushButton("Setup wizard")
        self._wizard_btn.clicked.connect(self._open_telegram_wizard)
        title_row.addWidget(self._wizard_btn)
        self._start_btn = QPushButton("Start services")
        self._start_btn.clicked.connect(self._on_start_services)
        title_row.addWidget(self._start_btn)
        self._stop_btn = QPushButton("Stop services")
        self._stop_btn.clicked.connect(self._on_stop_services)
        title_row.addWidget(self._stop_btn)
        layout.addLayout(title_row)

        self._tabs = QTabWidget()
        self._channels_tab = _ChannelsTab()
        self._tuning_tab = _TuningTab(stack)
        self._services_tab = _ServicesTab(stack)
        self._tabs.addTab(self._channels_tab, "Channels")
        self._tabs.addTab(self._tuning_tab, "Tuning")
        self._tabs.addTab(self._services_tab, "Services")
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
        TelegramWizard(self._stack, self).exec()
        self._tuning_tab.rebind(self._stack)
        self._refresh_status()

    def _refresh_status(self) -> None:
        missing = db_settings.missing_critical_keys(self._stack.db_path)
        if missing:
            self._status_label.setText(
                f"<span style='color:#dc322f;'>setup incomplete · "
                f"{len(missing)} critical key(s) missing</span>"
            )
            self._status_label.setTextFormat(Qt.TextFormat.RichText)
            self._start_btn.setEnabled(False)
            self._start_btn.setToolTip(
                "Setup wizard must complete first: missing " + ", ".join(missing)
            )
        else:
            running = sum(
                1 for svc in self._stack.service_names if nssm_client.service_running(svc)
            )
            total = len(self._stack.service_names)
            self._status_label.setText(
                f"<span style='color:#586e75;'>services: {running}/{total} running</span>"
            )
            self._status_label.setTextFormat(Qt.TextFormat.RichText)
            self._start_btn.setEnabled(running < total)
            self._start_btn.setToolTip("")
        any_running = any(
            nssm_client.service_running(svc) for svc in self._stack.service_names
        )
        self._stop_btn.setEnabled(any_running)

    def _on_start_services(self) -> None:
        if self._bootstrap is not None:
            return
        self._start_btn.setEnabled(False)
        self._bootstrap = BootstrapManager([self._stack], parent=self)
        self._bootstrap.all_completed.connect(self._on_bootstrap_done)
        self._bootstrap.step_failed.connect(self._on_bootstrap_failed)
        self._bootstrap.start()

    def _on_bootstrap_done(self) -> None:
        self._bootstrap = None
        QMessageBox.information(self, "Services", "Services started.")
        self._services_tab.rebind(self._stack)
        self._refresh_status()

    def _on_bootstrap_failed(self, stack: str, step: str, err: str) -> None:
        QMessageBox.warning(
            self, "Service step failed",
            f"{stack} · {step} · {err}",
        )

    def _on_stop_services(self) -> None:
        for svc in self._stack.service_names:
            if nssm_client.service_running(svc):
                nssm_client.nssm_stop(svc)
        self._services_tab.rebind(self._stack)
        self._refresh_status()


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
            f"<span style='color:#93a1a1;'>file: {stacks_config_path()}  ·  "
            "edits require an app restart to take effect</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        info.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        layout.addWidget(info)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(list(self.HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
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
            "Channels saved. Restart the app for changes to take full effect.",
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
        self._meta.setStyleSheet("color: #93a1a1;")
        layout.addWidget(self._meta)

        actions = QHBoxLayout()
        save_btn = QPushButton("Save")
        save_btn.clicked.connect(self._save)
        reload_btn = QPushButton("Reload")
        reload_btn.clicked.connect(self._load)
        actions.addWidget(save_btn)
        actions.addWidget(reload_btn)
        actions.addStretch()
        note = QLabel(
            "<span style='color:#586e75;'>changes apply on the next service restart</span>"
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
            content_layout.addWidget(self._make_section_label(title))
            content_layout.addLayout(self._make_section_grid(fields))

        content_layout.addStretch()
        scroll.setWidget(content)
        layout.addWidget(scroll, 1)

    def _make_section_label(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setStyleSheet(
            "color: #586e75; font-size: 11px; font-weight: 700; "
            "letter-spacing: 1.5px; padding-top: 4px; border-bottom: 1px solid #eee8d5;"
        )
        return lbl

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
            label.setStyleSheet("color: #073642; padding: 4px 0;")
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
        for key in ("ai_provider", "classifier_provider"):
            w = self._widgets.get(key)
            if isinstance(w, QComboBox):
                w.currentTextChanged.connect(lambda _t: self._update_visibility())

    def _resolved_provider(self, key: str) -> str:
        w = self._widgets.get(key)
        if isinstance(w, QComboBox):
            return (w.currentText() or w.currentData() or "").strip().lower()
        return ""

    def _update_visibility(self) -> None:
        """Show only the model fields relevant to the selected providers."""
        if not self._row_widgets:
            return
        main_provider = self._resolved_provider("ai_provider") or "anthropic"
        classifier_override = self._resolved_provider("classifier_provider")
        effective_classifier = classifier_override or main_provider

        rules = {
            "anthropic_model": main_provider == "anthropic",
            "openai_model": main_provider == "openai",
            "ai_triage_model": main_provider == "anthropic",
            "openai_triage_model": main_provider == "openai",
            "classifier_anthropic_model": effective_classifier == "anthropic",
            "classifier_openai_model": effective_classifier == "openai",
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
            "color: #93a1a1; padding: 0 6px; font-size: 14px;"
        )
        icon.setCursor(Qt.CursorShape.WhatsThisCursor)
        icon.setToolTip(tooltip or "(no description yet)")
        return icon

    def _make_widget(self, f: _Field) -> QWidget:
        if f.kind == "bool":
            cb = QCheckBox()
            cb.setToolTip(f.tooltip)
            return cb
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
            cb = QComboBox()
            for v in (f.opts[0] if f.opts else []):
                cb.addItem(str(v), v)
            cb.setEditable(f.editable)
            if f.editable:
                cb.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
            cb.setToolTip(f.tooltip)
            return cb
        if f.kind == "secret":
            container = QWidget()
            row = QHBoxLayout(container)
            row.setContentsMargins(0, 0, 0, 0)
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setObjectName("secret_input")
            edit.setToolTip(f.tooltip)
            row.addWidget(edit, 1)
            btn = QPushButton("show")
            btn.setCheckable(True)
            btn.setFixedWidth(64)
            btn.toggled.connect(
                lambda on, e=edit, b=btn: (
                    e.setEchoMode(QLineEdit.EchoMode.Normal if on else QLineEdit.EchoMode.Password),
                    b.setText("hide" if on else "show"),
                )
            )
            row.addWidget(btn)
            return container
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
                    assert isinstance(w, QCheckBox)
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
                        edit.setText(db_settings.get_str(db_path, f.key, ""))
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
                            db_settings.set_str(db_path, f.key, edit.text())
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
        _Field("anthropic_model", "Anthropic interpreter", "choice",
               tooltip=(
                   "Claude model used as the main interpreter — the step that "
                   "parses each kept message into action JSON. Sonnet 4.6 is "
                   "the default; Opus 4.7 is more accurate but ~5x pricier; "
                   "Haiku is too small for ambiguous interpretation. The field "
                   "is editable for new models Anthropic releases later."
               ),
               opts=(["claude-sonnet-4-6", "claude-opus-4-7", "claude-haiku-4-5-20251001"],),
               editable=True),
        _Field("openai_model", "OpenAI interpreter", "choice",
               tooltip=(
                   "OpenAI model used as the main interpreter. gpt-5 is the "
                   "default; gpt-5-mini cuts cost ~6x at a small accuracy hit; "
                   "gpt-5-nano is too small for compound message interpretation. "
                   "Editable for newer models."
               ),
               opts=(["gpt-5", "gpt-5-mini", "gpt-5-nano"],),
               editable=True),
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
               opts=(["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],),
               editable=True),
        _Field("openai_triage_model", "OpenAI triage model", "choice",
               tooltip=(
                   "OpenAI model used for triage when provider = OpenAI. "
                   "gpt-5-nano is the cheapest tier."
               ),
               opts=(["gpt-5-nano", "gpt-5-mini", "gpt-5"],),
               editable=True),
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
    ("CLASSIFIER (PROFILE WIZARD)", [
        _Field("classifier_batch_size", "Batch size", "int",
               tooltip=(
                   "How many messages to send to the model in one classify "
                   "call. Larger = fewer round-trips but more risk of the "
                   "model losing focus across items. 10 is the safe default."
               ),
               opts=(1, 50)),
        _Field("classifier_concurrency", "Concurrency", "choice",
               tooltip=(
                   "How many batches to fire in parallel. Each batch is a "
                   "separate HTTP request; higher values complete the run "
                   "faster but risk hitting provider rate limits. 4 is a "
                   "safe default for Haiku free tier; raise to 8-10 for "
                   "paid tiers or OpenAI."
               ),
               opts=(["1", "2", "4", "6", "8", "10"],)),
        _Field("classifier_provider", "Provider", "choice",
               tooltip=(
                   "Which AI service runs the discovery classifier. Can "
                   "differ from the interpreter — e.g. use cheap OpenAI "
                   "nano for classification while the live interpreter "
                   "stays on Anthropic Sonnet."
               ),
               opts=(["anthropic", "openai"],)),
        _Field("classifier_anthropic_model", "Anthropic classifier model", "choice",
               tooltip=(
                   "Claude model used by the classifier when its provider "
                   "resolves to anthropic. Default Haiku 4.5 — cheapest tier "
                   "that still classifies 14 buckets reliably."
               ),
               opts=(["claude-haiku-4-5-20251001", "claude-sonnet-4-6"],),
               editable=True),
        _Field("classifier_openai_model", "OpenAI classifier model", "choice",
               tooltip=(
                   "OpenAI model used by the classifier when its provider "
                   "resolves to openai. Default gpt-5-nano — cheapest tier."
               ),
               opts=(["gpt-5-nano", "gpt-5-mini", "gpt-5"],),
               editable=True),
    ]),
]


class _ServicesTab(QWidget):
    HEADERS = ("Service", "State", "Controls")

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
            "<span style='color:#93a1a1;'>NSSM service lifecycle for the active stack  ·  "
            "stop/start may need admin privileges</span>"
        )
        info.setTextFormat(Qt.TextFormat.RichText)
        layout.addWidget(info)

        self._table = QTableWidget(0, len(self.HEADERS))
        self._table.setHorizontalHeaderLabels(list(self.HEADERS))
        self._table.verticalHeader().setVisible(False)
        self._table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
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
            self._table.setCellWidget(i, 2, self._make_controls(svc, running, exists))

    def _make_controls(self, name: str, running: bool, exists: bool) -> QWidget:
        wrap = QWidget()
        layout = QHBoxLayout(wrap)
        layout.setContentsMargins(0, 0, 0, 0)
        start_btn = QPushButton("Start")
        start_btn.setEnabled(exists and not running)
        start_btn.clicked.connect(lambda _checked=False, n=name: self._do_start(n))
        stop_btn = QPushButton("Stop")
        stop_btn.setEnabled(running)
        stop_btn.clicked.connect(lambda _checked=False, n=name: self._do_stop(n))
        restart_btn = QPushButton("Restart")
        restart_btn.setEnabled(exists)
        restart_btn.clicked.connect(lambda _checked=False, n=name: self._do_restart(n))
        for b in (start_btn, stop_btn, restart_btn):
            layout.addWidget(b)
        layout.addStretch()
        return wrap

    def _do_start(self, name: str) -> None:
        ok, msg = nssm_client.nssm_start(name)
        if not ok:
            QMessageBox.warning(self, "Start failed", f"{name}\n\n{msg or 'nssm command failed'}")
        self._refresh()

    def _do_stop(self, name: str) -> None:
        ok, msg = nssm_client.nssm_stop(name)
        if not ok:
            QMessageBox.warning(self, "Stop failed", f"{name}\n\n{msg or 'nssm command failed'}")
        self._refresh()

    def _do_restart(self, name: str) -> None:
        ok, msg = nssm_client.nssm_restart(name)
        if not ok:
            QMessageBox.warning(self, "Restart failed", f"{name}\n\n{msg or 'nssm command failed'}")
        self._refresh()
