"""Tests for the v2 evaluator's synthesizer + style override + top-level glue."""
from __future__ import annotations

import json
from unittest.mock import MagicMock

from src import feature_store
from src.db import connect, init_schema
from src.evaluator.compose import compose
from src.evaluator.evaluator import evaluate_signal_v2
from src.evaluator.features import AxisScore
from src.evaluator.regime import RegimeTag
from src.evaluator.style_override import detect, resolve
from src.evaluator.synthesizer import _parse_response, synthesize
from src.trading_style import TradingStyle


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


# ---- style_override -------------------------------------------------

def test_detect_arabic_scalp():
    probe = detect("اسكالب فقط على الذهب")
    assert probe.style == TradingStyle.SCALP
    assert "اسكالب" in probe.matched_phrase


def test_detect_arabic_swing():
    probe = detect("صفقة طويلة على المدى الطويل")
    assert probe.style == TradingStyle.SWING


def test_detect_english_scalp():
    probe = detect("quick scalp on XAUUSD")
    assert probe.style == TradingStyle.SCALP


def test_detect_english_position():
    probe = detect("position trade — long-term hold")
    assert probe.style == TradingStyle.POSITION


def test_detect_no_match_returns_none():
    probe = detect("BUY XAUUSD entry 4700 sl 4690 tps 4720 4730 4740")
    assert probe.style is None
    assert probe.matched_phrase == ""


def test_detect_empty_message():
    assert detect("").style is None


def test_resolve_uses_stack_default_when_override_disabled():
    style, source = resolve(TradingStyle.INTRADAY, "اسكالب فقط", False)
    assert style == TradingStyle.INTRADAY
    assert source == "stack_default"


def test_resolve_overrides_when_message_declares():
    style, source = resolve(TradingStyle.INTRADAY, "scalp now", True)
    assert style == TradingStyle.SCALP
    assert source.startswith("ai_override:")


def test_resolve_no_override_when_message_matches_default():
    """If the message declares the SAME style as the stack default,
    that's not an override — keep the stack_default source for cleaner
    forensics."""
    style, source = resolve(TradingStyle.SCALP, "scalp now", True)
    assert style == TradingStyle.SCALP
    assert source == "stack_default"


def test_resolve_accepts_string_default():
    """Settings stores the style as a string tag; resolve must accept."""
    style, source = resolve("intraday", "quick scalp", True)
    assert style == TradingStyle.SCALP


# ---- synthesizer ----------------------------------------------------

_VALID_RESPONSE = json.dumps({
    "horizons": [
        {"minutes": 60, "p_profit": 0.58, "p_drawdown_first": 0.72},
        {"minutes": 240, "p_profit": 0.64, "p_drawdown_first": 0.81},
    ],
    "excursion": {"expected_mae_atr": 1.4, "expected_mfe_atr": 2.6, "implied_rr": 1.86},
    "edge_thesis": "real-yield-down macro tailwind on trend-aligned BUY",
    "key_risks": ["CPI in 14h", "H1 RSI 71 — pullback likely first"],
})


def test_parse_response_extracts_full_synthesis():
    syn = _parse_response(_VALID_RESPONSE)
    assert syn is not None
    assert len(syn.horizons) == 2
    assert syn.horizons[0].minutes == 60
    assert syn.horizons[0].p_profit == 0.58
    assert syn.excursion is not None
    assert syn.excursion.expected_mae_atr == 1.4
    assert syn.excursion.implied_rr == 1.86
    assert "real-yield" in syn.edge_thesis
    assert len(syn.key_risks) == 2


def test_parse_response_handles_markdown_fence():
    fenced = "```json\n" + _VALID_RESPONSE + "\n```"
    syn = _parse_response(fenced)
    assert syn is not None


def test_parse_response_returns_none_on_garbage():
    assert _parse_response("not json at all") is None
    assert _parse_response("") is None


def test_parse_response_tolerates_partial_fields():
    """If the LLM omits excursion, the horizons + thesis still come
    through — better than discarding the whole response."""
    partial = json.dumps({
        "horizons": [{"minutes": 60, "p_profit": 0.55, "p_drawdown_first": 0.6}],
        "edge_thesis": "moderate trend setup",
        "key_risks": [],
    })
    syn = _parse_response(partial)
    assert syn is not None
    assert len(syn.horizons) == 1
    assert syn.excursion is None


def test_parse_response_records_excursion_parse_error():
    """Malformed excursion gets logged but doesn't break the parse."""
    bad = json.dumps({
        "horizons": [{"minutes": 60, "p_profit": 0.55, "p_drawdown_first": 0.6}],
        "excursion": {"expected_mae_atr": "not a number"},
        "edge_thesis": "x",
        "key_risks": [],
    })
    syn = _parse_response(bad)
    assert syn is not None
    assert syn.excursion is None
    assert any("excursion" in e for e in syn.parse_errors)


def _baseline_composed(score: int = 65) -> object:
    axes = {
        "trend": AxisScore(score=0.5, rationale="trend up"),
        "levels": AxisScore(score=0.0, rationale="no levels nearby"),
        "regime": AxisScore(score=0.2, rationale="ADX 22"),
        "macro": AxisScore(score=0.4, rationale="real yield down"),
        "catalyst": AxisScore(score=0.0, rationale="clear"),
    }
    regime = RegimeTag(
        macro="real_yield_down_dxy_down", vol="normal",
        trend="aligned_up", session="ny_overlap", catalyst="clean",
    )
    return compose("BUY", TradingStyle.INTRADAY, axes, regime)


def test_synthesize_returns_parsed_synthesis_and_context():
    ai = MagicMock()
    ai.chat.return_value = _VALID_RESPONSE
    syn, ctx = synthesize(_baseline_composed(), TradingStyle.INTRADAY, {}, ai)
    assert syn is not None
    assert len(syn.horizons) == 2
    # Context must mention the regime tag so post-mortems can re-derive.
    assert "real_yield_down_dxy_down" in ctx


def test_synthesize_swallows_llm_exception():
    """A network error during LLM call returns None + context but
    never raises into the orchestrator."""
    ai = MagicMock()
    ai.chat.side_effect = RuntimeError("network down")
    syn, ctx = synthesize(_baseline_composed(), TradingStyle.INTRADAY, {}, ai)
    assert syn is None
    assert ctx  # context still produced


def test_synthesize_handles_unparseable_response():
    ai = MagicMock()
    ai.chat.return_value = "I cannot output JSON today"
    syn, _ = synthesize(_baseline_composed(), TradingStyle.INTRADAY, {}, ai)
    assert syn is None


# ---- evaluate_signal_v2 end-to-end ---------------------------------

def test_evaluate_signal_v2_runs_deterministic_only_when_ai_none(tmp_path):
    conn = _setup(tmp_path)
    out = evaluate_signal_v2({"side": "BUY"}, conn, ai_client=None)
    assert out["evaluator_version"] == "v2"
    assert "score" in out
    assert "verdict" in out
    assert out["effective_style"] == "intraday"  # default
    assert out["synthesizer_status"] == "unavailable"
    # No probabilities or excursion when synthesizer didn't run.
    assert "probabilities" not in out


def test_evaluate_signal_v2_includes_synthesis_when_ai_available(tmp_path):
    conn = _setup(tmp_path)
    ai = MagicMock()
    ai.chat.return_value = _VALID_RESPONSE
    out = evaluate_signal_v2({"side": "BUY"}, conn, ai_client=ai)
    assert out["synthesizer_status"] == "ok"
    assert "probabilities" in out
    assert len(out["probabilities"]) == 2
    assert "expected_excursion" in out
    assert "edge_thesis" in out
    assert "key_risks" in out
    assert "context_excerpt" in out


def test_evaluate_signal_v2_invalid_side_returns_stub(tmp_path):
    conn = _setup(tmp_path)
    out = evaluate_signal_v2({"side": "FLAT"}, conn, ai_client=None)
    assert out["score"] == 0
    assert out["verdict"] == "unavailable"


def test_evaluate_signal_v2_falls_back_when_synthesizer_fails(tmp_path):
    """Deterministic score still ships even if the LLM call fails."""
    conn = _setup(tmp_path)
    ai = MagicMock()
    ai.chat.side_effect = RuntimeError("rate limited")
    out = evaluate_signal_v2({"side": "BUY"}, conn, ai_client=ai)
    assert out["synthesizer_status"] == "failed"
    assert "score" in out
    assert "probabilities" not in out
    # Context excerpt still saved — useful for "what state was the
    # evaluator in when the LLM call failed?"
    assert "context_excerpt" in out


def test_evaluate_signal_v2_reads_features_from_store(tmp_path):
    """The evaluator must include feature_store values in the regime
    classification. Seeding a tailwind macro should pull the regime
    into 'real_yield_down_dxy_down'."""
    conn = _setup(tmp_path)
    feature_store.put(conn, "real_yield_10y", {"value": 1.85, "chg_pct": -0.40})
    feature_store.put(conn, "dxy", {"value": 103.0, "chg_pct": -0.30})
    out = evaluate_signal_v2({"side": "BUY"}, conn, ai_client=None)
    assert out["regime"]["macro"] == "real_yield_down_dxy_down"


def test_evaluate_signal_v2_horizons_from_style(tmp_path):
    """The list of probability horizons must match the resolved style's
    spec — scalp gets (15, 60, 240); swing gets (240, 1440, 4320)."""
    conn = _setup(tmp_path)
    out = evaluate_signal_v2(
        {"side": "BUY", "comment": "quick scalp on XAUUSD"},
        conn, ai_client=None,
    )
    # AI override picks scalp from the comment.
    assert out["effective_style"] == "scalp"
    assert out["style_source"].startswith("ai_override:")
    assert out["horizons_minutes"] == [15, 60, 240]


def test_evaluate_signal_v2_carries_vetoes(tmp_path):
    """If the catalyst sub-scorer hard-vetoes (news within 30 min),
    the composed score is capped AND the veto list is in the dict."""
    conn = _setup(tmp_path)
    # Stash a news_window into the feature store so the catalyst scorer
    # picks it up via gather_inputs.
    feature_store.put(conn, "news_window", {
        "next_event": {"event": "NFP", "impact": "high", "minutes": 14},
        "last_event": None,
    })
    out = evaluate_signal_v2({"side": "BUY"}, conn, ai_client=None)
    assert "news_high_impact_imminent" in out["vetoes"]
    assert out["score"] <= 30
