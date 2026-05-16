"""SERVICES bar: API / Bot / Listener / EA / Snapshot health pills + restart menu."""
from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone

from PySide6.QtCore import Qt, QObject, QSize, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QPushButton,
    QWidget,
)

from src.gui.services import nssm_client
from src.gui.services.health_pinger import ping_once
from src.gui.services.stack_registry import Stack


@dataclass(frozen=True)
class _Health:
    api_ok: bool
    api_caption: str
    bot_state: str       # "ok" | "warn" | "bad"
    bot_caption: str
    listener_state: str  # "ok" | "warn" | "bad"
    listener_caption: str
    ea_state: str
    ea_caption: str
    snapshot_state: str
    snapshot_caption: str


_OK_COLOR = "#26a69a"
_WARN_COLOR = "#ff9800"
_BAD_COLOR = "#ef5350"


def _human_age(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m {seconds % 60}s"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


class _HealthPoller(QThread):
    health = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._stack: Stack | None = None
        self._stop = threading.Event()
        self._wake = threading.Event()

    def set_stack(self, stack: Stack) -> None:
        self._stack = stack
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def run(self) -> None:
        while not self._stop.is_set():
            stack = self._stack
            if stack is not None:
                self.health.emit(self._probe(stack))
            self._wake.clear()
            self._wake.wait(timeout=5.0)

    def _probe(self, stack: Stack) -> _Health:
        api_url = f"{stack.api_url}/health"
        api_ok = ping_once(api_url, timeout=1.0)
        api_caption = f"port {stack.api_port}" if api_ok else "unreachable"

        _api_svc, bot_svc, listener_svc = stack.service_names
        bot_state, bot_caption = self._service_health(
            stack, bot_svc, "bot_telegram_ok_at",
        )
        listener_state, listener_caption = self._service_health(
            stack, listener_svc, "listener_telegram_ok_at",
        )

        ea_state, ea_caption = self._market_age_state(
            stack, "market_XAUUSD_at", warn_sec=30, bad_sec=60
        )
        snap_state, snap_caption = self._market_age_state(
            stack, "market_snapshot_XAUUSD_at", warn_sec=90, bad_sec=180
        )

        return _Health(
            api_ok=api_ok,
            api_caption=api_caption,
            bot_state=bot_state,
            bot_caption=bot_caption,
            listener_state=listener_state,
            listener_caption=listener_caption,
            ea_state=ea_state,
            ea_caption=ea_caption,
            snapshot_state=snap_state,
            snapshot_caption=snap_caption,
        )

    def _service_health(
        self, stack: Stack, svc_name: str, heartbeat_key: str,
    ) -> tuple[str, str]:
        """Combine 'Windows service running' + 'Telegram heartbeat fresh'.

        Service stopped       -> bad / "stopped"
        Service running + fresh heartbeat (<=60s)  -> ok / "<age>"
        Service running + stale heartbeat (<=300s) -> warn / "stale <age>"
        Service running + dead heartbeat (>300s)   -> bad / "no telegram"
        Service running + no heartbeat key at all  -> warn / "starting…"
        """
        if not nssm_client.service_running(svc_name):
            return "bad", "stopped"
        hb_state, hb_caption = self._market_age_state(
            stack, heartbeat_key, warn_sec=60, bad_sec=300
        )
        # Translate the heartbeat sub-states into messages that read
        # right next to a running service.
        if hb_state == "ok":
            return "ok", hb_caption
        if hb_state == "warn":
            return "warn", f"stale {hb_caption}"
        # bad — either no heartbeat ever, or it's gone cold.
        if hb_caption in ("no data", "no db", "db error", "bad ts"):
            return "warn", "starting…"
        return "bad", f"no telegram ({hb_caption})"

    def _market_age_state(
        self, stack: Stack, key: str, warn_sec: int, bad_sec: int
    ) -> tuple[str, str]:
        if not stack.db_path.exists():
            return "bad", "no db"
        try:
            with sqlite3.connect(str(stack.db_path)) as conn:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key=?", (key,)
                ).fetchone()
        except sqlite3.Error:
            return "bad", "db error"
        if row is None or not row[0]:
            return "bad", "no data"
        try:
            s = row[0].replace("Z", "+00:00")
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return "bad", "bad ts"
        age = int((datetime.now(timezone.utc) - dt).total_seconds())
        if age < 0:
            age = 0
        if age <= warn_sec:
            return "ok", _human_age(age)
        if age <= bad_sec:
            return "warn", _human_age(age)
        return "bad", _human_age(age)


class _Pill(QLabel):
    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label
        self.setTextFormat(Qt.TextFormat.RichText)
        self._set("·", _BAD_COLOR, "—")

    def set_state(self, state: str, caption: str) -> None:
        color = {"ok": _OK_COLOR, "warn": _WARN_COLOR, "bad": _BAD_COLOR}.get(
            state, _BAD_COLOR
        )
        self._set("●", color, caption)

    def _set(self, glyph: str, color: str, caption: str) -> None:
        self.setText(
            f"<span style='color:#787b86; font-weight:600;'>{self._label}</span>"
            f"&nbsp;<span style='color:{color}; font-size:13px;'>{glyph}</span>"
            f"&nbsp;<span style='color:#787b86;'>{caption}</span>"
        )


class ServicesBar(QWidget):
    def __init__(self, stack: Stack) -> None:
        super().__init__()
        self._stack = stack
        self.setFixedHeight(36)

        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 4, 12, 4)
        layout.setSpacing(18)

        layout.addWidget(QLabel("<b>SERVICES</b>"))
        self._api = _Pill("API")
        self._bot = _Pill("Bot")
        self._listener = _Pill("Listener")
        self._ea = _Pill("EA")
        self._snapshot = _Pill("Snapshot")
        for w in (self._api, self._bot, self._listener, self._ea, self._snapshot):
            layout.addWidget(w)
        layout.addStretch()

        # Replace the single "Restart ▾" combo with three icon buttons
        # (Start / Stop / Restart). Each opens the same per-service +
        # "all" submenu. Colors track the active palette via the
        # `role` property + QSS so light/dark both look right.
        self._start_btn = self._make_action_button(
            "start", "Start service…", self._build_start_menu,
        )
        self._stop_btn = self._make_action_button(
            "stop", "Stop service…", self._build_stop_menu,
        )
        self._restart_btn = self._make_action_button(
            "restart", "Restart service…", self._build_restart_menu,
        )
        layout.addWidget(self._start_btn)
        layout.addWidget(self._stop_btn)
        layout.addWidget(self._restart_btn)

        self._poller = _HealthPoller(parent=self)
        self._poller.health.connect(self._on_health)
        self._poller.set_stack(stack)
        from src.gui.services.thread_registry import register
        register(self._poller, stop_fn=self._poller.stop)
        self._poller.start()

        self._kick_timer = QTimer(self)
        self._kick_timer.setInterval(5000)
        self._kick_timer.timeout.connect(self._kick_poll)
        self._kick_timer.start()

        # Tint the action buttons to match the active palette so the
        # operator can scan start/stop/restart at a glance in both
        # light and dark modes.
        self._apply_action_styles()
        try:
            from src.gui.theme import bus as _theme_bus
            _theme_bus.theme_changed.connect(lambda _pal: self._apply_action_styles())
        except Exception:
            pass

    def _apply_action_styles(self) -> None:
        """Tint each button's icon with its semantic colour. Pure-icon
        affordance — no border or background, the colour is the only
        visual cue: green=start, red=stop, orange=restart. Works in
        both light and dark palettes because the icons are SVG and
        FluentIcon.icon() colours them directly."""
        from src.gui.theme import current_palette
        from PySide6.QtGui import QColor
        try:
            from qfluentwidgets import FluentIcon
        except Exception:
            return
        p = current_palette()
        icon_map = (
            (self._start_btn,   FluentIcon.PLAY,   p.success),
            (self._stop_btn,    FluentIcon.CLOSE,  p.danger),
            (self._restart_btn, FluentIcon.UPDATE, p.warning),
        )
        for btn, glyph, color in icon_map:
            try:
                btn.setIcon(glyph.icon(color=QColor(color)))
            except Exception:
                # FluentIcon.icon(color=...) is the typical API; if a
                # particular qfluentwidgets release dropped it, fall
                # back to the uncoloured icon so the button still works.
                btn.setIcon(glyph.icon())

    def rebind(self, stack: Stack) -> None:
        self._stack = stack
        self._start_btn.setMenu(self._build_start_menu())
        self._stop_btn.setMenu(self._build_stop_menu())
        self._restart_btn.setMenu(self._build_restart_menu())
        self._poller.set_stack(stack)

    def _make_action_button(self, kind: str, tooltip: str, menu_builder):
        """Build one of the three service-action buttons as a pure
        coloured icon (no border, no background, no chevron). Click
        pops the per-service menu. Falls back to a labelled QPushButton
        on stripped installs.
        """
        try:
            from qfluentwidgets import TransparentToolButton
            btn = TransparentToolButton()
            btn.setFixedSize(28, 28)
            btn.setIconSize(QSize(18, 18))
            btn.setProperty("svcAction", kind)
            # Hide the menu chevron drawn by Qt when setMenu() is used.
            # `::menu-indicator { image: none; width: 0; }` strips the
            # arrow, and the transparent base means no border to clip.
            btn.setStyleSheet(
                "QToolButton { background: transparent; border: none; padding: 0; }"
                "QToolButton::menu-indicator { image: none; width: 0; }"
                "QToolButton:hover { background: rgba(127, 127, 127, 0.12); border-radius: 4px; }"
            )
        except Exception:
            text_map = {"start": "Start", "stop": "Stop", "restart": "Restart"}
            btn = QPushButton(text_map[kind])
        btn.setToolTip(tooltip)
        btn.setMenu(menu_builder())
        return btn

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._poller.stop()
        self._poller.wait(1500)
        super().closeEvent(event)

    def _kick_poll(self) -> None:
        self._poller.set_stack(self._stack)

    def _on_health(self, h: _Health) -> None:
        self._api.set_state("ok" if h.api_ok else "bad", h.api_caption)
        self._bot.set_state(h.bot_state, h.bot_caption)
        self._listener.set_state(h.listener_state, h.listener_caption)
        self._ea.set_state(h.ea_state, h.ea_caption)
        self._snapshot.set_state(h.snapshot_state, h.snapshot_caption)

    def _build_service_menu(self, verb: str, handler) -> QMenu:
        """Shared per-service + all-services submenu. `verb` is the
        leading word ("Start"/"Stop"/"Restart"); `handler` is the
        callable that receives the list of service names to act on."""
        menu = QMenu(self)
        api_svc, bot_svc, listener_svc = self._stack.service_names
        for label, name in (
            (f"{verb} API ({api_svc})", api_svc),
            (f"{verb} Bot ({bot_svc})", bot_svc),
            (f"{verb} Listener ({listener_svc})", listener_svc),
        ):
            act = menu.addAction(label)
            act.triggered.connect(lambda _checked=False, n=name: handler([n]))
        menu.addSeparator()
        all_names = list(self._stack.service_names)
        act_all = menu.addAction(f"{verb} all")
        act_all.triggered.connect(lambda _checked=False: handler(all_names))
        return menu

    def _build_start_menu(self) -> QMenu:
        return self._build_service_menu("Start", self._do_start)

    def _build_stop_menu(self) -> QMenu:
        return self._build_service_menu("Stop", self._do_stop)

    def _build_restart_menu(self) -> QMenu:
        return self._build_service_menu("Restart", self._do_restart)

    def _do_start(self, names: list[str]) -> None:
        for n in names:
            nssm_client.nssm_start(n)
        self._kick_poll()

    def _do_stop(self, names: list[str]) -> None:
        for n in names:
            nssm_client.nssm_stop(n)
        self._kick_poll()

    def _do_restart(self, names: list[str]) -> None:
        for n in names:
            nssm_client.nssm_restart(n)
        self._kick_poll()
        self._kick_poll()
