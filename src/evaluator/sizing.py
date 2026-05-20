"""Score-tied lot-size sizing from the evaluator's probability output.

The v2 evaluator emits `probabilities[h] = p_profit` for every horizon
in the trading style's spec. We collapse that into a single `multiplier`
that the EA applies to its baseline lot size. Decoupling the calibration
table from the EA means:

  - The table lives in pure Python where it's testable.
  - Changes to the table don't require an MQL5 recompile.
  - The same logic applies to OPEN, OPEN_INSTANT, REOPEN_LAST, and
    REINFORCE without duplicating MT5 code.

Result lands in `evaluation["sizing"]`. EA reads `multiplier` and `skip`;
operator sees `reason` for forensic review.

The table below is **uncalibrated** — derived from the prompt's
"calibration hints" not from outcome data. Once ~100 trades with v2
evaluations are in the DB, a separate calibration script will refit it.
The table API is stable so refitting is a number-only diff.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class SizingDecision:
    """Output of the sizing function."""
    multiplier: float        # 0.0 .. ~2.0; EA multiplies baseline lots
    skip: bool               # True -> EA should post 'rejected', skip OrderSend
    p_profit_used: float | None   # which probability drove the decision
    horizon_minutes: int | None   # which horizon was picked
    reason: str              # one-line explanation for forensics


# Uncalibrated table: (lower_bound_inclusive, upper_bound_exclusive, multiplier).
# Below the lowest band = skip.
#
# Rationale on the bands:
#   - p_profit < 0.45 — slightly below coin flip in a positive-EV system
#     means negative expected value once spread + slippage + R:R are
#     factored in. Skip is safer than tiny-lot.
#   - 0.45-0.50 — coin flip; small position to gather outcome data
#     without much exposure.
#   - 0.50-0.55 — small edge; baseline.
#   - 0.55-0.60 — typical "good setup"; this is the calibration anchor.
#   - 0.60-0.65 — strong setup; size up.
#   - >= 0.65 — exceptionally strong; cap multiplier (don't martingale
#     into a single conviction read).
#
# Total multipliers span 0 to 1.8x. Once empirical calibration arrives,
# expect the bands to shift but the band STRUCTURE to remain.
_DEFAULT_BANDS: tuple[tuple[float, float, float], ...] = (
    (0.45, 0.50, 0.4),
    (0.50, 0.55, 0.7),
    (0.55, 0.60, 1.0),
    (0.60, 0.65, 1.4),
    (0.65, 1.01, 1.8),  # 1.01 to be inclusive of exact 1.0 (paranoid LLM)
)
# Multiplier when no probabilities are available (synthesizer failed or
# disabled). 1.0 keeps existing EA behavior — sizing is opt-in.
_NO_DATA_MULTIPLIER = 1.0


def _pick_decisional_horizon(probabilities: list[dict]) -> dict | None:
    """Pick which horizon's p_profit drives sizing.

    Policy: the MEDIAN horizon of the requested set. The shortest is
    susceptible to noise, the longest is more theoretical than
    actionable. Median = the operator's typical hold target.

    For an odd-length list `(60, 240, 720)` median picks `240`. For
    even-length lists we use the lower-middle so sizing is conservative.
    """
    if not probabilities:
        return None
    # Sort by minutes to be robust to LLM-side ordering.
    sorted_p = sorted(
        (p for p in probabilities if isinstance(p, dict) and "minutes" in p),
        key=lambda p: p.get("minutes", 0),
    )
    if not sorted_p:
        return None
    return sorted_p[(len(sorted_p) - 1) // 2]


def compute_sizing(
    evaluation: dict[str, Any],
    *,
    skip_threshold: float = 0.45,
    bands: tuple[tuple[float, float, float], ...] = _DEFAULT_BANDS,
) -> SizingDecision:
    """Map an evaluation dict to a lot-size decision.

    `evaluation` is the dict produced by `evaluate_signal_v2`. Reads
    `probabilities` (list of {minutes, p_profit, p_drawdown_first})
    and `vetoes` (hard-veto list).

    `skip_threshold` is the p_profit below which sizing returns
    `skip=True`. Configurable per stack via the settings layer; here
    we accept it as a parameter so the function stays pure.

    Vetoes are honored: any veto -> skip with the veto reason. This
    duplicates the catalyst sub-scorer's veto effect at the sizing
    layer too, so a degraded synthesizer can't accidentally fire
    inside an NFP window.
    """
    # Hard veto: catalyst-imminent news, etc.
    vetoes = evaluation.get("vetoes") or []
    if vetoes:
        return SizingDecision(
            multiplier=0.0,
            skip=True,
            p_profit_used=None,
            horizon_minutes=None,
            reason=f"veto: {vetoes[0]}",
        )

    probabilities = evaluation.get("probabilities") or []
    decisional = _pick_decisional_horizon(probabilities)
    if decisional is None:
        # No probabilities — synthesizer didn't run or returned empty.
        # Don't skip; just use baseline multiplier. The deterministic
        # composer's score is still in the dict, just not as the
        # probability axis.
        return SizingDecision(
            multiplier=_NO_DATA_MULTIPLIER,
            skip=False,
            p_profit_used=None,
            horizon_minutes=None,
            reason="no synthesizer probabilities; baseline multiplier",
        )

    p = decisional.get("p_profit")
    minutes = decisional.get("minutes")
    if not isinstance(p, (int, float)) or not isinstance(minutes, (int, float)):
        return SizingDecision(
            multiplier=_NO_DATA_MULTIPLIER,
            skip=False,
            p_profit_used=None,
            horizon_minutes=None,
            reason="malformed probability entry; baseline multiplier",
        )

    if p < skip_threshold:
        return SizingDecision(
            multiplier=0.0,
            skip=True,
            p_profit_used=p,
            horizon_minutes=int(minutes),
            reason=f"p_profit={p:.3f} < skip_threshold={skip_threshold:.2f}",
        )

    for lo, hi, mult in bands:
        if lo <= p < hi:
            return SizingDecision(
                multiplier=mult,
                skip=False,
                p_profit_used=p,
                horizon_minutes=int(minutes),
                reason=f"p_profit={p:.3f} in band [{lo:.2f},{hi:.2f}) -> {mult}x",
            )

    # p above the highest band's upper bound (impossible with the
    # 1.01 cap above, but defensive).
    return SizingDecision(
        multiplier=bands[-1][2] if bands else _NO_DATA_MULTIPLIER,
        skip=False,
        p_profit_used=p,
        horizon_minutes=int(minutes),
        reason=f"p_profit={p:.3f} above bands; using top multiplier",
    )


def to_dict(d: SizingDecision) -> dict[str, Any]:
    """Serialize a SizingDecision for embedding in `evaluation["sizing"]`."""
    return {
        "multiplier": round(d.multiplier, 3),
        "skip": d.skip,
        "p_profit_used": (
            round(d.p_profit_used, 3) if d.p_profit_used is not None else None
        ),
        "horizon_minutes": d.horizon_minutes,
        "reason": d.reason,
    }
