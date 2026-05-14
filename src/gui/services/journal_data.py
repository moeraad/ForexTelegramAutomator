"""Closed-trade journal queries: rows, summary stats, equity curve."""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


@dataclass(frozen=True)
class TradeRow:
    ticket: int
    action_id: int | None
    side: str
    volume: float
    original_volume: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    close_reason: str
    opened_at: str
    closed_at: str


@dataclass(frozen=True)
class JournalSummary:
    total_pnl: float
    total_trades: int
    win_count: int
    loss_count: int
    best: float
    worst: float
    avg_pnl: float

    @property
    def win_rate(self) -> float:
        if self.total_trades == 0:
            return 0.0
        return self.win_count / self.total_trades


@dataclass(frozen=True)
class EquityPoint:
    closed_at: datetime
    cumulative_pnl: float


def _since_iso(days: int | None) -> str | None:
    if days is None:
        return None
    if days == 0:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _parse_dt(raw: str) -> datetime:
    s = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def closed_trades(db_path: Path, days: int | None = 30) -> list[TradeRow]:
    if not db_path.exists():
        return []
    since = _since_iso(days)
    sql = (
        "SELECT mt5_ticket, action_id, side, volume, original_volume, "
        "entry_price, exit_price, realized_pnl, close_reason, opened_at, closed_at "
        "FROM positions WHERE status='closed'"
    )
    params: tuple = ()
    if since is not None:
        sql += " AND closed_at >= ?"
        params = (since,)
    sql += " ORDER BY closed_at DESC"
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    return [
        TradeRow(
            ticket=int(r["mt5_ticket"]),
            action_id=int(r["action_id"]) if r["action_id"] is not None else None,
            side=str(r["side"]).upper(),
            volume=float(r["volume"] or 0),
            original_volume=float(r["original_volume"] or r["volume"] or 0),
            entry_price=float(r["entry_price"] or 0),
            exit_price=float(r["exit_price"] or 0),
            realized_pnl=float(r["realized_pnl"] or 0),
            close_reason=str(r["close_reason"] or ""),
            opened_at=str(r["opened_at"] or ""),
            closed_at=str(r["closed_at"] or ""),
        )
        for r in rows
    ]


def summary(trades: list[TradeRow]) -> JournalSummary:
    if not trades:
        return JournalSummary(0.0, 0, 0, 0, 0.0, 0.0, 0.0)
    pnls = [t.realized_pnl for t in trades]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    return JournalSummary(
        total_pnl=sum(pnls),
        total_trades=len(pnls),
        win_count=wins,
        loss_count=losses,
        best=max(pnls),
        worst=min(pnls),
        avg_pnl=sum(pnls) / len(pnls),
    )


@dataclass(frozen=True)
class DailyPnl:
    day: str           # YYYY-MM-DD
    pnl: float
    trade_count: int


def pnl_by_day(trades: list[TradeRow]) -> list[DailyPnl]:
    """One bucket per UTC calendar day, oldest day first."""
    by_day: dict[str, tuple[float, int]] = {}
    for t in trades:
        if not t.closed_at:
            continue
        try:
            dt = _parse_dt(t.closed_at)
        except ValueError:
            continue
        key = dt.date().isoformat()
        prev_pnl, prev_n = by_day.get(key, (0.0, 0))
        by_day[key] = (prev_pnl + t.realized_pnl, prev_n + 1)
    return [
        DailyPnl(day=k, pnl=v[0], trade_count=v[1])
        for k, v in sorted(by_day.items())
    ]


def pnl_by_close_reason(trades: list[TradeRow]) -> dict[str, tuple[float, int]]:
    """{close_reason: (sum_pnl, count)}, sorted by count descending in the caller."""
    out: dict[str, tuple[float, int]] = {}
    for t in trades:
        reason = t.close_reason or "(unknown)"
        prev_pnl, prev_n = out.get(reason, (0.0, 0))
        out[reason] = (prev_pnl + t.realized_pnl, prev_n + 1)
    return out


def equity_curve(trades: list[TradeRow]) -> list[EquityPoint]:
    """Equity curve from closed trades, oldest-first cumulative.

    The trades list is newest-first; this function flips it to render
    chronologically.
    """
    if not trades:
        return []
    by_close = sorted(trades, key=lambda t: t.closed_at)
    cum = 0.0
    points: list[EquityPoint] = []
    for t in by_close:
        if not t.closed_at:
            continue
        try:
            dt = _parse_dt(t.closed_at)
        except ValueError:
            continue
        cum += t.realized_pnl
        points.append(EquityPoint(closed_at=dt, cumulative_pnl=cum))
    return points
