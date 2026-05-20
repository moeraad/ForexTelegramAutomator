"""Tests for the deterministic evaluator layer (Step 4).

Three groups:
  - scorers: each axis sub-scorer with synthetic inputs
  - regime: classifier with synthetic inputs
  - compose: weighted composition + vetoes + data quality
"""
from __future__ import annotations

from datetime import datetime, timezone

from src.evaluator.compose import compose, to_dict
from src.evaluator.features import (
    AxisScore,
    clamp,
    unwrap_macro_feature,
)
from src.evaluator.regime import RegimeTag, classify
from src.evaluator.scorers import (
    score_all,
    score_catalyst,
    score_levels,
    score_macro,
    score_regime,
    score_trend,
)
from src.trading_style import TradingStyle


# ---- unwrap_macro_feature -------------------------------------------

def test_unwrap_macro_feature_dict_shape():
    v, c = unwrap_macro_feature({"value": 104.32, "chg_pct": -0.21})
    assert v == 104.32
    assert c == -0.21


def test_unwrap_macro_feature_bare_float():
    v, c = unwrap_macro_feature(0.65)
    assert v == 0.65
    assert c is None


def test_unwrap_macro_feature_none():
    v, c = unwrap_macro_feature(None)
    assert v is None and c is None


def test_clamp_within_bounds():
    assert clamp(0.5) == 0.5
    assert clamp(2.0) == 1.0
    assert clamp(-2.0) == -1.0


# ---- score_trend ----------------------------------------------------

def test_score_trend_buy_in_uptrend_positive():
    inputs = {
        "d1": {"close": 4880.0, "sma50": 4800.0},  # +1.67% above SMA50
        "h4": {"open": 4870.0, "close": 4880.0},   # bull
        "h1_recent_closes": [4860.0, 4865.0, 4870.0, 4875.0, 4880.0],
    }
    s = score_trend("BUY", inputs)
    assert s.score > 0.5
    assert "BUY" in s.rationale


def test_score_trend_sell_in_uptrend_negative():
    """Direction-relative: SELL in an uptrend must give a negative
    score even though the market is the same."""
    inputs = {
        "d1": {"close": 4880.0, "sma50": 4800.0},
        "h4": {"open": 4870.0, "close": 4880.0},
        "h1_recent_closes": [4860.0, 4880.0],
    }
    s = score_trend("SELL", inputs)
    assert s.score < -0.4


def test_score_trend_missing_d1_lists_in_missing():
    s = score_trend("BUY", {})
    assert "d1" in s.inputs_missing


def test_score_trend_invalid_side():
    s = score_trend("FLAT", {})
    assert s.score == 0.0
    assert "invalid" in s.rationale.lower()


# ---- score_levels ---------------------------------------------------

def test_score_levels_buy_near_pdl_favorable():
    """BUY near PDL = support entry = favorable."""
    inputs = {
        "market_mid": 4700.0,
        "d1_prev": {"high": 4750.0, "low": 4705.0},
    }
    s = score_levels("BUY", inputs)
    assert s.score > 0.3


def test_score_levels_buy_near_pdh_unfavorable():
    """BUY near PDH = chasing into resistance = unfavorable."""
    inputs = {
        "market_mid": 4750.0,
        "d1_prev": {"high": 4753.0, "low": 4700.0},
    }
    s = score_levels("BUY", inputs)
    assert s.score < -0.3


def test_score_levels_sell_near_pdh_favorable():
    """Direction flip: SELL near PDH = fading resistance = favorable."""
    inputs = {
        "market_mid": 4750.0,
        "d1_prev": {"high": 4753.0, "low": 4700.0},
    }
    s = score_levels("SELL", inputs)
    assert s.score > 0.3


def test_score_levels_range_exhaustion_penalty():
    """BUY when today already used 2× ADR in the same direction =
    mean-reversion bias against continuation."""
    inputs = {
        "market_mid": 4730.0,
        "d1": {"open": 4670.0, "high": 4735.0, "low": 4665.0, "close": 4730.0},
        "adr20": 30.0,  # range used = 70, 2.3× ADR
        "d1_prev": {"high": 4710.0, "low": 4660.0},
    }
    s = score_levels("BUY", inputs)
    # Negative score because of range-exhaustion penalty.
    assert s.score < 0


# ---- score_regime ---------------------------------------------------

def test_score_regime_low_adx_negative():
    """ADX < 20 = chop = directional bets weak regardless of side."""
    inputs = {"adx_h1": 14.0}
    s = score_regime("BUY", inputs)
    assert s.score < 0


def test_score_regime_high_adx_aligned_positive():
    inputs = {
        "adx_h1": 30.0,
        "h1_recent_closes": [4860.0, 4880.0],  # uptrend
    }
    s = score_regime("BUY", inputs)
    assert s.score > 0.2


def test_score_regime_panic_vix_helps_buy():
    """High VIX = safe-haven flow = favorable for BUY, unfavorable for SELL."""
    inputs = {"vix": {"value": 35.0, "chg_pct": 8.0}}
    s_buy = score_regime("BUY", inputs)
    s_sell = score_regime("SELL", inputs)
    assert s_buy.score > 0
    assert s_sell.score < 0


# ---- score_macro ----------------------------------------------------

def test_score_macro_real_yield_down_favors_buy():
    inputs = {
        "real_yield_10y": {"value": 1.85, "chg_pct": -0.40},
        "dxy": {"value": 103.0, "chg_pct": -0.20},
    }
    s = score_macro("BUY", inputs)
    assert s.score > 0.4


def test_score_macro_real_yield_up_punishes_buy():
    inputs = {
        "real_yield_10y": {"value": 2.30, "chg_pct": +0.45},
        "dxy": {"value": 105.0, "chg_pct": +0.50},
    }
    s = score_macro("BUY", inputs)
    assert s.score < -0.4


def test_score_macro_cot_extreme_long_contrarian():
    """COT longs at 90th percentile = stretched = contrarian penalty for BUY."""
    inputs = {
        "real_yield_10y": {"value": 2.0, "chg_pct": 0.0},
        "cot_extremes_percentile": 92.0,
    }
    s = score_macro("BUY", inputs)
    assert s.score < 0


def test_score_macro_cot_extreme_short_contrarian_helps_buy():
    inputs = {"cot_extremes_percentile": 8.0}
    s = score_macro("BUY", inputs)
    assert s.score > 0


def test_score_macro_geopolitical_premium_helps_buy():
    inputs = {"geopolitical_index": 0.78}
    s = score_macro("BUY", inputs)
    assert s.score > 0


# ---- score_catalyst -------------------------------------------------

def test_score_catalyst_high_impact_imminent_hard_veto():
    inputs = {
        "news_window": {
            "next_event": {"event": "NFP", "impact": "high", "minutes": 12},
            "last_event": None,
        }
    }
    s = score_catalyst("BUY", inputs)
    assert s.score <= -0.9


def test_score_catalyst_post_news_whipsaw_soft_penalty():
    inputs = {
        "news_window": {
            "next_event": {"event": "Trade balance", "impact": "low", "minutes": 60},
            "last_event": {"event": "CPI", "impact": "high", "minutes": 8},
        }
    }
    s = score_catalyst("BUY", inputs)
    assert -0.7 < s.score < -0.3


def test_score_catalyst_clean_window_zero():
    inputs = {
        "news_window": {
            "next_event": {"event": "Speaker", "impact": "low", "minutes": 200},
            "last_event": None,
        }
    }
    s = score_catalyst("BUY", inputs)
    assert s.score == 0.0


def test_score_catalyst_no_news_window_zero_with_missing():
    s = score_catalyst("BUY", {})
    assert s.score == 0.0
    assert "news_window" in s.inputs_missing


# ---- score_all aggregation ------------------------------------------

def test_score_all_returns_five_axes():
    out = score_all("BUY", {"d1": {"close": 4880.0, "sma50": 4800.0}})
    assert set(out.keys()) == {"trend", "levels", "regime", "macro", "catalyst"}


# ---- Regime classifier ----------------------------------------------

def test_regime_tag_summary_joins_components():
    tag = RegimeTag(
        macro="real_yield_down_dxy_down",
        vol="normal",
        trend="aligned_up",
        session="ny_overlap",
        catalyst="clean",
    )
    assert "|" in tag.summary
    assert "ny_overlap" in tag.summary


def test_classify_macro_tailwind_when_both_down():
    inputs = {
        "real_yield_10y": {"chg_pct": -0.30},
        "dxy": {"chg_pct": -0.40},
    }
    tag = classify(inputs, now=datetime(2026, 5, 20, 13, tzinfo=timezone.utc))
    assert tag.macro == "real_yield_down_dxy_down"


def test_classify_macro_headwind_when_both_up():
    inputs = {
        "real_yield_10y": {"chg_pct": +0.30},
        "dxy": {"chg_pct": +0.50},
    }
    tag = classify(inputs, now=datetime(2026, 5, 20, 13, tzinfo=timezone.utc))
    assert tag.macro == "real_yield_up_dxy_up"


def test_classify_macro_mixed_when_conflict():
    inputs = {
        "real_yield_10y": {"chg_pct": -0.30},
        "dxy": {"chg_pct": +0.30},
    }
    tag = classify(inputs, now=datetime(2026, 5, 20, 13, tzinfo=timezone.utc))
    assert tag.macro == "mixed"


def test_classify_macro_unknown_when_both_missing():
    tag = classify({}, now=datetime(2026, 5, 20, 13, tzinfo=timezone.utc))
    assert tag.macro == "unknown"


def test_classify_vol_bands():
    base = datetime(2026, 5, 20, 13, tzinfo=timezone.utc)
    assert classify({"vix": {"value": 10.0}}, now=base).vol == "compressed"
    assert classify({"vix": {"value": 16.0}}, now=base).vol == "normal"
    assert classify({"vix": {"value": 25.0}}, now=base).vol == "elevated"
    assert classify({"vix": {"value": 35.0}}, now=base).vol == "panic"


def test_classify_session_by_hour():
    asian = datetime(2026, 5, 20, 3, tzinfo=timezone.utc)
    overlap = datetime(2026, 5, 20, 14, tzinfo=timezone.utc)
    weekend = datetime(2026, 5, 23, 14, tzinfo=timezone.utc)  # Sat
    assert classify({}, now=asian).session == "asian"
    assert classify({}, now=overlap).session == "ny_overlap"
    assert classify({}, now=weekend).session == "weekend"


def test_classify_catalyst_pre_news():
    inputs = {
        "news_window": {
            "next_event": {"impact": "high", "minutes": 15},
            "last_event": None,
        }
    }
    tag = classify(inputs, now=datetime(2026, 5, 20, 13, tzinfo=timezone.utc))
    assert tag.catalyst == "pre_news"


# ---- compose --------------------------------------------------------

def _full_axis(score: float, rationale: str = "") -> AxisScore:
    return AxisScore(score=score, rationale=rationale or f"score {score}")


def _baseline_regime() -> RegimeTag:
    return RegimeTag(
        macro="neutral", vol="normal", trend="flat",
        session="ny_overlap", catalyst="clean",
    )


def test_compose_score_in_band():
    axes = {
        "trend": _full_axis(0.5),
        "levels": _full_axis(0.3),
        "regime": _full_axis(0.2),
        "macro": _full_axis(0.6),
        "catalyst": _full_axis(0.0),
    }
    c = compose("BUY", TradingStyle.INTRADAY, axes, _baseline_regime())
    assert 50 <= c.score <= 95
    assert c.direction == "BUY"
    assert c.style == "intraday"
    assert c.data_quality == "full"
    assert c.verdict in ("strong", "moderate", "weak", "avoid")


def test_compose_hard_veto_caps_score():
    """A catalyst veto must cap the final score regardless of how
    positive other axes look — being inside an NFP window is always
    'avoid'."""
    axes = {
        "trend": _full_axis(1.0),
        "levels": _full_axis(1.0),
        "regime": _full_axis(1.0),
        "macro": _full_axis(1.0),
        "catalyst": _full_axis(-1.0),
    }
    c = compose("BUY", TradingStyle.INTRADAY, axes, _baseline_regime())
    assert c.score <= 30
    assert "news_high_impact_imminent" in c.vetoes


def test_compose_data_quality_cap():
    """Missing inputs cap the score at 70 ('moderate') — same as legacy."""
    axes = {
        "trend": AxisScore(score=1.0, rationale="x", inputs_missing=["d1"]),
        "levels": _full_axis(1.0),
        "regime": _full_axis(1.0),
        "macro": _full_axis(1.0),
        "catalyst": _full_axis(0.0),
    }
    c = compose("BUY", TradingStyle.INTRADAY, axes, _baseline_regime())
    assert c.data_quality == "reduced"
    assert c.score <= 70


def test_compose_swing_weights_macro_more_than_scalp():
    """Same axis scores, different style: swing must produce a HIGHER
    composed score when macro is the strongest contributor (because
    swing weights macro at 30% vs scalp at 5%)."""
    axes = {
        "trend": _full_axis(0.0),
        "levels": _full_axis(0.0),
        "regime": _full_axis(0.0),
        "macro": _full_axis(+1.0),
        "catalyst": _full_axis(0.0),
    }
    scalp = compose("BUY", TradingStyle.SCALP, axes, _baseline_regime())
    swing = compose("BUY", TradingStyle.SWING, axes, _baseline_regime())
    assert swing.score > scalp.score


def test_compose_sell_with_negative_trend_score_high():
    """SELL with all-negative axes (which mean "totally favorable for
    SELL" in direction-relative scoring) — but wait, the scorers RETURN
    direction-relative values. So a totally-favorable SELL has POSITIVE
    axis scores. This test confirms the direction-flip happened upstream
    in the scorers and the composer treats the sign as already aligned."""
    axes = {
        "trend": _full_axis(+1.0),  # +1 = totally favorable for SELL too
        "levels": _full_axis(+1.0),
        "regime": _full_axis(+1.0),
        "macro": _full_axis(+1.0),
        "catalyst": _full_axis(0.0),
    }
    c = compose("SELL", TradingStyle.INTRADAY, axes, _baseline_regime())
    assert c.score > 80
    assert c.direction == "SELL"


def test_to_dict_preserves_legacy_keys():
    axes = {
        "trend": _full_axis(0.5, "trend rationale"),
        "levels": _full_axis(0.3, "levels rationale"),
        "regime": _full_axis(0.2, "regime rationale"),
        "macro": _full_axis(0.6, "macro rationale"),
        "catalyst": _full_axis(0.0, "catalyst rationale"),
    }
    c = compose("BUY", TradingStyle.INTRADAY, axes, _baseline_regime())
    d = to_dict(c)
    # Legacy schema keys that the dashboard reads.
    for key in ("score", "verdict", "key_factor", "summary", "factors",
                "missing", "data_quality"):
        assert key in d
    # New keys ride along.
    for key in ("direction", "style", "regime", "vetoes"):
        assert key in d
    # Factors include all five axes.
    assert set(d["factors"].keys()) == {"TREND", "LEVELS", "REGIME", "MACRO", "CATALYST"}


def test_to_dict_key_factor_is_veto_when_vetoed():
    axes = {
        "trend": _full_axis(+1.0),
        "levels": _full_axis(+1.0),
        "regime": _full_axis(+1.0),
        "macro": _full_axis(+1.0),
        "catalyst": _full_axis(-1.0),
    }
    c = compose("BUY", TradingStyle.INTRADAY, axes, _baseline_regime())
    d = to_dict(c)
    assert d["key_factor"].startswith("veto:")
