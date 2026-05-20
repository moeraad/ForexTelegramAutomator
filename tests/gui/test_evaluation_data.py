"""Tests for the Evaluation view's data layer.

GUI rendering is exercised via the existing GUI smoke harness (qtbot);
this file targets the pure SQL + aggregation logic directly so the
calibration / regime math is testable without a Qt event loop.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone

import pytest

from src.db import connect, init_schema
from src.gui.services.evaluation_data import (
    calibration_buckets,
    calibration_status,
    evaluated_trades,
    regime_buckets,
)


def _setup(tmp_path):
    db_path = tmp_path / "e.db"
    conn = connect(str(db_path))
    init_schema(conn)
    return conn, db_path


def _insert_eval_trade(
    conn: sqlite3.Connection,
    *,
    ticket: int,
    action_type: str = "OPEN",
    side: str = "BUY",
    realized_pnl: float = 10.0,
    score: int = 65,
    verdict: str = "moderate",
    regime_summary: str = "real_yield_down_dxy_down|normal|aligned_up|ny_overlap|clean",
    sizing_p_profit: float | None = 0.62,
    sizing_multiplier: float = 1.4,
    closed_at: str | None = None,
) -> int:
    """Seed one action + closed position. Returns action_id."""
    payload = {
        "side": side,
        "evaluation": {
            "score": score,
            "verdict": verdict,
            "regime": {"summary": regime_summary},
            "sizing": {
                "multiplier": sizing_multiplier,
                "skip": False,
                "p_profit_used": sizing_p_profit,
                "horizon_minutes": 240,
                "reason": "test",
            },
        },
    }
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, "
        "executed_at, created_at) VALUES(?, ?, 'executed', ?, ?)",
        (action_type, json.dumps(payload),
         datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    aid = cur.lastrowid
    closed = closed_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, entry_price, exit_price, realized_pnl, "
        "status, opened_at, closed_at, close_reason) "
        "VALUES(?,?, 'XAUUSD', ?, 0.1, 0.1, 4700, 4710, ?, 'closed', ?, ?, 'tp1')",
        (aid, ticket, side, realized_pnl,
         datetime.now(timezone.utc).isoformat(),
         closed),
    )
    return aid


# ---- evaluated_trades ----------------------------------------------

def test_evaluated_trades_joins_action_and_position(tmp_path):
    conn, db_path = _setup(tmp_path)
    _insert_eval_trade(conn, ticket=1001, score=70, realized_pnl=15.0)
    out = evaluated_trades(db_path, days=None)
    assert len(out) == 1
    t = out[0]
    assert t.ticket == 1001
    assert t.side == "BUY"
    assert t.realized_pnl == 15.0
    assert t.evaluation is not None
    assert t.evaluation["score"] == 70


def test_evaluated_trades_filters_to_open_shape_action_types(tmp_path):
    """Only OPEN / OPEN_INSTANT / REOPEN_LAST / REINFORCE — CLOSE_PARTIAL
    and MOVE_SL_BE don't open positions and shouldn't appear."""
    conn, db_path = _setup(tmp_path)
    _insert_eval_trade(conn, ticket=2001, action_type="OPEN")
    _insert_eval_trade(conn, ticket=2002, action_type="REOPEN_LAST")
    # Plant a CLOSE_PARTIAL with the same shape — must NOT appear.
    payload = {"fraction": 0.5}
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('CLOSE_PARTIAL', ?, 'executed')",
        (json.dumps(payload),),
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, entry_price, exit_price, realized_pnl, "
        "status, opened_at, closed_at, close_reason) "
        "VALUES(?, 2003, 'XAUUSD', 'BUY', 0.05, 0.1, 4700, 4710, 5.0, "
        "'closed', ?, ?, 'partial')",
        (aid, datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    out = evaluated_trades(db_path, days=None)
    tickets = {t.ticket for t in out}
    assert tickets == {2001, 2002}


def test_evaluated_trades_missing_db_returns_empty(tmp_path):
    out = evaluated_trades(tmp_path / "missing.db", days=None)
    assert out == []


def test_evaluated_trades_tolerates_action_without_evaluation(tmp_path):
    """Pre-evaluator trades show up but with `evaluation is None`."""
    conn, db_path = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, entry_price, exit_price, realized_pnl, "
        "status, opened_at, closed_at, close_reason) "
        "VALUES(?, 3001, 'XAUUSD', 'BUY', 0.1, 0.1, 4700, 4710, 10.0, "
        "'closed', ?, ?, 'tp1')",
        (aid, datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    out = evaluated_trades(db_path, days=None)
    assert len(out) == 1
    assert out[0].evaluation is None


# ---- calibration_buckets -------------------------------------------

def test_calibration_buckets_aggregates_by_band(tmp_path):
    conn, db_path = _setup(tmp_path)
    # Two trades in 60-69 band, one win one loss.
    _insert_eval_trade(conn, ticket=4001, score=62, realized_pnl=15.0)
    _insert_eval_trade(conn, ticket=4002, score=68, realized_pnl=-5.0)
    # One in 80+, win.
    _insert_eval_trade(conn, ticket=4003, score=82, realized_pnl=20.0)
    trades = evaluated_trades(db_path, days=None)
    buckets = calibration_buckets(trades)
    by_label = {b.band_label: b for b in buckets}
    assert by_label["60-69"].n == 2
    assert by_label["60-69"].wins == 1
    assert by_label["60-69"].win_rate == 0.5
    assert by_label["80+"].n == 1
    assert by_label["80+"].win_rate == 1.0


def test_calibration_buckets_p_profit_avg_when_present(tmp_path):
    """When sizing.p_profit_used is set on every trade, the bucket's
    p_profit_avg is the mean — the synthesizer's claim for the band."""
    conn, db_path = _setup(tmp_path)
    _insert_eval_trade(conn, ticket=5001, score=62, sizing_p_profit=0.58)
    _insert_eval_trade(conn, ticket=5002, score=64, sizing_p_profit=0.62)
    trades = evaluated_trades(db_path, days=None)
    buckets = {b.band_label: b for b in calibration_buckets(trades)}
    assert buckets["60-69"].p_profit_avg == pytest.approx(0.60, abs=0.001)


def test_calibration_buckets_p_profit_none_when_missing(tmp_path):
    conn, db_path = _setup(tmp_path)
    _insert_eval_trade(conn, ticket=6001, score=62, sizing_p_profit=None)
    trades = evaluated_trades(db_path, days=None)
    buckets = {b.band_label: b for b in calibration_buckets(trades)}
    assert buckets["60-69"].n == 1
    assert buckets["60-69"].p_profit_avg is None


def test_calibration_buckets_skips_trades_without_evaluation(tmp_path):
    """Trades that fired without an evaluation can't be calibrated;
    they must not pollute the bucket counts."""
    conn, db_path = _setup(tmp_path)
    # One scored trade.
    _insert_eval_trade(conn, ticket=7001, score=65)
    # One unscored.
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, entry_price, exit_price, realized_pnl, "
        "status, opened_at, closed_at, close_reason) "
        "VALUES(?, 7002, 'XAUUSD', 'BUY', 0.1, 0.1, 4700, 4710, 10.0, "
        "'closed', ?, ?, 'tp1')",
        (aid, datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    trades = evaluated_trades(db_path, days=None)
    buckets = calibration_buckets(trades)
    # 60-69 band has 1, not 2.
    by_label = {b.band_label: b for b in buckets}
    assert by_label["60-69"].n == 1


# ---- regime_buckets -------------------------------------------------

def test_regime_buckets_groups_by_regime_summary(tmp_path):
    conn, db_path = _setup(tmp_path)
    tag1 = "real_yield_down_dxy_down|normal|aligned_up|ny_overlap|clean"
    tag2 = "real_yield_up_dxy_up|normal|aligned_down|london|clean"
    _insert_eval_trade(conn, ticket=8001, regime_summary=tag1, realized_pnl=10.0)
    _insert_eval_trade(conn, ticket=8002, regime_summary=tag1, realized_pnl=-5.0)
    _insert_eval_trade(conn, ticket=8003, regime_summary=tag1, realized_pnl=15.0)
    _insert_eval_trade(conn, ticket=8004, regime_summary=tag2, realized_pnl=5.0)
    _insert_eval_trade(conn, ticket=8005, regime_summary=tag2, realized_pnl=-10.0)
    _insert_eval_trade(conn, ticket=8006, regime_summary=tag2, realized_pnl=-15.0)
    trades = evaluated_trades(db_path, days=None)
    buckets = regime_buckets(trades, min_samples=2)
    by_tag = {b.regime_summary: b for b in buckets}
    assert by_tag[tag1].n == 3
    assert by_tag[tag1].wins == 2
    assert by_tag[tag1].win_rate == pytest.approx(2/3, abs=0.001)
    assert by_tag[tag2].n == 3
    assert by_tag[tag2].wins == 1


def test_regime_buckets_filters_below_min_samples(tmp_path):
    conn, db_path = _setup(tmp_path)
    tag = "tag_with_only_two|normal|flat|asian|clean"
    _insert_eval_trade(conn, ticket=9001, regime_summary=tag)
    _insert_eval_trade(conn, ticket=9002, regime_summary=tag)
    trades = evaluated_trades(db_path, days=None)
    out = regime_buckets(trades, min_samples=3)
    assert out == []


def test_regime_buckets_sorted_by_count_desc(tmp_path):
    conn, db_path = _setup(tmp_path)
    high = "freq_regime|normal|aligned_up|ny_overlap|clean"
    low = "rare_regime|elevated|mixed|london|clean"
    for i in range(5):
        _insert_eval_trade(conn, ticket=10000 + i, regime_summary=high)
    for i in range(3):
        _insert_eval_trade(conn, ticket=11000 + i, regime_summary=low)
    trades = evaluated_trades(db_path, days=None)
    out = regime_buckets(trades, min_samples=3)
    assert out[0].regime_summary == high
    assert out[1].regime_summary == low


def test_regime_buckets_skips_trades_without_regime_block(tmp_path):
    conn, db_path = _setup(tmp_path)
    # Action with evaluation but no regime key.
    payload = {"side": "BUY", "evaluation": {"score": 70}}  # no "regime"
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'executed')",
        (json.dumps(payload),),
    )
    aid = cur.lastrowid
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, entry_price, exit_price, realized_pnl, "
        "status, opened_at, closed_at, close_reason) "
        "VALUES(?, 12001, 'XAUUSD', 'BUY', 0.1, 0.1, 4700, 4710, 10.0, "
        "'closed', ?, ?, 'tp1')",
        (aid, datetime.now(timezone.utc).isoformat(),
         datetime.now(timezone.utc).isoformat()),
    )
    trades = evaluated_trades(db_path, days=None)
    out = regime_buckets(trades, min_samples=1)
    assert out == []


# ---- calibration_status --------------------------------------------

def test_calibration_status_no_data():
    """Zero trades -> 'no_data' band."""
    s = calibration_status([])
    assert s.band == "no_data"
    assert s.n_scored == 0
    assert s.n_with_p_profit == 0
    assert "no closed" in s.message.lower()


def test_calibration_status_uncalibrated_under_threshold(tmp_path):
    conn, db_path = _setup(tmp_path)
    for i in range(5):
        _insert_eval_trade(conn, ticket=20000 + i, score=60 + i)
    trades = evaluated_trades(db_path, days=None)
    s = calibration_status(trades)
    assert s.band == "uncalibrated"
    assert s.n_scored == 5


def test_calibration_status_partial_band(tmp_path):
    conn, db_path = _setup(tmp_path)
    for i in range(25):
        _insert_eval_trade(conn, ticket=21000 + i, score=50 + (i % 30))
    trades = evaluated_trades(db_path, days=None)
    s = calibration_status(trades)
    assert s.band == "partial"


def test_calibration_status_calibrating_at_threshold(tmp_path):
    conn, db_path = _setup(tmp_path)
    for i in range(60):
        _insert_eval_trade(conn, ticket=22000 + i, score=50 + (i % 30))
    trades = evaluated_trades(db_path, days=None)
    s = calibration_status(trades)
    assert s.band == "calibrating"
    assert s.n_scored == 60


def test_calibration_status_n_with_p_profit_counts_subset(tmp_path):
    """Only trades whose sizing.p_profit_used is non-None count toward
    the p_profit subset — used by the calibration chart's overlay."""
    conn, db_path = _setup(tmp_path)
    _insert_eval_trade(conn, ticket=23001, sizing_p_profit=0.6)
    _insert_eval_trade(conn, ticket=23002, sizing_p_profit=None)
    trades = evaluated_trades(db_path, days=None)
    s = calibration_status(trades)
    assert s.n_scored == 2
    assert s.n_with_p_profit == 1
