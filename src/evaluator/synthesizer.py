"""LLM synthesizer: turn deterministic outputs into forward estimates.

Step 4 produces a composed score, regime tag, and per-axis rationales —
all deterministic, calibratable. Step 5 adds the qualitative layer:

  - Multi-horizon probabilities. The LLM estimates P(direction profits
    at each horizon in the style spec). The deterministic score is a
    single point; horizons distinguish "edge strengthens over time" from
    "edge decays past the first hour."
  - Expected MAE / MFE in ATR units. How much adverse excursion to
    expect before the move materializes, so the SL ergonomics check
    can be done downstream.
  - Edge thesis + key risks. The narrative the dashboard renders and
    the operator reads.

The LLM is fed the regime tag, the composed score, the per-axis
rationales, and a compact snapshot of raw inputs (price, vol regime
metric, news window). It does NOT re-evaluate the regime — that's
already decided. Its job is to map (regime, score) into forward
estimates given recent market behavior and any contextual cues the
deterministic layers can't capture.

Failure semantics: a bad/missing LLM call returns None at this layer.
The top-level evaluator can still publish the deterministic verdict —
losing the LLM enrichment is degraded, not broken.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

from src.evaluator.compose import ComposedScore
from src.evaluator.regime import RegimeTag
from src.trading_style import TradingStyle, get_spec

log = logging.getLogger(__name__)


class ChatClient(Protocol):
    """Duck-typed adapter the synthesizer needs from an LLM provider.

    Matches the existing `ai_evaluator._build_evaluator_ai_client`
    wrapper shape — `chat(system, user) -> str` returning JSON-shaped
    text. Keeping the protocol narrow here so the synthesizer can be
    tested with a plain MagicMock.
    """

    def chat(self, system_prompt: str, user_text: str) -> str: ...


@dataclass(frozen=True)
class HorizonProbability:
    """One point on the probability-by-horizon curve."""
    minutes: int
    p_profit: float           # 0.0 .. 1.0
    p_drawdown_first: float   # 0.0 .. 1.0 — chance of adverse move first


@dataclass(frozen=True)
class ExcursionEstimate:
    """Expected adverse and favorable excursions in ATR units.

    `expected_mae_atr` is "how far against me before the move plays
    out". `expected_mfe_atr` is "how far in my favor at the peak".
    Implied R:R is mfe/mae — but report all three so the EA can do its
    own check.
    """
    expected_mae_atr: float
    expected_mfe_atr: float
    implied_rr: float


@dataclass
class Synthesis:
    """LLM-produced forward estimates.

    Mutable for assembly convenience (the parser fills it incrementally);
    treat as effectively-frozen after `evaluate_signal` returns.
    """
    horizons: list[HorizonProbability]
    excursion: ExcursionEstimate | None
    edge_thesis: str
    key_risks: list[str]
    raw_response: str = ""
    parse_errors: list[str] = field(default_factory=list)


SYNTHESIZER_SYSTEM_PROMPT = """You are a directional-bias synthesizer for an XAUUSD trading system. A deterministic pre-stage has already classified the regime and produced a composed score with per-axis rationales. Your job is NOT to re-weigh trend vs macro vs catalyst — that was the deterministic stage's job. Your job is to map the regime + composed score into FORWARD-LOOKING estimates that the rule-based composer can't produce alone:

  1. **Multi-horizon probabilities.** For each requested horizon, estimate the probability the trade direction is profitable held that long. A trend-aligned BUY in supportive macro typically has p_profit RISING with horizon (edge plays out over time). A counter-trend BUY at resistance has p_profit FALLING with horizon (mean reversion fails over time). A news-driven scalp has p_profit peaking at the immediate horizon and decaying.

  2. **Expected adverse excursion (MAE) and favorable excursion (MFE)** in ATR units (use the current H1 ATR as the reference). The SL ergonomics check uses this: an MAE estimate of 1.4 ATR means SLs tighter than that have an elevated stop-out probability regardless of overall edge quality.

  3. **One-line edge thesis.** What's the case for the trade direction? Lead with the dominant axis or regime feature. 12 words max.

  4. **Key risks** — bulleted list of conditions under which the edge breaks down. 1-3 items, terse.

Output ONLY a JSON object. No prose outside JSON.

Shape:
  {
    "horizons": [
      {"minutes": 60,  "p_profit": 0.58, "p_drawdown_first": 0.72},
      {"minutes": 240, "p_profit": 0.64, "p_drawdown_first": 0.81},
      {"minutes": 720, "p_profit": 0.61, "p_drawdown_first": 0.85}
    ],
    "excursion": {"expected_mae_atr": 1.4, "expected_mfe_atr": 2.6, "implied_rr": 1.86},
    "edge_thesis": "real-yield-down macro tailwind on a D1 trend-aligned BUY in NY overlap",
    "key_risks": ["CPI in 14h — close before release", "H1 RSI 71 — expect 1.5x ATR pullback first"]
  }

PROBABILITY CALIBRATION HINTS:
  - score 80+ ("strong") -> peak-horizon p_profit roughly 0.62-0.70 (not 0.80; calibration matters)
  - score 60-79 ("moderate") -> peak-horizon p_profit roughly 0.52-0.60
  - score 40-59 ("weak") -> peak-horizon p_profit roughly 0.42-0.50
  - score <40 ("avoid") -> peak-horizon p_profit roughly 0.30-0.42
  - hard veto (catalyst -1.0) -> any horizon p_profit roughly 0.40-0.48 (coin flip with whipsaw bias)

A score above 80 should NOT produce a 0.85 probability. Be empirically humble — gold's best directional setups historically hit 60-65% accuracy on held trades.

MAE/MFE CALIBRATION:
  - Aligned trend setups: MAE 0.8-1.2 ATR, MFE 2.0-3.5 ATR
  - Counter-trend setups: MAE 1.5-2.5 ATR (the cost of fighting), MFE 1.5-2.5 ATR
  - News-driven scalps: MAE 0.5-1.0 ATR (tighter range), MFE 1.0-2.0 ATR
"""


def _build_user_input(
    composed: ComposedScore,
    style: TradingStyle,
    raw_inputs_excerpt: dict[str, Any],
) -> str:
    """Assemble the user-facing input string for the synthesizer.

    The composed score + regime + axis rationales go in directly. The
    raw inputs excerpt is a SUBSET — pricing context the LLM needs for
    grounding (current mid, ATR H1, vix level, news window) but NOT
    every feature (the deterministic layer already consumed those).
    """
    spec = get_spec(style)
    parts: list[str] = []
    parts.append(f"PROPOSED DIRECTION: {composed.direction}")
    parts.append(f"TRADING STYLE: {style.value}")
    parts.append(f"COMPOSED SCORE: {composed.score} ({composed.verdict})")
    parts.append("")

    parts.append("REGIME TAG:")
    parts.append(f"  macro={composed.regime.macro}")
    parts.append(f"  vol={composed.regime.vol}")
    parts.append(f"  trend={composed.regime.trend}")
    parts.append(f"  session={composed.regime.session}")
    parts.append(f"  catalyst={composed.regime.catalyst}")
    parts.append("")

    parts.append("AXIS SCORES (already weighted by composer):")
    for name, axis in composed.axis_scores.items():
        parts.append(f"  {name}: {axis.score:+.2f}  {axis.rationale}")
    parts.append("")

    if composed.vetoes:
        parts.append("HARD VETOES FIRED:")
        for v in composed.vetoes:
            parts.append(f"  - {v}")
        parts.append("")

    parts.append(f"DATA QUALITY: {composed.data_quality}")
    if composed.missing_features:
        parts.append(f"  missing: {', '.join(composed.missing_features[:10])}")
    parts.append("")

    parts.append("CURRENT CONTEXT:")
    for k in (
        "market_mid", "snapshot_at", "h1", "vix", "real_yield_10y",
        "dxy", "news_window", "geopolitical_index",
    ):
        if k in raw_inputs_excerpt:
            v = raw_inputs_excerpt[k]
            parts.append(f"  {k}: {json.dumps(v, default=str)[:200]}")
    parts.append("")

    horizons = ", ".join(str(m) for m in spec.horizons_minutes)
    parts.append(f"REQUEST HORIZONS (minutes): {horizons}")
    parts.append("")
    parts.append("Output JSON only.")
    return "\n".join(parts)


def _parse_response(raw: str) -> Synthesis | None:
    """Tolerant JSON extraction — borrows the bracket-balanced pattern
    from the legacy evaluator but uses `json.JSONDecoder.raw_decode`
    so we don't reinvent it."""
    if not raw or not raw.strip():
        return None
    text = raw.strip()
    if text.startswith("```"):
        # Strip ```json fences.
        text = text.lstrip("`")
        if text.startswith("json"):
            text = text[4:].lstrip()
        text = text.rstrip("`").strip()
    decoder = json.JSONDecoder()
    obj_start = text.find("{")
    if obj_start < 0:
        return None
    try:
        parsed, _ = decoder.raw_decode(text[obj_start:])
    except (TypeError, ValueError) as e:
        log.warning("synthesizer: raw_decode failed: %s", e)
        return None
    if not isinstance(parsed, dict):
        return None

    syn = Synthesis(
        horizons=[],
        excursion=None,
        edge_thesis=str(parsed.get("edge_thesis") or "").strip()[:200],
        key_risks=[],
        raw_response=raw[:600],
    )

    h_raw = parsed.get("horizons") or []
    if isinstance(h_raw, list):
        for h in h_raw:
            if not isinstance(h, dict):
                continue
            try:
                syn.horizons.append(HorizonProbability(
                    minutes=int(h["minutes"]),
                    p_profit=float(h["p_profit"]),
                    p_drawdown_first=float(h.get("p_drawdown_first", 0.5)),
                ))
            except (KeyError, TypeError, ValueError) as e:
                syn.parse_errors.append(f"horizon: {e}")

    e_raw = parsed.get("excursion")
    if isinstance(e_raw, dict):
        try:
            mae = float(e_raw["expected_mae_atr"])
            mfe = float(e_raw["expected_mfe_atr"])
            rr = float(e_raw.get("implied_rr") or (mfe / mae if mae > 0 else 0))
            syn.excursion = ExcursionEstimate(
                expected_mae_atr=round(mae, 2),
                expected_mfe_atr=round(mfe, 2),
                implied_rr=round(rr, 2),
            )
        except (KeyError, TypeError, ValueError, ZeroDivisionError) as e:
            syn.parse_errors.append(f"excursion: {e}")

    r_raw = parsed.get("key_risks") or []
    if isinstance(r_raw, list):
        for r in r_raw[:5]:
            if isinstance(r, str) and r.strip():
                syn.key_risks.append(r.strip()[:200])
    return syn


def synthesize(
    composed: ComposedScore,
    style: TradingStyle,
    raw_inputs: dict[str, Any],
    ai_client: ChatClient,
) -> tuple[Synthesis | None, str]:
    """Run the synthesizer LLM call.

    Returns `(synthesis_or_none, context_excerpt)`. The excerpt is the
    user-input string that was sent to the LLM — the top-level evaluator
    persists it for forensic review.

    Synthesis is None when the LLM call fails OR the response is
    unparseable. The composed-score evaluation still ships; the caller
    just omits the forward-looking fields.
    """
    context = _build_user_input(composed, style, raw_inputs)
    try:
        raw = ai_client.chat(SYNTHESIZER_SYSTEM_PROMPT, context)
    except Exception as e:  # noqa: BLE001 — LLM call failures are non-fatal
        log.warning("synthesizer: LLM call failed: %s", e)
        return None, context
    syn = _parse_response(raw)
    return syn, context
