"""Tests for the score-tied sizing helper."""
from __future__ import annotations

from src.evaluator.sizing import (
    SizingDecision,
    _DEFAULT_BANDS,
    _pick_decisional_horizon,
    compute_sizing,
    to_dict,
)


def _eval_with_probs(probs: list[dict], vetoes: list[str] | None = None) -> dict:
    return {
        "probabilities": probs,
        "vetoes": vetoes or [],
    }


# ---- _pick_decisional_horizon --------------------------------------

def test_pick_median_of_three():
    probs = [
        {"minutes": 60, "p_profit": 0.50, "p_drawdown_first": 0.6},
        {"minutes": 240, "p_profit": 0.58, "p_drawdown_first": 0.7},
        {"minutes": 720, "p_profit": 0.62, "p_drawdown_first": 0.85},
    ]
    picked = _pick_decisional_horizon(probs)
    assert picked is not None
    assert picked["minutes"] == 240


def test_pick_lower_middle_of_even_length():
    """Even-length list picks the lower-middle (conservative)."""
    probs = [
        {"minutes": 60, "p_profit": 0.55, "p_drawdown_first": 0.6},
        {"minutes": 240, "p_profit": 0.60, "p_drawdown_first": 0.7},
        {"minutes": 720, "p_profit": 0.65, "p_drawdown_first": 0.8},
        {"minutes": 1440, "p_profit": 0.68, "p_drawdown_first": 0.85},
    ]
    picked = _pick_decisional_horizon(probs)
    assert picked is not None
    assert picked["minutes"] == 240


def test_pick_handles_unsorted_input():
    """LLM responses sometimes scramble horizon order."""
    probs = [
        {"minutes": 720, "p_profit": 0.62, "p_drawdown_first": 0.85},
        {"minutes": 60, "p_profit": 0.50, "p_drawdown_first": 0.6},
        {"minutes": 240, "p_profit": 0.58, "p_drawdown_first": 0.7},
    ]
    picked = _pick_decisional_horizon(probs)
    assert picked is not None
    assert picked["minutes"] == 240


def test_pick_returns_none_on_empty():
    assert _pick_decisional_horizon([]) is None


def test_pick_skips_malformed_entries():
    probs = [
        "not a dict",  # malformed
        {"minutes": 60, "p_profit": 0.55, "p_drawdown_first": 0.6},
    ]
    picked = _pick_decisional_horizon(probs)
    assert picked is not None
    assert picked["minutes"] == 60


# ---- compute_sizing — bands ----------------------------------------

def test_below_skip_threshold_skips():
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_profit": 0.40, "p_drawdown_first": 0.6},
    ]))
    assert d.skip is True
    assert d.multiplier == 0.0
    assert "skip_threshold" in d.reason


def test_band_45_to_50_low_multiplier():
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_profit": 0.48, "p_drawdown_first": 0.6},
    ]))
    assert d.skip is False
    assert d.multiplier == 0.4


def test_band_55_to_60_baseline():
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_profit": 0.58, "p_drawdown_first": 0.7},
    ]))
    assert d.skip is False
    assert d.multiplier == 1.0


def test_band_60_to_65_size_up():
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_profit": 0.62, "p_drawdown_first": 0.7},
    ]))
    assert d.skip is False
    assert d.multiplier == 1.4


def test_high_band_capped():
    """Even at p_profit=0.80, multiplier caps at the top band (1.8x).
    The cap exists so a single overconfident reading doesn't quadruple
    risk."""
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_profit": 0.80, "p_drawdown_first": 0.95},
    ]))
    assert d.skip is False
    assert d.multiplier == 1.8


def test_exact_band_boundary_inclusive_lower():
    """Boundary cases: at exactly 0.55 the band [0.55, 0.60) catches."""
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_profit": 0.55, "p_drawdown_first": 0.6},
    ]))
    assert d.multiplier == 1.0


# ---- compute_sizing — vetoes ---------------------------------------

def test_veto_forces_skip_regardless_of_probabilities():
    """A veto must override even strong probabilities — being inside
    an NFP window should NEVER trade."""
    d = compute_sizing({
        "probabilities": [{"minutes": 240, "p_profit": 0.75, "p_drawdown_first": 0.95}],
        "vetoes": ["news_high_impact_imminent"],
    })
    assert d.skip is True
    assert d.multiplier == 0.0
    assert "veto" in d.reason


def test_no_probabilities_falls_back_to_baseline_no_skip():
    """When synthesizer didn't run, sizing must not skip the trade —
    operator still gets baseline behavior."""
    d = compute_sizing({"probabilities": [], "vetoes": []})
    assert d.skip is False
    assert d.multiplier == 1.0
    assert "baseline" in d.reason


def test_malformed_probability_entry_baseline():
    """Defensive: a probability without p_profit field falls back to
    baseline rather than skipping."""
    d = compute_sizing(_eval_with_probs([
        {"minutes": 240, "p_drawdown_first": 0.7},  # missing p_profit
    ]))
    assert d.skip is False
    assert d.multiplier == 1.0


# ---- threshold parameter -------------------------------------------

def test_custom_skip_threshold_respected():
    """Operator can raise the skip threshold per stack."""
    d = compute_sizing(
        _eval_with_probs([{"minutes": 240, "p_profit": 0.48,
                           "p_drawdown_first": 0.6}]),
        skip_threshold=0.55,
    )
    assert d.skip is True


# ---- to_dict roundtrip ---------------------------------------------

def test_to_dict_includes_all_fields():
    d = SizingDecision(
        multiplier=1.4, skip=False, p_profit_used=0.62,
        horizon_minutes=240, reason="x",
    )
    out = to_dict(d)
    assert out == {
        "multiplier": 1.4, "skip": False, "p_profit_used": 0.62,
        "horizon_minutes": 240, "reason": "x",
    }


def test_to_dict_handles_none_p_profit():
    d = SizingDecision(
        multiplier=1.0, skip=False, p_profit_used=None,
        horizon_minutes=None, reason="no data",
    )
    out = to_dict(d)
    assert out["p_profit_used"] is None
