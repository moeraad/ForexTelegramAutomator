"""Tests for the trading-style taxonomy."""
from __future__ import annotations

import pytest

from src.trading_style import (
    DEFAULT_STYLE,
    TradingStyle,
    all_styles,
    get_spec,
    union_required_features,
)


def test_all_styles_resolve_to_a_spec():
    """Every member of the closed set must have a registered spec —
    otherwise `get_spec` would KeyError at runtime on a legal enum value."""
    for s in all_styles():
        spec = get_spec(s)
        assert spec.style == s
        assert spec.description, f"{s.value}: description is empty"


def test_each_spec_weights_sum_to_one():
    """The dataclass post-init validates this, so a failure here means
    the constants drifted."""
    for s in all_styles():
        spec = get_spec(s)
        assert abs(spec.weights.total - 1.0) < 0.01


def test_each_spec_has_at_least_two_horizons():
    """A single-horizon evaluator can't say anything about how the edge
    decays — UI needs at least a short and a long bucket per style."""
    for s in all_styles():
        spec = get_spec(s)
        assert len(spec.horizons_minutes) >= 2


def test_horizons_are_ascending():
    for s in all_styles():
        spec = get_spec(s)
        assert list(spec.horizons_minutes) == sorted(spec.horizons_minutes)


def test_scalp_horizons_shorter_than_swing():
    """Sanity that the per-style values weren't swapped — a scalp's
    longest horizon should be shorter than a swing's shortest."""
    scalp = get_spec(TradingStyle.SCALP)
    swing = get_spec(TradingStyle.SWING)
    assert max(scalp.horizons_minutes) <= min(swing.horizons_minutes)


def test_swing_weights_macro_higher_than_scalp_weights_macro():
    """Macro should dominate at long horizons and be near-zero at scalp.
    Regression: prior version had scalp.macro=0.3 by mistake."""
    scalp = get_spec(TradingStyle.SCALP)
    swing = get_spec(TradingStyle.SWING)
    assert swing.weights.macro > scalp.weights.macro * 2


def test_get_spec_accepts_string_tag():
    """Settings stores enum values as their string form; resolution
    must round-trip cleanly."""
    spec = get_spec("intraday")
    assert spec.style == TradingStyle.INTRADAY


def test_get_spec_rejects_unknown_string():
    """A typo in settings must fail loudly rather than silently default —
    silent defaults hide misconfigurations in prod."""
    with pytest.raises(ValueError):
        get_spec("daytrader")


def test_default_style_is_in_the_closed_set():
    assert DEFAULT_STYLE in all_styles()


def test_union_required_features_combines_correctly():
    """The feed worker uses this to skip pulls when no active stack
    needs them. Must be a true union."""
    intraday = get_spec(TradingStyle.INTRADAY).required_features
    swing = get_spec(TradingStyle.SWING).required_features
    combined = union_required_features([TradingStyle.INTRADAY, TradingStyle.SWING])
    assert combined == intraday | swing


def test_union_required_features_only_intraday_excludes_cot():
    """Confirms the optimization purpose: when no stack runs swing or
    position, the COT pull can be skipped. If COT leaks into intraday
    requirements that's a config bug — assert it stays excluded."""
    combined = union_required_features([TradingStyle.INTRADAY])
    assert "cot_managed_money" not in combined


def test_scalp_max_age_shorter_than_swing_max_age():
    """A 3-hour-old scalp signal is dead; a 3-hour-old swing signal is
    still actionable. Sanity-check the constants."""
    scalp = get_spec(TradingStyle.SCALP)
    swing = get_spec(TradingStyle.SWING)
    assert scalp.max_signal_age_minutes < swing.max_signal_age_minutes
