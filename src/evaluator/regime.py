"""Regime classifier.

A regime is the conditional under which scores are interpreted. Same
chart pattern produces different outcomes in (real_yield_down + DXY_down)
versus (real_yield_up + DXY_up). Tagging the regime explicitly lets:

  - The composer apply regime-conditional multipliers (e.g. damp trend
    scores during compressed-vol regimes where breakouts fail more).
  - The LLM synthesizer (Step 5) get a one-line conditional instead of
    re-deriving from the raw inputs.
  - Calibration partition by regime — a "70" in trend-up macro-supportive
    is empirically not the same probability as a "70" in macro-headwind.

The regime is a tuple of five labels chosen to be jointly exhaustive
without over-fragmenting. With 5 dimensions × ~4 levels each, ~1000
regime tags exist in principle; in practice gold spends most of its
time in 10-20 of them.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.evaluator.features import unwrap_macro_feature


@dataclass(frozen=True)
class RegimeTag:
    """Compact regime label. `summary` joins the five components with
    pipes for human-readable logging."""
    macro: str        # real_yield_down_dxy_down | real_yield_up_dxy_up | mixed | unknown
    vol: str          # compressed | normal | elevated | panic | unknown
    trend: str        # aligned_up | aligned_down | mixed | flat | unknown
    session: str      # asian | london | ny_overlap | ny_afternoon | late | weekend
    catalyst: str     # clean | pre_news | post_news

    @property
    def summary(self) -> str:
        return f"{self.macro}|{self.vol}|{self.trend}|{self.session}|{self.catalyst}"


# ---- Vol thresholds ---------------------------------------------------
#
# VIX bands aren't standard — quants use different splits. The bands
# below match practitioner consensus for current-cycle equities and
# carry sensible second-order effects for gold (high vol = risk-off
# tailwind for BUY, headwind for SELL).
_VIX_COMPRESSED = 13.0
_VIX_NORMAL_HIGH = 20.0
_VIX_PANIC = 30.0


def _classify_macro(inputs: dict[str, Any]) -> str:
    """Two-axis macro regime from real yield direction + DXY direction.

    Both inputs are macro features (dicts with `chg_pct`). The single
    most-important driver for gold is real yields; DXY is the
    confirmation. When both agree, the regime is named for the agreement
    direction; when they conflict, "mixed"; when both are missing,
    "unknown".
    """
    _, ry_chg = unwrap_macro_feature(inputs.get("real_yield_10y") or inputs.get("real_yield"))
    _, dxy_chg = unwrap_macro_feature(inputs.get("dxy"))
    if ry_chg is None and dxy_chg is None:
        return "unknown"
    # Threshold to consider a daily change "meaningful" — 0.05% on real
    # yields and 0.15% on DXY filter out daily noise.
    ry_dir = 0
    if ry_chg is not None:
        if ry_chg > 0.05:
            ry_dir = +1
        elif ry_chg < -0.05:
            ry_dir = -1
    dxy_dir = 0
    if dxy_chg is not None:
        if dxy_chg > 0.15:
            dxy_dir = +1
        elif dxy_chg < -0.15:
            dxy_dir = -1
    if ry_dir == +1 and dxy_dir == +1:
        return "real_yield_up_dxy_up"        # gold headwind
    if ry_dir == -1 and dxy_dir == -1:
        return "real_yield_down_dxy_down"    # gold tailwind
    if ry_dir != 0 and dxy_dir != 0 and ry_dir != dxy_dir:
        return "mixed"
    if ry_dir != 0:
        return f"real_yield_{'up' if ry_dir > 0 else 'down'}_dxy_flat"
    if dxy_dir != 0:
        return f"real_yield_flat_dxy_{'up' if dxy_dir > 0 else 'down'}"
    return "neutral"


def _classify_vol(inputs: dict[str, Any]) -> str:
    """VIX level. Bands chosen to map to gold's behavioral regimes —
    compressed-vol means failed breakouts; panic means safe-haven flows
    dominate."""
    vix_v, _ = unwrap_macro_feature(inputs.get("vix"))
    if vix_v is None:
        return "unknown"
    if vix_v < _VIX_COMPRESSED:
        return "compressed"
    if vix_v < _VIX_NORMAL_HIGH:
        return "normal"
    if vix_v < _VIX_PANIC:
        return "elevated"
    return "panic"


def _classify_trend(inputs: dict[str, Any]) -> str:
    """Multi-timeframe trend alignment.

    Reads D1 close vs SMA50 and H1 net delta. Aligned up/down when both
    agree; mixed when they conflict; flat when neither is decisive;
    unknown when snapshot is missing entirely. This mirrors what the
    state_summary's `_compute_trend_labels` produces but is restated
    here so the regime classifier is self-contained.
    """
    d1 = inputs.get("d1")
    closes = inputs.get("h1_recent_closes")
    d1_dir = 0
    if isinstance(d1, dict):
        c = d1.get("close")
        sma50 = d1.get("sma50")
        if c is not None and sma50 is not None and sma50 > 0:
            diff_pct = (c - sma50) / sma50 * 100.0
            if diff_pct > 0.1:
                d1_dir = +1
            elif diff_pct < -0.1:
                d1_dir = -1
    h1_dir = 0
    if isinstance(closes, list) and len(closes) >= 2:
        delta = closes[-1] - closes[0]
        # ~0.05% threshold on h1 closes — gold ticks at $0.01 so 5pts on
        # $4800 is 0.1%. Less than that is noise.
        if delta > 5.0:
            h1_dir = +1
        elif delta < -5.0:
            h1_dir = -1
    if d1_dir == +1 and h1_dir == +1:
        return "aligned_up"
    if d1_dir == -1 and h1_dir == -1:
        return "aligned_down"
    if d1_dir != 0 and h1_dir != 0 and d1_dir != h1_dir:
        return "mixed"
    if d1_dir == 0 and h1_dir == 0:
        if d1 is None and closes is None:
            return "unknown"
        return "flat"
    # One direction known, the other neutral.
    primary = d1_dir or h1_dir
    return f"weakly_{'up' if primary > 0 else 'down'}"


def _classify_session(now: datetime | None = None) -> str:
    """Same bucketing as `ai_evaluator._session_label`. Repeated here
    so the regime classifier doesn't import from the legacy module."""
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:
        return "weekend"
    h = now.hour
    if 0 <= h < 7:    return "asian"
    if 7 <= h < 12:   return "london"
    if 12 <= h < 16:  return "ny_overlap"
    if 16 <= h < 21:  return "ny_afternoon"
    return "late"


def _classify_catalyst(inputs: dict[str, Any]) -> str:
    """News-window state from the calendar feed.

    Reads `news_window` (computed elsewhere — see ai_evaluator) which
    should hold `{"next_event": {...}, "last_event": {...}}`. The
    regime tag is mostly used by the composer for hard vetoes; the
    catalyst sub-scorer reads the same data for soft scoring.
    """
    nw = inputs.get("news_window")
    if not isinstance(nw, dict):
        return "clean"
    nxt = nw.get("next_event") or {}
    last = nw.get("last_event") or {}
    next_min = nxt.get("minutes")
    next_impact = (nxt.get("impact") or "").lower()
    if next_impact == "high" and isinstance(next_min, (int, float)) and next_min < 30:
        return "pre_news"
    last_min = last.get("minutes")
    last_impact = (last.get("impact") or "").lower()
    if last_impact == "high" and isinstance(last_min, (int, float)) and last_min < 15:
        return "post_news"
    return "clean"


def classify(
    inputs: dict[str, Any], now: datetime | None = None
) -> RegimeTag:
    """Compose the full regime tag from the gathered inputs.

    Pure function: same inputs -> same tag. Time-dependent only via the
    `now` parameter (which the caller controls for tests; in production
    the default `datetime.now(timezone.utc)` is used).
    """
    return RegimeTag(
        macro=_classify_macro(inputs),
        vol=_classify_vol(inputs),
        trend=_classify_trend(inputs),
        session=_classify_session(now),
        catalyst=_classify_catalyst(inputs),
    )
