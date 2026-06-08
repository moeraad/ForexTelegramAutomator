# tests/test_api_candles.py
import pytest
from pydantic import ValidationError
from src.api_models import MarketCandlesBody, CandleBar


def test_candle_bar_parses_minimal_fields():
    bar = CandleBar(t="2026-06-09T10:00:00+00:00", o=4500.0, h=4505.0, l=4498.0, c=4502.0, v=123)
    assert bar.o == 4500.0 and bar.v == 123


def test_market_candles_body_defaults_symbol_xauusd():
    body = MarketCandlesBody(
        timeframe="M15",
        bars=[CandleBar(t="2026-06-09T10:00:00+00:00", o=1, h=2, l=0.5, c=1.5, v=1)],
    )
    assert body.symbol == "XAUUSD"
    assert body.timeframe == "M15"
    assert len(body.bars) == 1


def test_market_candles_body_rejects_bad_timeframe():
    with pytest.raises(ValidationError):
        MarketCandlesBody(
            timeframe="M5",
            bars=[CandleBar(t="2026-06-09T10:00:00+00:00", o=1, h=2, l=0.5, c=1.5, v=1)],
        )


import json
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _app(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn, build_app(conn)


def _bars():
    return [
        {"t": "2026-06-09T10:00:00+00:00", "o": 4500, "h": 4505, "l": 4498, "c": 4502, "v": 10},
        {"t": "2026-06-09T10:15:00+00:00", "o": 4502, "h": 4508, "l": 4501, "c": 4507, "v": 12},
    ]


def test_post_candles_then_get_round_trip(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/market/candles", json={"timeframe": "M15", "bars": _bars()})
    assert r.status_code == 200, r.text
    g = client.get("/market/candles", params={"symbol": "XAUUSD", "timeframe": "M15"})
    assert g.status_code == 200
    body = g.json()
    assert body["timeframe"] == "M15"
    assert len(body["bars"]) == 2
    assert body["bars"][1]["c"] == 4507
    assert body["stale"] is False


def test_get_candles_absent_returns_empty_stale(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    g = client.get("/market/candles", params={"symbol": "XAUUSD", "timeframe": "H1"})
    assert g.status_code == 200
    body = g.json()
    assert body["bars"] == []
    assert body["stale"] is True


def test_post_candles_rejects_unsupported_symbol(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/market/candles", json={"symbol": "EURUSD", "timeframe": "M15", "bars": _bars()})
    assert r.status_code == 400
