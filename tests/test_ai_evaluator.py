"""Tests for src.ai_evaluator (directional-bias rubric — revised 2026-05-09)
plus the /market/snapshot and /actions/latest_open_evaluation API endpoints.

The evaluator no longer judges signal mechanics (R:R, SL placement, TP
plausibility) — it scores the proposed DIRECTION against the current
market regime. Signal-mechanic tests from the prior rubric (R:R math,
channel win-rate) have been removed; their replacements cover the new
helpers (`_session_label`, `_today_range_used`, `_get_recent_precedent`,
`_get_macro_snapshot`)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.ai_evaluator import (
    _get_macro_snapshot,
    _get_market_mid,
    _get_market_snapshot,
    _get_recent_precedent,
    _parse_evaluator_response,
    _session_label,
    _today_range_used,
    _verdict_from_score,
    build_evaluator_input,
    evaluate_signal,
)
from src.api import build_app
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "v.db"))
    init_schema(conn)
    return conn


# ---- _verdict_from_score ------------------------------------------------


def test_verdict_thresholds():
    assert _verdict_from_score(95) == "strong"
    assert _verdict_from_score(80) == "strong"
    assert _verdict_from_score(79) == "moderate"
    assert _verdict_from_score(60) == "moderate"
    assert _verdict_from_score(59) == "weak"
    assert _verdict_from_score(40) == "weak"
    assert _verdict_from_score(39) == "avoid"
    assert _verdict_from_score(0) == "avoid"


# ---- _session_label -----------------------------------------------------


def test_session_label_boundaries():
    """UTC hour bands. Weekend short-circuits."""
    def at(year, mon, day, hour):
        return datetime(year, mon, day, hour, 0, tzinfo=timezone.utc)
    # 2026-05-11 is a Monday (weekday=0)
    assert _session_label(at(2026, 5, 11, 3)) == "asian"
    assert _session_label(at(2026, 5, 11, 7)) == "london"
    assert _session_label(at(2026, 5, 11, 11)) == "london"
    assert _session_label(at(2026, 5, 11, 12)) == "ny_overlap"
    assert _session_label(at(2026, 5, 11, 15)) == "ny_overlap"
    assert _session_label(at(2026, 5, 11, 16)) == "ny_afternoon"
    assert _session_label(at(2026, 5, 11, 21)) == "late"
    assert _session_label(at(2026, 5, 11, 23)) == "late"
    # 2026-05-09 = Saturday, 2026-05-10 = Sunday
    assert _session_label(at(2026, 5, 9, 12)) == "weekend"
    assert _session_label(at(2026, 5, 10, 12)) == "weekend"


# ---- _today_range_used --------------------------------------------------


def test_today_range_used_basic():
    d1 = {"open": 4700, "high": 4730, "low": 4690, "close": 4720}
    res = _today_range_used(d1, mid=4725.0)
    assert res is not None
    assert res["range_today"] == 40.0
    # mid 4725 is 35 above low 4690 in a 40-pt range -> 87.5%
    assert abs(res["position_in_range_pct"] - 87.5) < 1e-6
    # mid 4725 is 25 above open 4700 -> +0.532%
    assert abs(res["from_open_pct"] - (25.0 / 4700.0 * 100.0)) < 1e-6


def test_today_range_used_returns_none_when_inputs_missing():
    assert _today_range_used(None, 4720) is None
    assert _today_range_used({"open": 4700, "high": 4730, "low": 4690}, None) is None
    # Degenerate range (high == low)
    assert _today_range_used({"open": 4700, "high": 4700, "low": 4700}, 4700) is None


# ---- _get_market_snapshot / _get_market_mid -----------------------------


def test_get_market_snapshot_missing(tmp_path):
    conn = _setup(tmp_path)
    snap, age = _get_market_snapshot(conn, "XAUUSD")
    assert snap is None and age is None


def test_get_market_snapshot_present_with_extensions(tmp_path):
    """v2 snapshot may include directional-rubric extensions
    (d1/adr20/adx_h1/h1_recent_closes). Reader returns them as-is."""
    conn = _setup(tmp_path)
    payload = {
        "m15": {"open": 4685, "high": 4690, "low": 4682, "close": 4687, "atr14": 4.5},
        "h1":  {"open": 4675, "high": 4692, "low": 4672, "close": 4687, "atr14": 12.0},
        "h4":  {"open": 4660, "high": 4695, "low": 4655, "close": 4687, "atr14": 28.0},
        "d1":  {"open": 4670, "high": 4695, "low": 4660, "close": 4687,
                "atr14": 35.0, "sma50": 4650, "sma200": 4500},
        "adr20": 32.5,
        "adx_h1": 28.4,
        "h1_recent_closes": [4670, 4675, 4680, 4685, 4687],
    }
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("market_snapshot_XAUUSD", json.dumps(payload)),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("market_snapshot_XAUUSD_at", datetime.now(timezone.utc).isoformat()),
    )
    snap, age = _get_market_snapshot(conn, "XAUUSD")
    assert snap is not None
    assert snap["d1"]["sma50"] == 4650
    assert snap["adr20"] == 32.5
    assert age is not None and age >= 0


def test_get_market_mid_missing(tmp_path):
    conn = _setup(tmp_path)
    mid, _ = _get_market_mid(conn, "XAUUSD")
    assert mid is None


# ---- _get_macro_snapshot ------------------------------------------------


def test_get_macro_snapshot_missing(tmp_path):
    conn = _setup(tmp_path)
    snap, age = _get_macro_snapshot(conn)
    assert snap is None and age is None


def test_get_macro_snapshot_present(tmp_path):
    conn = _setup(tmp_path)
    payload = {
        "dxy": 102.45, "dxy_chg_pct": -0.32,
        "tnx": 4.21,   "tnx_chg_pct": 0.05,
        "vix": 16.8,   "vix_chg_pct": 1.4,
        "jpy": 156.2,  "jpy_chg_pct": -0.18,
        "oil": 78.4,   "oil_chg_pct": 0.6,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("macro_snapshot", json.dumps(payload)),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("macro_snapshot_at", datetime.now(timezone.utc).isoformat()),
    )
    snap, age = _get_macro_snapshot(conn)
    assert snap is not None
    assert snap["dxy"] == 102.45
    assert age is not None and age >= 0


def test_get_macro_snapshot_returns_age_when_stale(tmp_path):
    """Stale rows are returned with their (large) age — caller decides
    whether to use them. The `missing` list path in build_evaluator_input
    flips on stale based on the age, not on the reader."""
    conn = _setup(tmp_path)
    payload = {"dxy": 100.0, "dxy_chg_pct": 0.0,
               "fetched_at": "2025-01-01T00:00:00+00:00"}
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("macro_snapshot", json.dumps(payload)),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("macro_snapshot_at", "2025-01-01T00:00:00+00:00"),
    )
    snap, age = _get_macro_snapshot(conn)
    assert snap is not None
    assert age is not None and age > 60  # very stale


# ---- _get_recent_precedent ----------------------------------------------


def test_recent_precedent_no_trades(tmp_path):
    conn = _setup(tmp_path)
    res = _get_recent_precedent(conn, "XAUUSD")
    assert res["trades_today"] == 0
    assert res["consecutive_losses"] == 0
    assert res["last_close_reason"] is None


def test_recent_precedent_consecutive_sl_hits(tmp_path):
    """Three SL hits at the head of the recent window. consecutive_losses
    counts contiguous mt5_sl reasons starting from the most recent close
    and stopping as soon as a non-SL closure appears."""
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid
    base = datetime.now(timezone.utc)
    # Insert 4 closed positions: 3 most recent are SL hits, the 4th
    # (oldest) is a TP — so consecutive_losses must == 3.
    rows = [
        (10, "BUY",  "ai_close_full", base - timedelta(hours=8)),
        (11, "BUY",  "mt5_sl",        base - timedelta(hours=2)),
        (12, "SELL", "mt5_sl",        base - timedelta(hours=1)),
        (13, "BUY",  "mt5_sl",        base - timedelta(minutes=20)),
    ]
    for ticket, side, reason, ts in rows:
        conn.execute(
            "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
            "volume, original_volume, partial_close_count, "
            "entry_price, sl, tp, opened_at, closed_at, close_reason, status) "
            "VALUES(?, ?, 'XAUUSD', ?, 1.0, 1.0, 0, 4700, 4690, 4720, "
            "?, ?, ?, 'closed')",
            (aid, ticket, side, ts.isoformat(), ts.isoformat(), reason),
        )
    res = _get_recent_precedent(conn, "XAUUSD", lookback_hours=12)
    assert res["trades_today"] == 4
    assert res["consecutive_losses"] == 3
    assert res["last_close_reason"] == "mt5_sl"
    assert res["last_close_side"] == "BUY"
    assert res["minutes_since_last_close"] is not None
    assert res["minutes_since_last_close"] < 60


# ---- build_evaluator_input ----------------------------------------------


def test_build_evaluator_input_marks_missing_when_no_snapshot(tmp_path):
    """Empty DB: market price + ohlc_snapshot + macro all missing. Direction
    appears in the prompt; signal mechanics (entry/SL/TPs) do NOT (the
    evaluator no longer judges them)."""
    conn = _setup(tmp_path)
    signal = {
        "side": "BUY",
        "entry_low": 4553, "entry_high": 4555,
        "sl": 4540, "tps": [4570, 4580, 4600],
        "comment": "FXENGIN",
    }
    text, missing = build_evaluator_input(signal, conn, "XAUUSD")
    assert "ohlc_snapshot" in missing
    assert "market_price" in missing
    assert "macro_snapshot" in missing
    assert "PROPOSED DIRECTION:" in text
    assert "side=BUY" in text
    assert "MULTI-TIMEFRAME OHLC" in text
    assert "MACRO" in text
    assert "SESSION + TIME:" in text
    # The new prompt MUST NOT leak signal mechanics.
    assert "R:R" not in text
    assert "tps=[" not in text
    assert "sl=4540" not in text


def test_build_evaluator_input_includes_macro_when_present(tmp_path):
    """When the bot's macro feed has populated settings.macro_snapshot, the
    MACRO block in the prompt shows live values + intraday %change arrows."""
    conn = _setup(tmp_path)
    payload = {
        "dxy": 102.45, "dxy_chg_pct": -0.32,
        "tnx": 4.21,   "tnx_chg_pct": 0.05,
        "vix": 16.8,   "vix_chg_pct": 1.4,
        "jpy": 156.2,  "jpy_chg_pct": -0.18,
        "oil": 78.4,   "oil_chg_pct": 0.6,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
    }
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)",
                 ("macro_snapshot", json.dumps(payload)))
    conn.execute("INSERT INTO settings(key, value) VALUES(?, ?)",
                 ("macro_snapshot_at", datetime.now(timezone.utc).isoformat()))
    signal = {"side": "SELL", "entry_low": 4720, "entry_high": 4722,
              "sl": 4730, "tps": [4700]}
    text, missing = build_evaluator_input(signal, conn, "XAUUSD")
    assert "macro_snapshot" not in missing
    assert "DXY=102.45" in text
    assert "10Y nominal=4.21" in text
    assert "VIX=16.80" in text


# ---- _parse_evaluator_response -------------------------------------------


def test_parse_clean_json():
    raw = '{"score": 72, "verdict": "moderate", "summary": "ok"}'
    parsed = _parse_evaluator_response(raw)
    assert parsed["score"] == 72
    assert parsed["verdict"] == "moderate"


def test_parse_markdown_fenced_json():
    raw = "Here's my evaluation:\n```json\n{\"score\": 55, \"summary\":\"x\"}\n```\nDone."
    parsed = _parse_evaluator_response(raw)
    assert parsed["score"] == 55
    # verdict should be inferred from score
    assert parsed["verdict"] == "weak"


def test_parse_garbage_returns_none():
    assert _parse_evaluator_response("not json at all") is None
    assert _parse_evaluator_response("") is None


# ---- evaluate_signal end-to-end (mocked AI) -----------------------------


def test_evaluate_signal_full_path_reduced(tmp_path):
    """Empty DB -> snapshot + macro both missing -> data_quality=reduced
    + score capped at 70. factors keys reflect the new T1..C3 rubric
    (the AI returns them; the evaluator preserves whatever shape it gets
    as long as `score`/`verdict`/`missing` are sane)."""
    conn = _setup(tmp_path)
    signal = {
        "side": "BUY",
        "entry_low": 4553, "entry_high": 4555,
        "sl": 4540, "tps": [4570, 4580, 4600],
    }
    ai = MagicMock()
    ai.chat.return_value = json.dumps({
        "score": 78,
        "verdict": "moderate",
        "key_factor": "trend aligned across H1/H4/D1",
        "summary": "BUY agrees with all three timeframes.",
        "factors": {
            "T1": "D1 above SMA50, slope up",
            "T2": "H4 close > open",
            "T3": "H1 close > open",
            "T4": "data_quality_limited: no h1_recent_closes",
            "M1": "data_quality_limited: no d1",
            "M2": "data_quality_limited: no atr20",
            "M3": "data_quality_limited: no adx",
            "G1": "data_quality_limited: macro feed unavailable",
            "G2": "data_quality_limited: macro feed unavailable",
            "G3": "data_quality_limited: macro feed unavailable",
            "C1": "no veto known",
            "C2": "session ny_overlap",
            "C3": "no recent trades",
        },
    })
    result = evaluate_signal(signal, conn, ai, symbol="XAUUSD")
    assert result["data_quality"] == "reduced"
    assert result["score"] == 70   # capped from 78
    assert result["verdict"] == "moderate"
    assert "ohlc_snapshot" in result["missing"]
    assert "macro_snapshot" in result["missing"]
    assert set(result["factors"].keys()) >= {"T1", "M1", "G1", "C1"}
    assert "evaluated_at" in result


def test_evaluate_signal_handles_ai_error(tmp_path):
    conn = _setup(tmp_path)
    signal = {"side": "BUY", "entry_low": 4553, "entry_high": 4555,
              "sl": 4540, "tps": [4570]}
    ai = MagicMock()
    ai.chat.side_effect = RuntimeError("boom")
    result = evaluate_signal(signal, conn, ai, symbol="XAUUSD")
    assert result["verdict"] == "unavailable"
    assert "evaluator failed" in result["key_factor"]


# ---- API: POST /market/snapshot -----------------------------------------


def test_post_market_snapshot_writes_settings(tmp_path):
    """Old EA payload (m15/h1/h4 only) still validates and persists."""
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    body = {
        "symbol": "XAUUSD",
        "m15": {"open": 4685, "high": 4690, "low": 4682, "close": 4687, "atr14": 4.5},
        "h1":  {"open": 4675, "high": 4692, "low": 4672, "close": 4687, "atr14": 12.0},
        "h4":  {"open": 4660, "high": 4695, "low": 4655, "close": 4687, "atr14": 28.0},
    }
    r = client.post("/market/snapshot", json=body)
    assert r.status_code == 200
    raw = conn.execute(
        "SELECT value FROM settings WHERE key='market_snapshot_XAUUSD'"
    ).fetchone()
    saved = json.loads(raw["value"])
    assert saved["h1"]["close"] == 4687
    # No d1/adr20 keys when the EA didn't push them.
    assert "d1" not in saved
    assert "adr20" not in saved


def test_post_market_snapshot_writes_directional_extensions(tmp_path):
    """v2 EA payload with d1/adr20/adx_h1/h1_recent_closes is persisted
    in the same settings row."""
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    body = {
        "symbol": "XAUUSD",
        "m15": {"open": 4685, "high": 4690, "low": 4682, "close": 4687, "atr14": 4.5},
        "h1":  {"open": 4675, "high": 4692, "low": 4672, "close": 4687, "atr14": 12.0},
        "h4":  {"open": 4660, "high": 4695, "low": 4655, "close": 4687, "atr14": 28.0},
        "d1":  {"open": 4670, "high": 4695, "low": 4660, "close": 4687,
                "atr14": 35.0, "sma50": 4650.0, "sma200": 4500.0},
        "d1_prev": {"open": 4660, "high": 4675, "low": 4655, "close": 4670, "atr14": 33.0},
        "adr20": 32.5,
        "adx_h1": 28.4,
        "h1_recent_closes": [4670.0, 4675.0, 4680.0, 4685.0, 4687.0],
    }
    r = client.post("/market/snapshot", json=body)
    assert r.status_code == 200
    raw = conn.execute(
        "SELECT value FROM settings WHERE key='market_snapshot_XAUUSD'"
    ).fetchone()
    saved = json.loads(raw["value"])
    assert saved["d1"]["sma50"] == 4650.0
    assert saved["d1_prev"]["close"] == 4670
    assert saved["adr20"] == 32.5
    assert saved["adx_h1"] == 28.4
    assert saved["h1_recent_closes"] == [4670.0, 4675.0, 4680.0, 4685.0, 4687.0]


def test_post_market_snapshot_rejects_unsupported_symbol(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    body = {
        "symbol": "EURUSD",
        "m15": {"open": 1, "high": 1, "low": 1, "close": 1, "atr14": 0.001},
        "h1":  {"open": 1, "high": 1, "low": 1, "close": 1, "atr14": 0.001},
        "h4":  {"open": 1, "high": 1, "low": 1, "close": 1, "atr14": 0.001},
    }
    r = client.post("/market/snapshot", json=body)
    assert r.status_code == 400


# ---- API: GET /actions/latest_open_evaluation ---------------------------


def test_latest_open_evaluation_404_when_no_open(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.get("/actions/latest_open_evaluation")
    assert r.status_code == 404


def test_latest_open_evaluation_404_when_open_has_no_evaluation(tmp_path):
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'pending')",
        (json.dumps({"side": "BUY", "entry_low": 4553, "entry_high": 4555,
                     "sl": 4540, "tps": [4570]}),),
    )
    client = TestClient(build_app(conn))
    r = client.get("/actions/latest_open_evaluation")
    assert r.status_code == 404


def test_latest_open_evaluation_returns_evaluation(tmp_path):
    conn = _setup(tmp_path)
    payload = {
        "side": "BUY",
        "entry_low": 4553, "entry_high": 4555,
        "sl": 4540, "tps": [4570, 4580],
        "evaluation": {
            "score": 72,
            "verdict": "moderate",
            "key_factor": "ok",
            "summary": "x",
            "factors": {"T1": "...", "G1": "..."},
            "data_quality": "reduced",
            "missing": ["macro_snapshot"],
            "evaluated_at": "2026-05-07T14:30:00+00:00",
        },
    }
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'pending')",
        (json.dumps(payload),),
    )
    aid = cur.lastrowid
    client = TestClient(build_app(conn))
    r = client.get("/actions/latest_open_evaluation")
    assert r.status_code == 200
    body = r.json()
    assert body["action_id"] == aid
    assert body["evaluation"]["score"] == 72
    assert body["evaluation"]["verdict"] == "moderate"
