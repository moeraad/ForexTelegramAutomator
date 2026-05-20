"""Trading-style taxonomy + per-style data requirements.

A trading style is the operator's stated holding-period intent — scalp,
intraday, swing, position. The evaluator uses it to:

  1. Choose which input features matter (intraday cares about session
     phase + ADR-used; swing cares about real yields + COT).
  2. Choose which probability horizons to score (scalp: 15m/1h; swing:
     1d/3d).
  3. Choose the rubric weights between trend / macro / levels / catalyst.

Two layers of control:

  - **Stack default** (settings.trading_style) — the operator's normal
    holding period for that channel.
  - **AI per-signal override** (settings.trading_style_ai_override) — when
    on, the AI may infer a different style from the message text
    ("اسكالب فقط" → scalp; "صفقة طويلة" → swing) and override the default
    for that one signal.

The set of styles is closed. New styles get added here, not by free-text
in the DB. Each style declares its required feature feeds so the data
worker can subscribe to only what's actually used across active stacks.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable


class TradingStyle(str, Enum):
    """Closed set of supported trading styles.

    `str` mixin: each value is its own string, so storing
    `TradingStyle.INTRADAY.value` in settings is a plain ASCII tag and
    `TradingStyle("intraday")` round-trips without ceremony.
    """
    SCALP = "scalp"
    INTRADAY = "intraday"
    SWING = "swing"
    POSITION = "position"


# Default when no style is set yet (fresh stack, post-migration row).
# Intraday covers the most common Telegram channel cadence — minutes-to-
# hours holds — so it's the safest blank-slate guess.
DEFAULT_STYLE = TradingStyle.INTRADAY


@dataclass(frozen=True)
class StyleSpec:
    """All the per-style knobs the evaluator and feed workers consult.

    `horizons_minutes` — the time-to-outcome buckets the evaluator scores
    probabilities for. Must be sorted ascending. The EA / dashboard will
    surface the median horizon for the operator-facing verdict.

    `weights` — relative axis emphasis. Must sum to 1.0 (validated at
    import time). The evaluator multiplies sub-scores by these weights
    when composing the final probability; the rubric is style-aware.

    `required_features` — names of pre-computed feature blocks the feed
    worker must keep fresh for this style to produce a full-quality
    evaluation. When a feature is missing/stale, the evaluator drops to
    reduced mode (per existing behavior).

    `max_signal_age_minutes` — how stale a signal can be before this
    style declines to act on it. Scalp dies fast; position can tolerate
    hours of latency. Used by the orchestrator if/when we wire age-based
    rejection.
    """
    style: TradingStyle
    horizons_minutes: tuple[int, ...]
    weights: "AxisWeights"
    required_features: frozenset[str]
    max_signal_age_minutes: int
    description: str

    def __post_init__(self) -> None:
        # Sanity checks at import time — a malformed style is a config
        # bug, not a runtime concern. Fail fast.
        if not self.horizons_minutes:
            raise ValueError(f"{self.style.value}: horizons must be non-empty")
        if list(self.horizons_minutes) != sorted(self.horizons_minutes):
            raise ValueError(f"{self.style.value}: horizons must be ascending")
        if not (0.99 <= self.weights.total <= 1.01):
            raise ValueError(
                f"{self.style.value}: weights sum to "
                f"{self.weights.total:.4f}, expected ~1.0"
            )


@dataclass(frozen=True)
class AxisWeights:
    """Per-family weights for the evaluator's rubric. Must sum to ~1.0.

    Five families chosen to match the analyst-rationale grouping:

      - `trend`     — multi-timeframe trend alignment (the structural bias)
      - `levels`    — proximity to S/R, range exhaustion
      - `regime`    — volatility, ADX, session, intraday tape character
      - `macro`     — DXY, real yields, VIX, Fed expectations
      - `catalyst`  — news window, scheduled events (mostly vetoes)

    Styles emphasize different families: scalp leans regime + levels,
    swing leans macro + trend, etc.
    """
    trend: float
    levels: float
    regime: float
    macro: float
    catalyst: float

    @property
    def total(self) -> float:
        return self.trend + self.levels + self.regime + self.macro + self.catalyst


# ---- Per-style specifications -------------------------------------------
#
# These are the only places where the per-style numbers live. Editing a
# weight here propagates to the evaluator, feed worker, and GUI status
# panel automatically.

_SPECS: dict[TradingStyle, StyleSpec] = {
    TradingStyle.SCALP: StyleSpec(
        style=TradingStyle.SCALP,
        # Seconds-to-minutes hold. Probabilities at 15m, 1h, 4h — past
        # that, the edge for a scalp is decayed regardless of direction.
        horizons_minutes=(15, 60, 240),
        weights=AxisWeights(
            trend=0.15,    # tactical, not structural
            levels=0.30,   # nearest S/R is the dominant scalp signal
            regime=0.35,   # session + spread + tick vol decide whether to fire at all
            macro=0.05,    # near-irrelevant on a 15-min hold
            catalyst=0.15, # hard veto inside news windows
        ),
        required_features=frozenset({
            "market_price", "ohlc_m15", "ohlc_h1",
            "session_phase", "spread_bp", "adr_used_pct",
            "key_levels_intraday", "news_window",
            "atr_h1", "h1_recent_closes",
        }),
        max_signal_age_minutes=5,
        description=(
            "Seconds-to-minutes holds. Session phase, spread, and "
            "level proximity dominate. Macro inputs are largely "
            "irrelevant; catalyst veto is hard."
        ),
    ),
    TradingStyle.INTRADAY: StyleSpec(
        style=TradingStyle.INTRADAY,
        # Hours hold, closed by NY end. 1h / 4h / 12h cover the realistic
        # outcome window.
        horizons_minutes=(60, 240, 720),
        weights=AxisWeights(
            trend=0.30,
            levels=0.25,
            regime=0.20,
            macro=0.15,
            catalyst=0.10,
        ),
        required_features=frozenset({
            "market_price", "ohlc_m15", "ohlc_h1", "ohlc_h4",
            "ohlc_d1", "ohlc_d1_prev",
            "atr_h1", "adr20", "adx_h1", "h1_recent_closes",
            "key_levels_intraday", "session_phase",
            "dxy", "vix",
            "news_window",
        }),
        max_signal_age_minutes=30,
        description=(
            "Hours-long intraday holds, typically closed by NY end. "
            "Balanced rubric — trend and macro both matter, levels "
            "and session shape execution."
        ),
    ),
    TradingStyle.SWING: StyleSpec(
        style=TradingStyle.SWING,
        # Multi-day. 4h / 24h / 72h. 4h horizon catches the first move;
        # 72h is where the macro thesis is supposed to play out.
        horizons_minutes=(240, 1440, 4320),
        weights=AxisWeights(
            trend=0.35,
            levels=0.15,
            regime=0.10,
            macro=0.30,
            catalyst=0.10,
        ),
        required_features=frozenset({
            "market_price", "ohlc_h4", "ohlc_d1", "ohlc_d1_prev",
            "ohlc_w1",
            "atr_h1", "adr20", "adx_h1",
            "key_levels_swing",
            "dxy", "real_yield_10y", "tnx", "vix",
            "cot_managed_money", "etf_flows_gld",
            "silver", "copper", "usdjpy",
            "news_window", "fed_funds_path",
        }),
        max_signal_age_minutes=180,
        description=(
            "Multi-day holds. Macro (real yields, DXY trend, COT, ETF "
            "flows) dominates. Multi-timeframe structure on H4/D1/W1."
        ),
    ),
    TradingStyle.POSITION: StyleSpec(
        style=TradingStyle.POSITION,
        # Weeks-to-months. Beyond 72h the directional probability flattens
        # into a coin flip without macro thesis, so the longest horizon
        # is bounded; the evaluator surfaces a thesis-quality verdict
        # rather than a tight number for these.
        horizons_minutes=(1440, 4320, 10080),  # 1d, 3d, 7d
        weights=AxisWeights(
            trend=0.25,
            levels=0.10,
            regime=0.05,
            macro=0.50,    # macro thesis is the whole game
            catalyst=0.10,
        ),
        required_features=frozenset({
            "market_price", "ohlc_d1", "ohlc_w1", "ohlc_mn1",
            "dxy", "real_yield_10y", "real_yield_trend_30d",
            "tnx", "vix", "fed_funds_path", "breakevens_5y5y",
            "cot_managed_money", "cot_extremes_percentile",
            "etf_flows_gld", "etf_flows_30d_trend",
            "silver", "copper",
            "central_bank_buying", "geopolitical_index",
        }),
        max_signal_age_minutes=720,
        description=(
            "Weeks-to-months. Macro thesis (real yields trend, Fed "
            "expectations, central-bank flows) is the dominant input. "
            "Technicals are weekly/monthly only."
        ),
    ),
}


def get_spec(style: TradingStyle | str) -> StyleSpec:
    """Resolve a style enum or string tag to its spec.

    Tolerant of string inputs because settings stores values as strings
    and the GUI roundtrips through them. Unknown strings raise — this is
    a closed set; silent fallback would hide config errors.
    """
    if isinstance(style, str):
        try:
            style = TradingStyle(style)
        except ValueError as e:
            raise ValueError(
                f"unknown trading style: {style!r}. Known: "
                f"{[s.value for s in TradingStyle]}"
            ) from e
    return _SPECS[style]


def all_styles() -> tuple[TradingStyle, ...]:
    """Iteration order matches the GUI dropdown order — shortest-hold
    first, longest-hold last. Operators read the list as a spectrum."""
    return (
        TradingStyle.SCALP,
        TradingStyle.INTRADAY,
        TradingStyle.SWING,
        TradingStyle.POSITION,
    )


def union_required_features(styles: Iterable[TradingStyle]) -> frozenset[str]:
    """Union of feature names required by the given set of styles.

    The feed worker uses this to decide which feeds to actually fetch:
    when only `intraday` is active across all stacks, it can skip the
    COT and ETF pulls that only `swing`/`position` need. Reduces both
    API quota usage and the surface for stale-data warnings.
    """
    out: set[str] = set()
    for s in styles:
        out.update(_SPECS[s].required_features)
    return frozenset(out)
