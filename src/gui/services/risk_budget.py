"""Risk-budget enforcement: monitor live metrics + auto-halt on breach.

Config lives in settings.risk_budget (JSON). Monitor polls every 10 s:
- today realized PnL  (vs daily_dd_limit_usd; values are NEGATIVE losses)
- week realized PnL   (vs weekly_dd_limit_usd)
- open lots total     (vs max_open_lots)
- today opened trades (vs daily_trade_count_limit)

On breach (if enabled), the monitor flips settings.kill_switch='on' and
emits a `triggered` signal. The user must clear the halt manually — the
monitor never auto-resumes.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal


@dataclass(frozen=True)
class RiskConfig:
    enabled: bool = False
    daily_dd_limit_usd: float = -200.0
    weekly_dd_limit_usd: float = -500.0
    max_open_lots: float = 0.10
    daily_trade_count_limit: int = 20


@dataclass(frozen=True)
class RiskMetrics:
    today_pnl: float
    week_pnl: float
    open_lots: float
    today_trade_count: int
    today_wins: int = 0
    today_losses: int = 0


def _today_start_iso() -> str:
    now = datetime.now(timezone.utc)
    return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()


def _week_start_iso() -> str:
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return (midnight - timedelta(days=now.weekday())).isoformat()


def load_config(db_path: Path) -> RiskConfig:
    if not db_path.exists():
        return RiskConfig()
    try:
        with sqlite3.connect(str(db_path)) as conn:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='risk_budget'"
            ).fetchone()
    except sqlite3.Error:
        return RiskConfig()
    if row is None or not row[0]:
        return RiskConfig()
    try:
        data = json.loads(row[0])
    except json.JSONDecodeError:
        return RiskConfig()
    return RiskConfig(
        enabled=bool(data.get("enabled", False)),
        daily_dd_limit_usd=float(data.get("daily_dd_limit_usd", -200.0)),
        weekly_dd_limit_usd=float(data.get("weekly_dd_limit_usd", -500.0)),
        max_open_lots=float(data.get("max_open_lots", 0.10)),
        daily_trade_count_limit=int(data.get("daily_trade_count_limit", 20)),
    )


def save_config(db_path: Path, cfg: RiskConfig) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('risk_budget', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (json.dumps(asdict(cfg)),),
        )
        conn.commit()


def compute_metrics(db_path: Path) -> RiskMetrics:
    if not db_path.exists():
        return RiskMetrics(0.0, 0.0, 0.0, 0)
    today_start = _today_start_iso()
    week_start = _week_start_iso()
    with sqlite3.connect(str(db_path)) as conn:
        today_pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
            "WHERE status='closed' AND closed_at >= ?",
            (today_start,),
        ).fetchone()[0] or 0.0
        week_pnl = conn.execute(
            "SELECT COALESCE(SUM(realized_pnl), 0) FROM positions "
            "WHERE status='closed' AND closed_at >= ?",
            (week_start,),
        ).fetchone()[0] or 0.0
        open_lots = conn.execute(
            "SELECT COALESCE(SUM(volume), 0) FROM positions WHERE status='open'"
        ).fetchone()[0] or 0.0
        today_trades = conn.execute(
            "SELECT COUNT(*) FROM positions WHERE opened_at >= ?",
            (today_start,),
        ).fetchone()[0] or 0
        today_wins = conn.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE status='closed' AND closed_at >= ? AND realized_pnl > 0",
            (today_start,),
        ).fetchone()[0] or 0
        today_losses = conn.execute(
            "SELECT COUNT(*) FROM positions "
            "WHERE status='closed' AND closed_at >= ? AND realized_pnl < 0",
            (today_start,),
        ).fetchone()[0] or 0
    return RiskMetrics(
        today_pnl=float(today_pnl),
        week_pnl=float(week_pnl),
        open_lots=float(open_lots),
        today_trade_count=int(today_trades),
        today_wins=int(today_wins),
        today_losses=int(today_losses),
    )


def check_breaches(cfg: RiskConfig, m: RiskMetrics) -> list[str]:
    breaches: list[str] = []
    if m.today_pnl <= cfg.daily_dd_limit_usd:
        breaches.append(
            f"daily drawdown: today PnL ${m.today_pnl:+.2f} ≤ limit ${cfg.daily_dd_limit_usd:+.2f}"
        )
    if m.week_pnl <= cfg.weekly_dd_limit_usd:
        breaches.append(
            f"weekly drawdown: week PnL ${m.week_pnl:+.2f} ≤ limit ${cfg.weekly_dd_limit_usd:+.2f}"
        )
    if m.open_lots > cfg.max_open_lots:
        breaches.append(
            f"open lots: {m.open_lots:.2f} > limit {cfg.max_open_lots:.2f}"
        )
    if m.today_trade_count > cfg.daily_trade_count_limit:
        breaches.append(
            f"daily trade count: {m.today_trade_count} > limit {cfg.daily_trade_count_limit}"
        )
    return breaches


def _flip_kill_switch_on(db_path: Path) -> None:
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('kill_switch', 'on') "
            "ON CONFLICT(key) DO UPDATE SET value='on'"
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES('kill_switch_reason', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"risk_budget@{datetime.now(timezone.utc).isoformat()}",),
        )
        conn.commit()


class RiskMonitor(QObject):
    metrics_updated = Signal(object)
    config_changed = Signal(object)
    triggered = Signal(list)

    def __init__(self, db_path: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._db_path = db_path
        self._config = load_config(db_path)
        self._last_breach: list[str] = []
        self._timer = QTimer(self)
        self._timer.setInterval(10_000)
        self._timer.timeout.connect(self._tick)

    @property
    def config(self) -> RiskConfig:
        return self._config

    def start(self) -> None:
        self._tick()
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def rebind(self, db_path: Path) -> None:
        self._db_path = db_path
        self._config = load_config(db_path)
        self.config_changed.emit(self._config)
        self._last_breach = []
        self._tick()

    def update_config(self, cfg: RiskConfig) -> None:
        save_config(self._db_path, cfg)
        self._config = cfg
        self.config_changed.emit(cfg)
        self._tick()

    def _tick(self) -> None:
        try:
            metrics = compute_metrics(self._db_path)
        except sqlite3.Error:
            return
        self.metrics_updated.emit(metrics)
        breaches = check_breaches(self._config, metrics)
        if breaches and breaches != self._last_breach and self._config.enabled:
            _flip_kill_switch_on(self._db_path)
            self.triggered.emit(breaches)
        self._last_breach = breaches
