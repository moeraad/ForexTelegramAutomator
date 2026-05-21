"""Five direction-relative sub-scorers, one per rubric family.

Each scorer reads the gathered-inputs dict, returns an AxisScore. Score
is in [-1, +1] where +1 = totally favorable for the proposed direction
and -1 = totally against. The composer multiplies by the trading-style's
axis weight.

All scorers are pure: same (side, inputs) -> same AxisScore. They never
raise — missing inputs degrade the score to 0.0 with `inputs_missing`
populated, and the composer downgrades data_quality accordingly.
"""
from __future__ import annotations

from typing import Any

from src.evaluator.features import AxisScore, clamp, unwrap_macro_feature


# Sides we accept. Anything else short-circuits to a zero score.
_VALID_SIDES = frozenset({"BUY", "SELL"})


def _side_sign(side: str) -> int:
    """+1 for BUY, -1 for SELL. Lets a single arithmetic expression
    flip direction-relative interpretations."""
    return +1 if side == "BUY" else -1


def _empty_score(reason: str) -> AxisScore:
    return AxisScore(score=0.0, rationale=reason, inputs_missing=["side"])


def score_trend(side: str, inputs: dict[str, Any]) -> AxisScore:
    """Multi-timeframe trend alignment.

    Inputs read: d1 (close + sma50), h4 (open + close), h1_recent_closes.
    Each timeframe contributes a signed component scaled to its weight
    within the family. Missing timeframes contribute 0 and are listed
    in `inputs_missing`.

    Logic:
      - D1 trend (close > SMA50, slope) -> ±0.5
      - H4 bar direction (close > open)  -> ±0.2
      - H1 recent closes net direction   -> ±0.3
      All flipped by `side` so BUY in uptrend = +1, SELL in uptrend = -1.
    """
    if side not in _VALID_SIDES:
        return _empty_score("invalid side")
    sign = _side_sign(side)
    score = 0.0
    used: list[str] = []
    missing: list[str] = []
    parts: list[str] = []

    d1 = inputs.get("d1")
    if isinstance(d1, dict):
        c = d1.get("close")
        sma50 = d1.get("sma50")
        if c is not None and sma50 is not None and sma50 > 0:
            diff_pct = (c - sma50) / sma50 * 100.0
            # ±0.5 capped — magnitude past ±2% on D1 vs SMA50 is rare
            # enough that we don't need to amplify further.
            d1_component = clamp(diff_pct / 2.0, -1.0, 1.0) * 0.5
            score += sign * d1_component
            used.append("d1")
            parts.append(f"D1 close {'>' if d1_component > 0 else '<'} SMA50 ({diff_pct:+.2f}%)")
    if d1 is None:
        missing.append("d1")

    h4 = inputs.get("h4")
    if isinstance(h4, dict):
        o = h4.get("open")
        c = h4.get("close")
        if o is not None and c is not None and o > 0:
            body_pct = (c - o) / o * 100.0
            h4_component = clamp(body_pct / 1.0, -1.0, 1.0) * 0.2
            score += sign * h4_component
            used.append("h4")
            parts.append(f"H4 {'bull' if c > o else 'bear'} bar ({body_pct:+.2f}%)")
    elif h4 is None:
        missing.append("h4")

    closes = inputs.get("h1_recent_closes")
    if isinstance(closes, list) and len(closes) >= 2:
        delta = closes[-1] - closes[0]
        # ~$10 net move on H1 5-bar window is decisive; scale.
        h1_component = clamp(delta / 10.0, -1.0, 1.0) * 0.3
        score += sign * h1_component
        used.append("h1_recent_closes")
        parts.append(f"H1 net {delta:+.2f}")
    elif closes is None:
        missing.append("h1_recent_closes")

    score = clamp(score)
    rationale = (
        f"{side} trend axis: {'; '.join(parts)}" if parts else
        f"{side} trend axis: no timeframe data"
    )
    return AxisScore(
        score=round(score, 3),
        rationale=rationale,
        inputs_used=used,
        inputs_missing=missing,
    )


def score_levels(side: str, inputs: dict[str, Any]) -> AxisScore:
    """Proximity to key levels + range exhaustion.

    Logic:
      - BUY near support / SELL near resistance -> favorable (+).
      - BUY near resistance / SELL near support -> unfavorable (-).
      - High % of today's ADR already used -> mean-reversion bias;
        unfavorable for continuation in the prevailing direction.

    "Near" = within 10 points (XAUUSD: ~0.2% of spot). The asymmetric
    treatment of support vs resistance is the levels axis: where you
    are matters, not just how strong each level is.
    """
    if side not in _VALID_SIDES:
        return _empty_score("invalid side")
    score = 0.0
    used: list[str] = []
    missing: list[str] = []
    parts: list[str] = []

    mid = inputs.get("market_mid")
    d1 = inputs.get("d1") if isinstance(inputs.get("d1"), dict) else None
    d1_prev = inputs.get("d1_prev") if isinstance(inputs.get("d1_prev"), dict) else None

    # Proximity to PDH / PDL.
    if mid is not None and d1_prev is not None:
        pdh = d1_prev.get("high")
        pdl = d1_prev.get("low")
        if pdh is not None:
            dist_pdh = pdh - mid
            if abs(dist_pdh) <= 10.0:
                if side == "BUY":
                    score -= 0.4
                    parts.append(f"BUY {dist_pdh:+.1f} from PDH (resistance close)")
                else:
                    score += 0.4
                    parts.append(f"SELL {dist_pdh:+.1f} from PDH (continuation room)")
                used.append("d1_prev")
        if pdl is not None:
            dist_pdl = pdl - mid
            if abs(dist_pdl) <= 10.0:
                if side == "BUY":
                    score += 0.4
                    parts.append(f"BUY {dist_pdl:+.1f} from PDL (support close)")
                else:
                    score -= 0.4
                    parts.append(f"SELL {dist_pdl:+.1f} from PDL (capitulation risk)")
                used.append("d1_prev")
    if d1_prev is None:
        missing.append("d1_prev")
    if mid is None:
        missing.append("market_mid")

    # Range exhaustion: percentage of today's range already used.
    adr20 = inputs.get("adr20")
    if d1 is not None and adr20 is not None and adr20 > 0:
        high = d1.get("high")
        low = d1.get("low")
        if high is not None and low is not None:
            range_used_pct = (high - low) / adr20
            # 1.5× ADR exhausted -> strong mean-reversion bias against
            # whichever direction the day was driven.
            if range_used_pct > 1.5:
                # Penalize signal that asks for continuation of today's move.
                close = d1.get("close")
                open_ = d1.get("open")
                if close is not None and open_ is not None:
                    day_direction = +1 if close > open_ else -1
                    if _side_sign(side) == day_direction:
                        score -= 0.3
                        parts.append(
                            f"{range_used_pct:.2f}x ADR used in same direction "
                            f"(mean-reversion bias)"
                        )
                used.append("adr20")
                used.append("d1")
    if adr20 is None:
        missing.append("adr20")

    score = clamp(score)
    rationale = (
        f"{side} levels axis: {'; '.join(parts)}" if parts else
        f"{side} levels axis: no level proximity within 10pts"
    )
    return AxisScore(
        score=round(score, 3),
        rationale=rationale,
        inputs_used=list(set(used)),
        inputs_missing=list(set(missing)),
    )


def score_regime(side: str, inputs: dict[str, Any]) -> AxisScore:
    """Volatility regime + trend strength + session quality.

    Logic:
      - ADX H1 < 20 -> chop, directional bets weak regardless of side -> negative.
      - ADX H1 > 25 -> trend present, favorable IF the prevailing trend
        aligns with `side` (we re-derive trend direction here so the
        scorer is self-contained).
      - VIX panic + side=BUY -> favorable (safe-haven flow); panic +
        SELL -> unfavorable.
      - Session: NY-overlap adds +0.1 for either side (best directional
        window); late session -0.1 (range-prone).
    """
    if side not in _VALID_SIDES:
        return _empty_score("invalid side")
    sign = _side_sign(side)
    score = 0.0
    used: list[str] = []
    missing: list[str] = []
    parts: list[str] = []

    adx = inputs.get("adx_h1")
    if isinstance(adx, (int, float)):
        if adx < 20:
            score -= 0.4
            parts.append(f"ADX H1 {adx:.1f} chop")
        elif adx > 25:
            # Need to know which direction the trend is so we can credit
            # alignment. Reuse h1_recent_closes net.
            closes = inputs.get("h1_recent_closes")
            if isinstance(closes, list) and len(closes) >= 2:
                trend_dir = +1 if closes[-1] > closes[0] else -1
                if sign == trend_dir:
                    score += 0.4
                    parts.append(f"ADX H1 {adx:.1f} trend aligns with {side}")
                else:
                    score -= 0.4
                    parts.append(f"ADX H1 {adx:.1f} trend AGAINST {side}")
            else:
                # ADX strong but direction unknown — small neutral credit.
                score += 0.1
                parts.append(f"ADX H1 {adx:.1f} trend present (direction unclear)")
        used.append("adx_h1")
    elif adx is None:
        missing.append("adx_h1")

    vix_v, _ = unwrap_macro_feature(inputs.get("vix"))
    if vix_v is not None:
        if vix_v > 30:
            # Panic — gold is the haven; BUY gets a tailwind.
            score += 0.3 * sign
            parts.append(f"VIX panic {vix_v:.1f}")
        elif vix_v < 13:
            # Compressed -> failed breakouts; mild penalty either side.
            score -= 0.1
            parts.append(f"VIX compressed {vix_v:.1f}")
        used.append("vix")
    elif inputs.get("vix") is None:
        missing.append("vix")

    score = clamp(score)
    if parts:
        rationale = f"{side} regime axis: {'; '.join(parts)}"
    elif missing:
        rationale = f"{side} regime axis: no regime data ({', '.join(missing)} missing)"
    else:
        rationale = f"{side} regime axis: normal (no extreme readings)"
    return AxisScore(
        score=round(score, 3),
        rationale=rationale,
        inputs_used=used,
        inputs_missing=missing,
    )


def score_macro(side: str, inputs: dict[str, Any]) -> AxisScore:
    """Cross-asset macro alignment with the proposed direction.

    Logic for BUY (flipped for SELL):
      - Real yields ↓ today -> +0.5 (strongest empirical driver)
      - DXY ↓ today          -> +0.3
      - COT managed-money percentile >85 -> contrarian -0.3
      - COT percentile <15 -> contrarian +0.3
      - Geopolitical index high -> +0.2 (safe-haven flow)
      - 30d ETF flow trend positive -> +0.2 (accumulation)
    """
    if side not in _VALID_SIDES:
        return _empty_score("invalid side")
    sign = _side_sign(side)
    score = 0.0
    used: list[str] = []
    missing: list[str] = []
    parts: list[str] = []

    _, ry_chg = unwrap_macro_feature(
        inputs.get("real_yield_10y") or inputs.get("real_yield")
    )
    if ry_chg is not None:
        # 0.5% real-yield daily move is enormous; scale aggressively.
        component = clamp(-ry_chg / 0.5, -1.0, 1.0) * 0.5  # ↓ favors BUY
        score += sign * component
        used.append("real_yield_10y")
        parts.append(f"real yield {ry_chg:+.2f}%")
    else:
        missing.append("real_yield_10y")

    _, dxy_chg = unwrap_macro_feature(inputs.get("dxy"))
    if dxy_chg is not None:
        # 0.5% DXY daily is large; scale.
        component = clamp(-dxy_chg / 0.5, -1.0, 1.0) * 0.3  # ↓ favors BUY
        score += sign * component
        used.append("dxy")
        parts.append(f"DXY {dxy_chg:+.2f}%")
    else:
        missing.append("dxy")

    cot_pct = inputs.get("cot_extremes_percentile")
    if isinstance(cot_pct, (int, float)):
        if cot_pct > 85:
            # Stretched longs -> contrarian; BUY is fading the crowd.
            score -= 0.3 * sign
            parts.append(f"COT longs at {cot_pct:.0f}th pctile (stretched)")
        elif cot_pct < 15:
            score += 0.3 * sign
            parts.append(f"COT longs at {cot_pct:.0f}th pctile (capitulation)")
        used.append("cot_extremes_percentile")
    elif cot_pct is None:
        missing.append("cot_extremes_percentile")

    geo = inputs.get("geopolitical_index")
    if isinstance(geo, (int, float)):
        if geo > 0.6:
            # Crisis premium -> tailwind for BUY, headwind for SELL.
            score += 0.2 * sign
            parts.append(f"geopolitical index {geo:.2f}")
        used.append("geopolitical_index")
    elif geo is None:
        missing.append("geopolitical_index")

    etf_trend = inputs.get("etf_flows_30d_trend")
    if isinstance(etf_trend, (int, float)):
        if etf_trend > 0:
            score += 0.2 * sign
            parts.append(f"ETF 30d trend +${etf_trend:.0f}M")
        elif etf_trend < 0:
            score -= 0.2 * sign
            parts.append(f"ETF 30d trend -${abs(etf_trend):.0f}M")
        used.append("etf_flows_30d_trend")
    elif etf_trend is None:
        missing.append("etf_flows_30d_trend")

    score = clamp(score)
    rationale = (
        f"{side} macro axis: {'; '.join(parts)}" if parts else
        f"{side} macro axis: no macro data"
    )
    return AxisScore(
        score=round(score, 3),
        rationale=rationale,
        inputs_used=used,
        inputs_missing=missing,
    )


def score_catalyst(side: str, inputs: dict[str, Any]) -> AxisScore:
    """News-window catalysts. Mostly a veto — within 30min of high-impact
    news, return a heavily negative score (the composer maps this into
    a hard score cap). After-news whipsaw window is a soft penalty.

    A "neutral" return (score=0.0) is the common case — most messages
    arrive outside news windows.
    """
    if side not in _VALID_SIDES:
        return _empty_score("invalid side")
    nw = inputs.get("news_window")
    if not isinstance(nw, dict):
        return AxisScore(
            score=0.0,
            rationale=f"{side} catalyst axis: no news window data",
            inputs_missing=["news_window"],
        )
    nxt = nw.get("next_event") or {}
    last = nw.get("last_event") or {}
    next_min = nxt.get("minutes")
    next_impact = (nxt.get("impact") or "").lower()
    if next_impact == "high" and isinstance(next_min, (int, float)) and next_min < 30:
        return AxisScore(
            score=-1.0,
            rationale=(
                f"{side} catalyst veto: high-impact {nxt.get('event','?')!r} "
                f"in {next_min} min"
            ),
            inputs_used=["news_window"],
        )
    last_min = last.get("minutes")
    last_impact = (last.get("impact") or "").lower()
    if last_impact == "high" and isinstance(last_min, (int, float)) and last_min < 15:
        return AxisScore(
            score=-0.5,
            rationale=(
                f"{side} catalyst whipsaw: {last_min}min after high-impact "
                f"{last.get('event','?')!r}"
            ),
            inputs_used=["news_window"],
        )
    return AxisScore(
        score=0.0,
        rationale=f"{side} catalyst axis: clear",
        inputs_used=["news_window"],
    )


def score_all(side: str, inputs: dict[str, Any]) -> dict[str, AxisScore]:
    """Convenience: run all five sub-scorers and return them keyed by
    axis family name. The composer reads this dict by name."""
    return {
        "trend": score_trend(side, inputs),
        "levels": score_levels(side, inputs),
        "regime": score_regime(side, inputs),
        "macro": score_macro(side, inputs),
        "catalyst": score_catalyst(side, inputs),
    }
