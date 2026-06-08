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
