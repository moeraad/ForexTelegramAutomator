"""Tests for src.ai_evaluator + the /market/snapshot and
/actions/latest_open_evaluation API endpoints."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import MagicMock

from fastapi.testclient import TestClient

from src.ai_evaluator import (
    _compute_channel_recent_outcomes,
    _compute_rr_ratio,
    _get_market_mid,
    _get_market_snapshot,
    _parse_evaluator_response,
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


# ---- _compute_rr_ratio --------------------------------------------------


def test_rr_ratio_buy():
    rr = _compute_rr_ratio(
        side="BUY", entry_low=4553, entry_high=4555,
        sl=4540, tps=[4570, 4580, 4600],
    )
    # entry_mid=4554, risk=14, reward=46, ratio≈3.29
    assert rr["entry_mid"] == 4554
    assert rr["risk"] == 14.0
    assert rr["reward_final"] == 46.0
    assert abs(rr["ratio_final"] - 46.0 / 14.0) < 1e-9


def test_rr_ratio_sell():
    rr = _compute_rr_ratio(
        side="SELL", entry_low=4570, entry_high=4572,
        sl=4585, tps=[4555, 4540, 4520],
    )
    assert rr["entry_mid"] == 4571
    assert rr["risk"] == 14.0
    assert rr["reward_final"] == 51.0


def test_rr_ratio_no_tps_safe():
    rr = _compute_rr_ratio(
        side="BUY", entry_low=4553, entry_high=4555, sl=4540, tps=[],
    )
    assert rr["ratio_final"] == 0.0


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


# ---- _get_market_snapshot / _get_market_mid -----------------------------


def test_get_market_snapshot_missing(tmp_path):
    conn = _setup(tmp_path)
    snap, age = _get_market_snapshot(conn, "XAUUSD")
    assert snap is None and age is None


def test_get_market_snapshot_present(tmp_path):
    conn = _setup(tmp_path)
    payload = {
        "m15": {"open": 4685, "high": 4690, "low": 4682, "close": 4687, "atr14": 4.5},
        "h1":  {"open": 4675, "high": 4692, "low": 4672, "close": 4687, "atr14": 12.0},
        "h4":  {"open": 4660, "high": 4695, "low": 4655, "close": 4687, "atr14": 28.0},
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
    assert snap["m15"]["close"] == 4687
    assert age is not None and age >= 0


def test_get_market_mid_missing(tmp_path):
    conn = _setup(tmp_path)
    mid, age = _get_market_mid(conn, "XAUUSD")
    assert mid is None


# ---- _compute_channel_recent_outcomes -----------------------------------


def test_channel_outcomes_empty(tmp_path):
    conn = _setup(tmp_path)
    res = _compute_channel_recent_outcomes(conn, 10)
    assert res == {"count": 0, "wins": 0, "losses": 0, "win_rate": None}


def test_channel_outcomes_partial_counts_as_win(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, partial_close_count, "
        "entry_price, sl, tp, opened_at, closed_at, close_reason, status) "
        "VALUES(?, 100, 'XAUUSD', 'BUY', 0.5, 1.0, 1, 4673.71, 4664, 4720, "
        "?, ?, 'mt5_close', 'closed')",
        (aid, now, now),
    )
    res = _compute_channel_recent_outcomes(conn, 10)
    assert res["wins"] == 1
    assert res["losses"] == 0
    assert res["win_rate"] == 1.0


def test_channel_outcomes_no_partial_default_loss(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, "
        "volume, original_volume, partial_close_count, "
        "entry_price, sl, tp, opened_at, closed_at, close_reason, status) "
        "VALUES(?, 200, 'XAUUSD', 'BUY', 1.0, 1.0, 0, 4673.71, 4664, 4720, "
        "?, ?, 'mt5_close', 'closed')",
        (aid, now, now),
    )
    res = _compute_channel_recent_outcomes(conn, 10)
    assert res["losses"] == 1
    assert res["wins"] == 0


# ---- build_evaluator_input ----------------------------------------------


def test_build_evaluator_input_marks_missing_when_no_snapshot(tmp_path):
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
    assert "channel_history" in missing
    assert "SIGNAL:" in text
    assert "CURRENT MARKET:" in text
    assert "MULTI-TIMEFRAME OHLC" in text
    assert "CHANNEL RECENT" in text
    assert "R:R_final" in text


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


def test_evaluate_signal_full_path(tmp_path):
    conn = _setup(tmp_path)
    signal = {
        "side": "BUY",
        "entry_low": 4553, "entry_high": 4555,
        "sl": 4540, "tps": [4570, 4580, 4600],
        "comment": "FXENGIN",
    }
    ai = MagicMock()
    ai.chat.return_value = json.dumps({
        "score": 78,
        "verdict": "moderate",
        "key_factor": "R:R 1:3.3 strong",
        "summary": "Acceptable setup",
        "factors": {"rr_ratio": "1:3.3"},
    })
    result = evaluate_signal(signal, conn, ai, symbol="XAUUSD")
    # Snapshot is missing -> reduced + cap at 70
    assert result["data_quality"] == "reduced"
    assert result["score"] == 70
    assert result["verdict"] == "moderate"
    assert "ohlc_snapshot" in result["missing"]
    assert "evaluated_at" in result


def test_evaluate_signal_handles_ai_error(tmp_path):
    conn = _setup(tmp_path)
    signal = {
        "side": "BUY", "entry_low": 4553, "entry_high": 4555,
        "sl": 4540, "tps": [4570],
    }
    ai = MagicMock()
    ai.chat.side_effect = RuntimeError("boom")
    result = evaluate_signal(signal, conn, ai, symbol="XAUUSD")
    assert result["verdict"] == "unavailable"
    assert "evaluator failed" in result["key_factor"]


# ---- API: POST /market/snapshot -----------------------------------------


def test_post_market_snapshot_writes_settings(tmp_path):
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
    assert r.json()["ok"] is True

    raw = conn.execute(
        "SELECT value FROM settings WHERE key='market_snapshot_XAUUSD'"
    ).fetchone()
    assert raw is not None
    saved = json.loads(raw["value"])
    assert saved["h1"]["close"] == 4687

    at = conn.execute(
        "SELECT value FROM settings WHERE key='market_snapshot_XAUUSD_at'"
    ).fetchone()
    assert at is not None
    assert "+00:00" in at["value"] or at["value"].endswith("Z")


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
            "factors": {},
            "data_quality": "reduced",
            "missing": ["ohlc_snapshot"],
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
