"""Compose per-axis scores into a single verdict with style weights.

The composer is the deterministic backbone of the new evaluator. Step 5
adds an LLM synthesizer on top that reads the composed score + regime
tag + axis rationales and produces multi-horizon probabilities. But the
composed score below is already actionable on its own — score-tied
sizing in the EA reads it directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.evaluator.features import AxisScore
from src.evaluator.regime import RegimeTag
from src.trading_style import TradingStyle, get_spec


@dataclass(frozen=True)
class ComposedScore:
    """Final deterministic output of the evaluator.

    `score` is the integer 0-100 the dashboard renders. `verdict` maps
    score bands to a label. `direction` echoes the input side so the
    composed result is self-describing.

    `axis_scores` carries each AxisScore for forensic rendering and for
    the LLM synthesizer's input. `regime` is the full tag the synthesizer
    conditions on.

    `vetoes` is a list of "hard caps" that fired. The catalyst sub-scorer
    can return -1.0 to mean "high-impact news imminent" — the composer
    treats that as a hard cap rather than just a negative weighted
    contribution, because being inside an NFP window should ALWAYS cap
    the score regardless of other axes.
    """
    score: int
    verdict: str
    direction: str
    style: str
    axis_scores: dict[str, AxisScore]
    regime: RegimeTag
    vetoes: list[str] = field(default_factory=list)
    data_quality: str = "full"   # "full" | "reduced"
    missing_features: list[str] = field(default_factory=list)


# Verdict bands. Same thresholds as the legacy evaluator so dashboard
# rendering stays consistent during the cutover.
def _verdict(score: int) -> str:
    if score >= 80:
        return "strong"
    if score >= 60:
        return "moderate"
    if score >= 40:
        return "weak"
    return "avoid"


# Reduced-data-quality score cap. Same as legacy.
_REDUCED_CAP = 70


def compose(
    side: str,
    style: TradingStyle | str,
    axis_scores: dict[str, AxisScore],
    regime: RegimeTag,
) -> ComposedScore:
    """Apply the trading-style's weights to the five axis scores,
    clip on hard vetoes, and produce the final verdict.

    The math:
      weighted = Σ(axis_score × style.weight) for axis ∈ {trend, levels,
                  regime, macro, catalyst}
      score    = round((weighted + 1.0) × 50)   # [-1,+1] -> [0,100]
      cap on hard vetoes (catalyst <= -0.9 -> score min(score, 30))
      cap on reduced data quality (score = min(score, 70))
    """
    spec = get_spec(style)
    w = spec.weights
    weighted = (
        axis_scores["trend"].score * w.trend
        + axis_scores["levels"].score * w.levels
        + axis_scores["regime"].score * w.regime
        + axis_scores["macro"].score * w.macro
        + axis_scores["catalyst"].score * w.catalyst
    )
    # Map [-1, +1] -> [0, 100]. Clamped before the int() to avoid <0/>100
    # from accumulated rounding error.
    raw_score = max(0, min(100, int(round((weighted + 1.0) * 50))))

    vetoes: list[str] = []
    # Hard veto: catalyst's full -1.0 means high-impact news < 30min away.
    # Cap score at 30 ("avoid" band) regardless of how positive other
    # axes look. Capping rather than zeroing preserves the directional
    # ranking — operator can still see that a deeply unfavorable veto
    # score is worse than a mild one.
    if axis_scores["catalyst"].score <= -0.9:
        vetoes.append("news_high_impact_imminent")
        raw_score = min(raw_score, 30)

    # Data-quality cap. Aggregate missing inputs across all axes; if any
    # axis ran reduced, the composed verdict also reduces.
    missing: list[str] = []
    for axis_name, axis in axis_scores.items():
        for m in axis.inputs_missing:
            tag = f"{axis_name}:{m}"
            if tag not in missing:
                missing.append(tag)
    data_quality = "reduced" if missing else "full"
    if data_quality == "reduced" and raw_score > _REDUCED_CAP:
        raw_score = _REDUCED_CAP

    return ComposedScore(
        score=raw_score,
        verdict=_verdict(raw_score),
        direction=side,
        style=spec.style.value,
        axis_scores=axis_scores,
        regime=regime,
        vetoes=vetoes,
        data_quality=data_quality,
        missing_features=missing,
    )


def to_dict(c: ComposedScore) -> dict[str, Any]:
    """Serialize a ComposedScore to a plain dict for JSON storage in
    `actions.payload_json["evaluation"]`. The legacy schema (used by the
    EA dashboard) expects `{score, verdict, key_factor, summary,
    factors, missing, data_quality, evaluated_at}` — this shape is
    preserved as a subset so the dashboard keeps working unchanged
    while the new fields ride along.
    """
    # Build the legacy 5-axis "factors" dict from the new axis scores.
    # The legacy keys (T1..C2) don't map 1:1 to our families, so we
    # provide a flatter representation: one line per family.
    factors: dict[str, str] = {}
    for name, axis in c.axis_scores.items():
        factors[name.upper()] = (
            f"{axis.score:+.2f}  {axis.rationale}"
        )

    key_factor: str
    if c.vetoes:
        key_factor = f"veto: {c.vetoes[0]}"
    else:
        # Pick the axis with the largest weighted contribution.
        spec = get_spec(c.style)
        contributions = {
            "trend": c.axis_scores["trend"].score * spec.weights.trend,
            "levels": c.axis_scores["levels"].score * spec.weights.levels,
            "regime": c.axis_scores["regime"].score * spec.weights.regime,
            "macro": c.axis_scores["macro"].score * spec.weights.macro,
            "catalyst": c.axis_scores["catalyst"].score * spec.weights.catalyst,
        }
        dominant = max(contributions, key=lambda k: abs(contributions[k]))
        key_factor = c.axis_scores[dominant].rationale

    summary_lines = [
        f"{c.direction} {c.style} | regime: {c.regime.summary}",
        f"score {c.score} ({c.verdict}); axes "
        + ", ".join(f"{k}={v.score:+.2f}" for k, v in c.axis_scores.items()),
    ]

    return {
        "score": c.score,
        "verdict": c.verdict,
        "direction": c.direction,
        "style": c.style,
        "key_factor": key_factor,
        "summary": " | ".join(summary_lines),
        "factors": factors,
        "regime": {
            "macro": c.regime.macro, "vol": c.regime.vol,
            "trend": c.regime.trend, "session": c.regime.session,
            "catalyst": c.regime.catalyst,
            "summary": c.regime.summary,
        },
        "vetoes": list(c.vetoes),
        "missing": list(c.missing_features),
        "data_quality": c.data_quality,
    }
