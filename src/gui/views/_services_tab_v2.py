"""V2-native Services tab.

Replaces the legacy flat-table ``_ServicesTab`` (which assumed one
stack = one 3-tuple of services). With v2 multi-channel topologies
(N channels -> 1 account -> N destinations -> N bots), a single
destination's service list can hit 5+ services across role groups.

Layout:
  - Topology header (read-only counts)
  - Global action bar:  Install ALL | Uninstall ALL | Start ALL |
                        Stop ALL | Restart ALL | Export diagnostics
  - Three grouped sections (Accounts / Destinations / Bots).
    Each section is a card-style group with one row per service:
        entity name + role badge
        service name + db path / port
        state badge (RUNNING / STOPPED / NOT INSTALLED)
        per-row buttons: Start Stop Restart Install Uninstall Logs

State refreshes on a 5s timer (same cadence as the legacy tab).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from src.gui.services import nssm_client
from src.gui.services.stack_registry import Stack


# --------------------------------------------------------------------- model

@dataclass(frozen=True)
class _ServiceRow:
    """One installable service surfaced in the v2 services tab."""
    role: str            # "Account" | "Destination" | "Bot"
    entity_name: str     # human label (e.g. "Forex Engineer", "Main MT5")
    service_name: str    # NSSM service name
    subtitle: str        # "DB: ..." or "port 8765" or "-> dest_a"
    spec_entry: dict     # the dict from derive_v2_service_spec, for install


def _collect_rows() -> tuple[list[_ServiceRow], list[_ServiceRow], list[_ServiceRow], str]:
    """Read v2 config + derive per-role rows for the services tab.

    Returns (accounts, destinations, bots, error_message). When v2
    config is unreadable, returns three empty lists plus an
    explanatory string the caller surfaces inline.
    """
    try:
        from src import config_v2
        from src.gui.services.v2_service_spec import derive_v2_service_spec
    except Exception as e:
        return [], [], [], f"v2 config module unavailable: {e}"

    cfg_path = config_v2.config_path()
    if not config_v2.is_v2(cfg_path):
        return [], [], [], (
            "v2 config not found at " + str(cfg_path) +
            " - open V2 Config to add an Account / Profile / Destination / "
            "Bot / Channel."
        )
    cfg = config_v2.load_v2(cfg_path)
    if cfg is None:
        return [], [], [], "v2 config could not be loaded (parse error)."

    spec = derive_v2_service_spec(cfg)
    by_service: dict[str, dict] = {e["service"]: e for e in spec}

    accounts: list[_ServiceRow] = []
    destinations: list[_ServiceRow] = []
    bots: list[_ServiceRow] = []

    for dest in cfg.destinations:
        if not dest.service_name:
            continue
        entry = by_service.get(dest.service_name)
        if entry is None:
            continue
        destinations.append(_ServiceRow(
            role="Destination",
            entity_name=dest.name,
            service_name=dest.service_name,
            subtitle=f"port {dest.api_port}  -  DB: {dest.db_path}",
            spec_entry=entry,
        ))

    for bot in cfg.bots:
        if not bot.service_name:
            continue
        entry = by_service.get(bot.service_name)
        if entry is None:
            continue
        dest_names: list[str] = []
        for bd in cfg.bot_bindings:
            if bd.bot_id != bot.id:
                continue
            dest = cfg.destination(bd.destination_id) if bd.destination_id else None
            if dest is not None:
                dest_names.append(dest.name)
        target = ", ".join(dest_names) if dest_names else "(no destination bound)"
        bots.append(_ServiceRow(
            role="Bot",
            entity_name=bot.name,
            service_name=bot.service_name,
            subtitle=f"-> {target}",
            spec_entry=entry,
        ))

    for acct in cfg.accounts:
        if not acct.service_name:
            continue
        entry = by_service.get(acct.service_name)
        if entry is None:
            continue
        ch_count = sum(1 for ch in cfg.channels if ch.account_id == acct.id and ch.enabled)
        accounts.append(_ServiceRow(
            role="Account",
            entity_name=acct.name or acct.id,
            service_name=acct.service_name,
            subtitle=f"{ch_count} channel(s)",
            spec_entry=entry,
        ))

    return accounts, destinations, bots, ""


# ---------------------------------------------------------------------- view

class _ServiceRowWidget(QFrame):
    """One row in a section card. Lays out state + buttons."""

    def __init__(self, row: _ServiceRow, on_action: Callable[[str, _ServiceRow], None]) -> None:
        super().__init__()
        self._row = row
        self._on_action = on_action
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setObjectName("ct_service_row")
        self.setStyleSheet(
            "QFrame#ct_service_row {"
            " border: 1px solid #2a2e39;"
            " border-radius: 6px;"
            " padding: 6px 8px;"
            " margin: 2px 0;"
            "}"
        )

        outer = QHBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(10)

        left = QVBoxLayout()
        left.setSpacing(2)
        title = QLabel(
            f"<b>{row.entity_name}</b>  "
            f"<span style='color:#787b86;'>-  {row.service_name}</span>"
        )
        title.setTextFormat(Qt.TextFormat.RichText)
        sub = QLabel(f"<span style='color:#787b86; font-size:11px;'>{row.subtitle}</span>")
        sub.setTextFormat(Qt.TextFormat.RichText)
        left.addWidget(title)
        left.addWidget(sub)
        outer.addLayout(left, 1)

        self._state = QLabel("...")
        self._state.setMinimumWidth(120)
        self._state.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._state.setTextFormat(Qt.TextFormat.RichText)
        outer.addWidget(self._state)

        buttons = QHBoxLayout()
        buttons.setSpacing(4)
        # Per-row controls mirror the header services-bar's start/stop/
        # restart trio: transparent icon-only, tinted by semantic.
        # Install/Uninstall/Logs added (services-tab-specific) using the
        # same vocabulary. Tooltip carries the action name.
        from src.gui._button_helpers import make_icon_button
        _row_buttons = (
            ("PLAY",     "success", "Start this service",                   "start"),
            ("CLOSE",    "danger",  "Stop this service",                    "stop"),
            ("UPDATE",   "warning", "Restart this service",                 "restart"),
            ("DOWNLOAD", "success", "(Re-)install this service via NSSM",   "install"),
            ("REMOVE",   "danger",  "Stop + unregister this service",       "uninstall"),
            ("FOLDER",   "",        "Open the log folder for this service", "logs"),
        )
        for icon_name, variant, tip, key in _row_buttons:
            btn = make_icon_button(icon_name, tip, variant=variant,
                                   fallback_text=key.capitalize())
            btn.clicked.connect(lambda _checked=False, k=key: self._on_action(k, self._row))
            buttons.addWidget(btn)
        outer.addLayout(buttons)
        outer.setSizeConstraint(QHBoxLayout.SizeConstraint.SetMinimumSize)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)

    def refresh_state(self) -> None:
        name = self._row.service_name
        if not nssm_client.service_exists(name):
            self._state.setText(
                "<span style='color:#787b86; font-weight:600;'>NOT INSTALLED</span>"
            )
            return
        if nssm_client.service_running(name):
            self._state.setText(
                "<span style='color:#26a69a; font-weight:700;'>RUNNING</span>"
            )
        else:
            self._state.setText(
                "<span style='color:#ef5350; font-weight:600;'>STOPPED</span>"
            )


class _SectionCard(QFrame):
    """Card wrapper with a title and a column of _ServiceRowWidgets."""

    def __init__(self, title: str) -> None:
        super().__init__()
        self.setObjectName("ct_service_section")
        self.setFrameShape(QFrame.Shape.StyledPanel)
        self.setStyleSheet(
            "QFrame#ct_service_section {"
            " border: 1px solid #2a2e39;"
            " border-radius: 8px;"
            " padding: 10px;"
            " margin-bottom: 8px;"
            "}"
        )
        self._title = QLabel(
            f"<span style='font-size:11px;font-weight:700;letter-spacing:1.5px;"
            f"color:#787b86;'>{title.upper()}</span>"
        )
        self._title.setTextFormat(Qt.TextFormat.RichText)
        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(8, 6, 8, 6)
        self._layout.setSpacing(4)
        self._layout.addWidget(self._title)
        self._rows_layout = QVBoxLayout()
        self._rows_layout.setSpacing(4)
        self._layout.addLayout(self._rows_layout)

    def set_rows(self, widgets: list[_ServiceRowWidget]) -> None:
        while self._rows_layout.count():
            item = self._rows_layout.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.deleteLater()
        if not widgets:
            empty = QLabel(
                "<span style='color:#787b86; font-style:italic;'>"
                "(no entities in this group yet - add one via V2 Config)"
                "</span>"
            )
            empty.setTextFormat(Qt.TextFormat.RichText)
            self._rows_layout.addWidget(empty)
            return
        for w in widgets:
            self._rows_layout.addWidget(w)


class ServicesTabV2(QWidget):
    """V2-native services tab - entity-grouped with per-row + bulk controls."""

    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self._row_widgets: list[_ServiceRowWidget] = []
        self._build_ui()
        self._refresh()
        self._timer = QTimer(self)
        self._timer.setInterval(5000)
        self._timer.timeout.connect(self._refresh_state_only)
        self._timer.start()

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._refresh()

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 8, 0, 0)
        layout.setSpacing(8)

        self._topology = QLabel("")
        self._topology.setTextFormat(Qt.TextFormat.RichText)
        self._topology.setWordWrap(True)
        layout.addWidget(self._topology)

        # Office-style compact ribbon with three semantic groups:
        # Install (one-shot register/unregister), Lifecycle (start/stop/
        # restart), Utility (refresh + diagnostics export). Visually
        # mirrors the per-row icon trio so the eye treats them as a
        # consistent service-control language.
        from src.gui.panels.ribbon_bar import RibbonAction, RibbonBar, RibbonGroup
        ribbon = RibbonBar([
            RibbonGroup("Install", [
                RibbonAction("DOWNLOAD", "Install ALL",
                             "Install every v2 service in one elevated call",
                             variant="success", callback=self._on_install_all),
                RibbonAction("REMOVE", "Uninstall ALL",
                             "Stop + unregister every v2 service",
                             variant="danger", callback=self._on_uninstall_all),
            ]),
            RibbonGroup("Lifecycle", [
                RibbonAction("PLAY", "Start ALL",
                             "Start every installed v2 service",
                             variant="success", callback=self._on_start_all),
                RibbonAction("CLOSE", "Stop ALL",
                             "Stop every running v2 service",
                             variant="danger", callback=self._on_stop_all),
                RibbonAction("UPDATE", "Restart ALL",
                             "Restart every running v2 service",
                             variant="warning", callback=self._on_restart_all),
            ]),
            RibbonGroup("Utility", [
                RibbonAction("SYNC", "Refresh",
                             "Re-read v2 config and refresh state",
                             callback=self._refresh),
                RibbonAction("SHARE", "Export diag.",
                             "Bundle logs + sanitized DB for the active destination",
                             variant="primary", callback=self._on_export_diagnostics),
            ]),
        ])
        layout.addWidget(ribbon)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        container = QWidget()
        body = QVBoxLayout(container)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(8)

        self._accounts_section = _SectionCard("Accounts (Telegram listeners)")
        self._destinations_section = _SectionCard("Destinations (MT5 -> API bridges)")
        self._bots_section = _SectionCard("Bots (Telegram control + promoter)")
        body.addWidget(self._accounts_section)
        body.addWidget(self._destinations_section)
        body.addWidget(self._bots_section)
        body.addStretch()

        scroll.setWidget(container)
        layout.addWidget(scroll, 1)

        self._error_label = QLabel("")
        self._error_label.setTextFormat(Qt.TextFormat.RichText)
        self._error_label.setWordWrap(True)
        layout.addWidget(self._error_label)

    def _refresh(self) -> None:
        """Reload from v2 config + rebuild row widgets."""
        accounts, destinations, bots, err = _collect_rows()

        if err:
            self._error_label.setText(
                f"<span style='color:#ef5350;'>{err}</span>"
            )
        else:
            self._error_label.setText("")

        self._topology.setText(
            "<span style='color:#787b86;'>"
            f"<b>{len(accounts)}</b> account(s)  -  "
            f"<b>{len(destinations)}</b> destination(s)  -  "
            f"<b>{len(bots)}</b> bot(s)  ::  "
            f"<b>{len(accounts) + len(destinations) + len(bots)}</b> service(s) total"
            "</span>"
        )

        self._row_widgets = []
        acct_widgets = [self._mk_row(r) for r in accounts]
        dest_widgets = [self._mk_row(r) for r in destinations]
        bot_widgets = [self._mk_row(r) for r in bots]
        self._accounts_section.set_rows(acct_widgets)
        self._destinations_section.set_rows(dest_widgets)
        self._bots_section.set_rows(bot_widgets)
        self._row_widgets = acct_widgets + dest_widgets + bot_widgets
        self._refresh_state_only()

    def _refresh_state_only(self) -> None:
        for w in self._row_widgets:
            w.refresh_state()

    def _mk_row(self, row: _ServiceRow) -> _ServiceRowWidget:
        return _ServiceRowWidget(row, on_action=self._on_row_action)

    def _on_row_action(self, key: str, row: _ServiceRow) -> None:
        if key == "start":
            ok, msg = nssm_client.nssm_start(row.service_name)
            self._notify(ok, "Start", row.service_name, msg)
        elif key == "stop":
            ok, msg = nssm_client.nssm_stop(row.service_name)
            self._notify(ok, "Stop", row.service_name, msg)
        elif key == "restart":
            ok, msg = nssm_client.nssm_restart(row.service_name)
            self._notify(ok, "Restart", row.service_name, msg)
        elif key == "install":
            from src.gui.services.bootstrap import install_v2_service_subset
            ok, msg = install_v2_service_subset([row.spec_entry])
            self._notify(ok, "Install", row.service_name, msg)
        elif key == "uninstall":
            confirm = QMessageBox.question(
                self, "Uninstall service",
                f"Stop and unregister '{row.service_name}'?\n\n"
                "Logs and the database are not touched.",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if confirm != QMessageBox.StandardButton.Yes:
                return
            from src.gui.services.elevation import run_elevated_python
            ok = run_elevated_python(
                "src.gui.helpers.bootstrap_services_uninstall",
                [row.service_name],
            )
            self._notify(
                ok, "Uninstall", row.service_name,
                "Requested; check Services.msc to confirm."
                if ok else "Elevation cancelled or helper failed.",
            )
        elif key == "logs":
            self._open_logs_for(row)
        self._refresh_state_only()
        QTimer.singleShot(1500, self._refresh_state_only)

    def _open_logs_for(self, row: _ServiceRow) -> None:
        db_path = row.spec_entry.get("db_path") or ""
        if db_path:
            logs_dir = Path(db_path).parent / "logs"
        else:
            logs_dir = Path.cwd() / "logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(logs_dir)))

    def _notify(self, ok: bool, verb: str, svc: str, detail: str) -> None:
        title = f"{verb} {svc}"
        if ok:
            return
        QMessageBox.warning(self, title, detail or "(no detail)")

    def _on_install_all(self) -> None:
        from src.gui.services.bootstrap import install_v2_services_all
        ok, msg = install_v2_services_all()
        (QMessageBox.information if ok else QMessageBox.warning)(
            self, "Install ALL v2 services", msg,
        )
        QTimer.singleShot(1500, self._refresh)
        QTimer.singleShot(4000, self._refresh)

    def _on_uninstall_all(self) -> None:
        names = [w._row.service_name for w in self._row_widgets]
        if not names:
            return
        confirm = QMessageBox.question(
            self, "Uninstall ALL v2 services",
            "Stop and unregister the following services?\n\n  - "
            + "\n  - ".join(names)
            + "\n\nLogs and the database are not touched.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        from src.gui.services.elevation import run_elevated_python
        ok = run_elevated_python(
            "src.gui.helpers.bootstrap_services_uninstall", names,
        )
        if not ok:
            QMessageBox.warning(
                self, "Uninstall ALL v2 services",
                "Elevation cancelled or helper failed.",
            )
        QTimer.singleShot(1500, self._refresh)
        QTimer.singleShot(4000, self._refresh)

    def _on_start_all(self) -> None:
        self._bulk_nssm(nssm_client.nssm_start, "Start")

    def _on_stop_all(self) -> None:
        self._bulk_nssm(nssm_client.nssm_stop, "Stop")

    def _on_restart_all(self) -> None:
        self._bulk_nssm(nssm_client.nssm_restart, "Restart")

    def _bulk_nssm(self, op: Callable[[str], tuple[bool, str]], verb: str) -> None:
        failures: list[str] = []
        for w in self._row_widgets:
            svc = w._row.service_name
            if not nssm_client.service_exists(svc):
                continue
            ok, msg = op(svc)
            if not ok:
                failures.append(f"{svc}: {msg}")
        if failures:
            QMessageBox.warning(
                self, f"{verb} ALL",
                "Some services could not be controlled:\n\n"
                + "\n".join(failures),
            )
        self._refresh_state_only()
        QTimer.singleShot(1500, self._refresh_state_only)

    def _on_export_diagnostics(self) -> None:
        from src.gui.windows.diagnostics_export_dialog import DiagnosticsExportDialog
        dlg = DiagnosticsExportDialog(self._stack, self)
        dlg.exec_()
