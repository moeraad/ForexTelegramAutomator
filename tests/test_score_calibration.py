"""Tests for scripts/score_calibration.py.

Hermetic: seeds an in-memory DB with synthetic actions + positions
covering all four buckets (avoid/weak/moderate/strong), each with one
winner and one loser, then asserts the bucket-summary math.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from src.db import connect, init_schema

# scripts/ is not a package — load the module by path so test runs work
# without an extra setup.cfg packaging entry.
_SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "score_calibration.py"
_spec = importlib.util.spec_from_file_location("score_calibration", _SCRIPT_PATH)
_score_calibration = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["score_calibration"] = _score_calibration
_spec.loader.exec_module(_score_calibration)  # type: ignore[union-attr]


# ---- pure helpers -------------------------------------------------------


def test_bucket_for_boundaries():
    assert _score_calibration._bucket_for(0).startswith("avoid")
    assert _score_calibration._bucket_for(39).startswith("avoid")
    assert _score_calibration._bucket_for(40).startswith("weak")
    assert _score_calibration._bucket_for(59).startswith("weak")
    assert _score_calibration._bucket_for(60).startswith("moderate")
    assert _score_calibration._bucket_for(79).startswith("moderate")
    assert _score_calibration._bucket_for(80).startswith("strong")
    assert _score_calibration._bucket_for(100).startswith("strong")
    assert _score_calibration._bucket_for(-5) is None
    assert _score_calibration._bucket_for(101) is None


def test_r_multiple_basic():
    """BUY @ 4700, SL 4690, vol 1.0, +$1500 P&L, tick_value 100.
    planned_risk = 10 * 1.0 * 100 = $1000 -> R = 1500/1000 = 1.5."""
    rm = _score_calibration._r_multiple(
        side="BUY", entry=4700.0, sl=4690.0, volume=1.0,
        pnl=1500.0, tick_value=100.0,
    )
    assert abs(rm - 1.5) < 1e-9


def test_r_multiple_loss_negative():
    """SELL @ 4700, SL 4710, vol 0.5, -$300 P&L, tick_value 100.
    planned_risk = 10 * 0.5 * 100 = $500 -> R = -300/500 = -0.6."""
    rm = _score_calibration._r_multiple(
        side="SELL", entry=4700.0, sl=4710.0, volume=0.5,
        pnl=-300.0, tick_value=100.0,
    )
    assert abs(rm - (-0.6)) < 1e-9


def test_r_multiple_returns_none_on_zero_risk():
    """Degenerate signal where entry == sl -> None (skip in calibration)."""
    assert _score_calibration._r_multiple(
        "BUY", 4700.0, 4700.0, 1.0, 100.0, 100.0,
    ) is None


def test_r_multiple_returns_none_on_missing():
    assert _score_calibration._r_multiple("BUY", None, 4690.0, 1.0, 100.0, 100.0) is None
    assert _score_calibration._r_multiple("BUY", 4700.0, None, 1.0, 100.0, 100.0) is None


# ---- collect_rows + render_report end-to-end ---------------------------


def _seed(conn, *, score: int, pnl: float, ticket: int):
    """Insert one OPEN action with embedded evaluation + a closed position
    referencing it. Entry/SL/volume are constant — the only knobs the
    test cares about are score and realized_pnl."""
    payload = {
        "side": "BUY",
        "entry_low": 4700, "entry_high": 4702,
        "sl": 4690, "tps": [4720, 4730, 4750],
        "evaluation": {
            "score": score,
            "verdict": "x",
            "key_factor": "fixture",
            "summary": "fixture",
            "factors": {},
            "data_quality": "full",
            "missing": [],
            "evaluated_at": "2026-05-09T12:00:00+00:00",
        },
    }
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'executed')",
        (json.dumps(payload),),
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, partial_close_count, "
        "entry_price, sl, tp, exit_price, realized_pnl, "
        "opened_at, closed_at, close_reason, status) "
        "VALUES(?, ?, 'XAUUSD', 'BUY', 1.0, 1.0, 0, 4701.0, 4690.0, 4720.0, "
        "       4710.0, ?, '2026-05-09T11:00:00+00:00', "
        "       '2026-05-09T12:00:00+00:00', 'mt5_tp', 'closed')",
        (cur.lastrowid, ticket, pnl),
    )


def test_collect_rows_buckets_and_skips(tmp_path):
    conn = connect(str(tmp_path / "calib.db"))
    init_schema(conn)
    # 8 trades: 2 per bucket, one win one loss per bucket.
    _seed(conn, score=20, pnl=+100.0, ticket=1)   # avoid winner
    _seed(conn, score=20, pnl=-300.0, ticket=2)   # avoid loser
    _seed(conn, score=50, pnl=+200.0, ticket=3)   # weak winner
    _seed(conn, score=50, pnl=-150.0, ticket=4)   # weak loser
    _seed(conn, score=70, pnl=+800.0, ticket=5)   # moderate winner
    _seed(conn, score=70, pnl=-100.0, ticket=6)   # moderate loser
    _seed(conn, score=90, pnl=+1500.0, ticket=7)  # strong winner
    _seed(conn, score=90, pnl=+50.0, ticket=8)    # strong winner #2 (cap)

    # One row missing realized_pnl — should be silently skipped.
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'executed')",
        (json.dumps({"side": "BUY", "entry_low": 4700, "entry_high": 4702,
                     "sl": 4690, "tps": [4720],
                     "evaluation": {"score": 70}}),),
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "original_volume, entry_price, sl, tp, opened_at, closed_at, "
        "close_reason, status) "
        "VALUES(?, 999, 'XAUUSD', 'BUY', 1.0, 1.0, 4701.0, 4690.0, 4720.0, "
        "       '2026-05-09T11:00:00+00:00', '2026-05-09T12:00:00+00:00', "
        "       'mt5_tp', 'closed')",
        (cur.lastrowid,),
    )

    rows = _score_calibration.collect_rows(conn, tick_value=100.0)
    assert len(rows) == 8

    # Bucket distribution
    by_bucket = {}
    for r in rows:
        by_bucket.setdefault(r["bucket"], []).append(r)
    assert len(by_bucket) == 4
    for bucket_rows in by_bucket.values():
        assert len(bucket_rows) == 2

    # R-multiple sanity for one row: vol=1.0, |entry-sl|=11, tick=100
    # planned_risk = 1100; pnl=100 -> R≈0.0909
    avoid_winner = next(r for r in rows if r["score"] == 20 and r["realized_pnl"] > 0)
    assert avoid_winner["r_multiple"] is not None
    assert abs(avoid_winner["r_multiple"] - (100.0 / 1100.0)) < 1e-6


def test_render_report_no_rows_handles_gracefully():
    out = _score_calibration.render_report([])
    assert "Eligible closed positions" in out
    assert "0" in out
    # Must not crash on empty input.
    assert "Bucket summary" not in out


def test_render_report_with_rows_includes_all_buckets(tmp_path):
    conn = connect(str(tmp_path / "calib.db"))
    init_schema(conn)
    _seed(conn, score=20, pnl=+50.0, ticket=1)
    _seed(conn, score=70, pnl=-100.0, ticket=2)
    _seed(conn, score=90, pnl=+500.0, ticket=3)
    rows = _score_calibration.collect_rows(conn, tick_value=100.0)
    report = _score_calibration.render_report(rows)
    # All four bucket labels must appear (even empty ones — the row shows '0').
    assert "avoid" in report
    assert "weak" in report
    assert "moderate" in report
    assert "strong" in report
    assert "Top 5 winners" in report
    assert "Top 5 losers" in report
