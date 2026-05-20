"""MainWindow shell: nav rail + header + QStackedWidget of views.

Day 1 wires the chrome. Views are stubs filled in Days 2+.
"""
from __future__ import annotations

from dataclasses import replace

from PySide6.QtWidgets import (
    QHBoxLayout,
    QMainWindow,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from src.gui.panels.header_bar import HeaderBar
from src.gui.panels.nav_rail import NavRail
from src.gui.panels.services_bar import ServicesBar
from src.gui.services.halt_controller import HaltController
from src.gui.services.notifications import NotifEvent, NotificationWatcher
from src.gui.services.risk_budget import RiskMetrics, RiskMonitor
from src.gui.services.stack_registry import Stack, discover_stacks
from src.gui.services.stack_state import load_state, save_state
from src.gui.services.tray import TrayController
from src.gui.views.cost_view import CostView
from src.gui.views.evaluation_view import EvaluationView
from src.gui.views.journal_view import JournalView
from src.gui.views.live_view import LiveView
from src.gui.views.profile_view import ProfileView
from src.gui.views.prompts_view import PromptsView
from src.gui.views.rejected_view import RejectedView
from src.gui.views.replay_view import ReplayView
from src.gui.views.risk_view import RiskView
from src.gui.views.settings_view import SettingsView


_VIEW_KEYS = (
    "live", "journal", "evaluation", "rejected", "cost", "risk",
    "replay", "profile", "prompts", "settings",
)


class MainWindow(QMainWindow):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stacks = discover_stacks()
        self._stack = stack
        self._halt = HaltController(stack.db_path, parent=self)
        self._risk_monitor = RiskMonitor(stack.db_path, parent=self)
        self._notif_watcher = NotificationWatcher(stack.db_path, parent=self)
        self._tray = TrayController(parent=self)
        self._allow_close = False
        self._today_pnl_text = ""

        self.setWindowTitle("CopyTrades")
        self.resize(1280, 820)
        self._build_ui()
        self._wire_tray()
        self._halt.start()
        self._risk_monitor.start()
        self._notif_watcher.start()

        from src.gui.services.crash_watcher import CrashWatcher
        from src.gui.services.thread_registry import register
        self._crash_watcher = CrashWatcher(self._stack, parent=self)
        self._crash_watcher.crashed.connect(self._on_service_crashed)
        self._crash_watcher.recovered.connect(self._on_service_recovered)
        register(self._crash_watcher, stop_fn=self._crash_watcher.stop)
        self._crash_watcher.start()

        from PySide6.QtCore import QTimer
        self._nav_badge_timer = QTimer(self)
        self._nav_badge_timer.setInterval(5000)
        self._nav_badge_timer.timeout.connect(self._refresh_nav_badges)
        self._nav_badge_timer.start()
        self._refresh_nav_badges()

        state = load_state()
        if state.last_active_view in _VIEW_KEYS:
            self._switch_view(state.last_active_view)

    def _refresh_nav_badges(self) -> None:
        """Poll the stack DB and update nav-item badges:
          - Live      : count of actions in non-terminal status
          - Rejected  : count of today's rejected actions
          - Cost      : '!' when today's spend > 80% of budget, '×' if over cap
        """
        import sqlite3
        from datetime import datetime, timezone
        from pathlib import Path
        try:
            with sqlite3.connect(str(self._stack.db_path)) as conn:
                today = datetime.now(timezone.utc).date().isoformat()
                live = conn.execute(
                    "SELECT COUNT(*) FROM actions "
                    "WHERE status IN ('pending', 'sent', 'claimed', 'watching')"
                ).fetchone()[0]
                rejected_today = conn.execute(
                    "SELECT COUNT(*) FROM actions "
                    "WHERE status='rejected' AND created_at >= ?",
                    (today,),
                ).fetchone()[0]
        except sqlite3.OperationalError:
            live = 0
            rejected_today = 0
        self._nav.set_badge("live", live or None)
        self._nav.set_badge("rejected", rejected_today or None)

        # Cost badge: read ai_calls.jsonl + budget.
        try:
            from src import db_settings
            from src.cost_guard import todays_cost_usd
            log = self._stack.project_path / "logs" / "ai_calls.jsonl"
            today_cost = todays_cost_usd(log)
            budget = db_settings.get_float(self._stack.db_path, "cost_daily_budget_usd", 0.0)
            badge: str | None = None
            if budget > 0:
                pct = today_cost / budget
                if pct >= 1.0:
                    badge = "×"
                elif pct >= 0.8:
                    badge = "!"
            self._nav.set_badge("cost", badge)
        except Exception:
            pass

    def closeEvent(self, event) -> None:  # type: ignore[override]
        if self._tray.is_available() and not self._allow_close:
            event.ignore()
            self.hide()
            self._tray.notify(
                "CopyTrades is still running",
                "Right-click the tray icon for menu or Quit.",
                "info",
            )
            return
        self._halt.stop()
        self._risk_monitor.stop()
        self._notif_watcher.stop()
        from src.gui.services.thread_registry import stop_all
        stop_all(timeout_ms=2000)
        super().closeEvent(event)

    def _wire_tray(self) -> None:
        self._tray.show_window.connect(self._on_show_request)
        self._tray.toggle_halt.connect(self._on_tray_halt)
        self._tray.quit_requested.connect(self._on_tray_quit)
        self._tray.update_state(self._stack.name, self._halt.is_halted())
        self._halt.state_changed.connect(self._on_halt_state_changed)
        self._notif_watcher.event.connect(self._on_notif)
        self._risk_monitor.metrics_updated.connect(self._on_risk_metrics)
        self._risk_monitor.triggered.connect(self._on_risk_triggered)

    def _on_show_request(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def _on_tray_halt(self) -> None:
        self._halt.toggle()

    def _on_tray_quit(self) -> None:
        self._allow_close = True
        self.close()
        from PySide6.QtWidgets import QApplication
        QApplication.instance().quit()

    def _on_halt_state_changed(self, halted: bool) -> None:
        self._tray.update_state(self._stack.name, halted, self._today_pnl_text)

    def _on_notif(self, event: NotifEvent) -> None:
        self._tray.notify(event.title, event.body, event.severity)

    def _on_risk_metrics(self, m: RiskMetrics) -> None:
        self._today_pnl_text = f"${m.today_pnl:+.2f}"
        self._tray.update_state(self._stack.name, self._halt.is_halted(), self._today_pnl_text)
        closed = m.today_wins + m.today_losses
        if closed:
            wr = f"{(m.today_wins / closed) * 100:.0f}%"
        else:
            wr = "—"
        summary = (
            f"Today  {self._today_pnl_text}  ·  "
            f"Trades {m.today_trade_count}  ·  "
            f"Wins {m.today_wins}/{closed}  ·  WR {wr}"
        )
        self._header.set_summary(summary)

    def _on_risk_triggered(self, breaches: list) -> None:
        self._tray.notify(
            "Risk budget triggered HALT",
            "; ".join(breaches)[:200],
            "error",
        )

    def _build_ui(self) -> None:
        self._nav = NavRail()
        self._nav.item_selected.connect(self._switch_view)

        self._header = HeaderBar(self._stacks, self._stack, self._halt)
        self._header.stack_change_requested.connect(self._rebind_stack)
        self._header.new_stack_requested.connect(self._open_new_stack_wizard)
        self._services_bar = ServicesBar(self._stack)

        settings_view = SettingsView(self._stack)
        settings_view.new_stack_requested.connect(self._open_new_stack_wizard)
        rejected_view = RejectedView(self._stack)
        replay_view = ReplayView(self._stack)
        # Cross-view: clicking Replay on a rejected row jumps to the
        # Replay view with that specific message pre-loaded (REVIEW.md
        # §4.3). Keeps the operator's flow tight when triaging spikes.
        rejected_view.replay_message_requested.connect(
            lambda mid: (
                replay_view.preload_message(mid),
                self._switch_view("replay"),
            )
        )
        self._views: dict[str, QWidget] = {
            "live": LiveView(self._stack),
            "journal": JournalView(self._stack),
            "evaluation": EvaluationView(self._stack),
            "rejected": rejected_view,
            "cost": CostView(self._stack),
            "risk": RiskView(self._stack, self._risk_monitor),
            "replay": replay_view,
            "profile": ProfileView(self._stack),
            "prompts": PromptsView(self._stack),
            "settings": settings_view,
        }
        self._stack_widget = QStackedWidget()
        for key in _VIEW_KEYS:
            self._stack_widget.addWidget(self._views[key])

        right = QWidget()
        right_layout = QVBoxLayout(right)
        right_layout.setContentsMargins(0, 0, 0, 0)
        right_layout.setSpacing(0)
        right_layout.addWidget(self._header)
        right_layout.addWidget(self._services_bar)

        from src.gui.panels.crash_banner import CrashBanner
        self._crash_banner = CrashBanner()
        self._crash_banner.restart_requested.connect(self._on_crash_restart)
        right_layout.addWidget(self._crash_banner)

        right_layout.addWidget(self._stack_widget, 1)

        central = QWidget()
        central_layout = QHBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(self._nav)
        central_layout.addWidget(right, 1)

        self.setCentralWidget(central)
        self.statusBar().showMessage(self._status_text())

    def _switch_view(self, key: str) -> None:
        if key not in self._views:
            return
        self._nav.set_active(key)
        self._stack_widget.setCurrentWidget(self._views[key])
        save_state(replace(load_state(), last_active_view=key))

    def _rebind_stack(self, new_stack: Stack) -> None:
        if new_stack.name == self._stack.name:
            return
        self._stack = new_stack
        self._halt.rebind(new_stack.db_path)
        self._risk_monitor.rebind(new_stack.db_path)
        self._notif_watcher.rebind(new_stack.db_path)
        self._tray.update_state(new_stack.name, self._halt.is_halted(), self._today_pnl_text)
        self._services_bar.rebind(new_stack)
        self._crash_watcher.set_stack(new_stack)
        for view in self._views.values():
            if hasattr(view, "rebind"):
                view.rebind(new_stack)
        self.statusBar().showMessage(self._status_text())

    def _open_new_stack_wizard(self) -> None:
        """Launch the full setup wizard with stack=None so a brand-new
        stack can be created end-to-end (identity + AI + bot + TG +
        services). On success, refresh the stack list and switch active
        stack to the freshly-created one.
        """
        from src.gui.windows.telegram_wizard import TelegramWizard
        wiz = TelegramWizard(None, self)
        result = wiz.exec()
        if not result or wiz.stack is None:
            return
        new_stack = wiz.stack
        self._stacks = discover_stacks()
        self._header.switcher.set_stacks(self._stacks, new_stack)
        from src.gui.app import _ensure_db_ready
        _ensure_db_ready(new_stack)
        self._rebind_stack(new_stack)

    def _on_service_crashed(self, service: str, tail: list, log_path: str) -> None:
        self._crash_banner.push_alert(service, list(tail), log_path)

    def _on_service_recovered(self, service: str) -> None:
        self._crash_banner.dismiss_service(service)

    def _on_crash_restart(self, service: str) -> None:
        from PySide6.QtWidgets import QMessageBox
        from src.gui.services import nssm_client
        ok, msg = nssm_client.nssm_restart(service)
        if not ok:
            QMessageBox.warning(
                self, "Restart failed",
                f"nssm restart {service} failed:\n\n{msg}\n\n"
                "Service restart typically needs admin rights. Run as "
                "administrator, or restart from the Services panel.",
            )

    def _status_text(self) -> str:
        return (
            f"connected to {self._stack.api_url}  ·  "
            f"DB: {self._stack.db_path.name}  ·  v0.1.0"
        )
