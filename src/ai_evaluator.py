"""Directional-bias evaluator for XAUUSD OPEN actions.

Runs ASYNCHRONOUSLY after the orchestrator inserts an OPEN action — i.e.,
the action is already 'pending' / on its way to execution by the time the
evaluator returns its score. This is by design: the evaluator is purely
informational for the dashboard / forensic audit, never gating execution.

**Scope (revised 2026-05-09):**
The evaluator no longer judges the SIGNAL'S MECHANICS (R:R, SL placement,
TP plausibility). It judges WHETHER OPENING A TRADE IN THE REQUESTED
DIRECTION makes sense given current market state. Signal parameters
(entry/SL/TPs) are taken as given; only `signal.side` matters.

The new question:

    P( direction D is profitable during the active session
       | current market state )

where D ∈ {BUY, SELL} is the signal's `side`.

Inputs (all read from the live DB at evaluation time):
  - Signal direction (`side` only — entry/SL/TPs are ignored)
  - Current market price + age (from settings, written by /market/price)
  - Multi-timeframe OHLC + ATR snapshot (from settings, written by
    /market/snapshot — extended with d1/d1_prev/adr20/adx_h1/h1_recent_closes)
  - Macro snapshot (from settings, written by Step-2 macro feed loop —
    optional; reduced mode while absent)
  - Recent precedent (last few trades on the symbol today)

Output: a dict suitable for json.dumps that gets written back into the
action's payload_json under the 'evaluation' key. Shape (preserved across
the rubric refactor so the dashboard and /actions/latest_open_evaluation
keep working unchanged):

    {
      "score": 72,                              # 0-100
      "verdict": "moderate",                    # strong | moderate | weak | avoid
      "key_factor": "...",                      # 1-line dominant reason
      "summary": "...",                         # 2-3 sentence rationale
      "factors": {                              # per-axis breakdown
         "T1": "...", "T2": "...", "T3": "...", "T4": "...",
         "M1": "...", "M2": "...", "M3": "...",
         "G1": "...", "G2": "...", "G3": "...",
         "C1": "...", "C2": "...", "C3": "..."
      },
      "data_quality": "full" | "reduced",       # 'reduced' = inputs missing/stale
      "missing": ["d1_snapshot", "macro_snapshot", ...],
      "evaluated_at": "2026-05-07T14:30:00+00:00"
    }

The verdict-to-color mapping the dashboard uses:
  80-100 -> strong   (green)
  60-79  -> moderate (amber)
  40-59  -> weak     (orange)
   0-39  -> avoid    (red)
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timedelta, timezone
from typing import Any

log = logging.getLogger(__name__)


# Freshness thresholds.
_SNAPSHOT_STALE_SECONDS = 120
_MACRO_STALE_SECONDS = 300


EVALUATOR_SYSTEM_PROMPT = """You are an expert XAUUSD (gold) directional-bias evaluator. You read the proposed trade direction (BUY or SELL) plus current market context, and produce a 0-100 conviction score for THAT DIRECTION being profitable during the active session. Your output is informational ONLY — it does NOT gate trade execution. The trade has already been queued by the time you score it.

YOU DO NOT EVALUATE THE SIGNAL'S PARAMETERS. Entry zone, SL, TPs, R:R — none of these are your concern. Treat them as given. Your single question: given current market state, is opening a position in the requested DIRECTION a good idea right now?

INPUTS YOU WILL RECEIVE:
  1. SIGNAL DIRECTION: BUY or SELL (the only signal field you use)
  2. CURRENT MARKET: bid/ask/mid (with age) — may be stale or missing
  3. MULTI-TIMEFRAME OHLC + ATR(14): m15, h1, h4 (always), plus d1 + d1_prev + adr20 + adx_h1 + h1_recent_closes (when EA pushes the extended snapshot)
  4. MACRO: DXY, US 10Y yield, VIX, JPY, oil — current values + today's % change. May be unavailable (Step-2 feed not yet shipped or feed down).
  5. SESSION + TIME: current trading session, time of day, day of week
  6. RECENT PRECEDENT: last few trades on XAUUSD today + their outcomes

EVALUATION FRAMEWORK — judge along 13 axes grouped into 4 families:

  TREND ALIGNMENT (40% weight) — does the direction agree with the market's actual trend?
    T1. D1 trend: price vs SMA50/SMA200 + slope. BUY against a falling D1 50-SMA = structural headwind.
    T2. H4 trend: close vs open + position vs recent swing. The intermediate-timeframe regime.
    T3. H1 trend: tactical timing. H1 against H4 against D1 = three-way mismatch = avoid.
    T4. Structural integrity on H1: are the recent H1 closes trending in the signal direction (HH/HL for BUY, LL/LH for SELL), or have they reversed?

  MARKET STATE (30% weight) — is the market regime supportive of a directional bet?
    M1. Range exhaustion: today's range used / ADR20. After 1.5x ADR consumed in one direction, mean-reversion probability rises sharply. Buying near today's high after a big up day is a poor direction signal.
    M2. Volatility regime: current H1 ATR vs 20-day-avg H1 ATR (use atr14 across the m15/h1/h4 fields as a proxy). Compressed-vol days produce false breakouts. Expansion days reward trend-following but punish counter-trend.
    M3. Trend vs chop: ADX H1. <20 = ranging (directional bets weak regardless of direction). 20-25 = transitional. >25 = trend present.

  MACRO (20% weight) — gold-specific cross-asset alignment
    G1. DXY direction today. Gold is dollar-inverse. BUY needs DXY ↓; SELL needs DXY ↑. DXY +0.5%/day on a BUY signal is a structural headwind.
    G2. US 10Y yield direction today. Gold is yield-inverse (gold pays no yield). Yields ↑ = headwind for BUY.
    G3. Risk regime (VIX level + intraday change). Risk-off (VIX up sharp) supports gold. Compressed VIX in sustained risk-on = gold drifts.

  CONTEXT (10% weight, mostly veto) — non-direction-specific filters
    C1. News window proximity. The NEWS block below tells you `next_event` (event name + impact + minutes) and `last_event` (most recent past). HARD VETO: if `next_event.impact == "high"` AND `next_event.minutes < 30`, cap your score at 50 regardless of other factors and call it out in `key_factor`. SOFT GUIDANCE: within 15 min AFTER a high-impact release the tape is whipsaw — do NOT score >60 even if trend looks aligned. Outside those windows, news has no effect.
    C2. Session fit: London-NY overlap (12:00-16:00 UTC) favours directional follow-through. Asian session moves often reverse during London. Late session (post-21:00 UTC) tends to range.
    C3. Recent precedent: re-entering the same direction within 30 min of a SL hit on that direction is statistically poor. Three consecutive losses in a direction today = that direction has been wrong in this regime.

REDUCED-CONTEXT MODE:
The MACRO block is only available once Step 2 (macro feed loop) ships. Until then it will say "(unavailable)". When that's the case:
  - Compute T1-T4, M1-M3, C1-C3 normally.
  - Set G1/G2/G3 each to "data_quality_limited: macro feed unavailable" in the factors dict.
  - Add "macro_snapshot" to the missing list.
  - Set "data_quality": "reduced".
  - Cap the final score at 70.

Same reduced rules apply if the OHLC snapshot is stale or absent (cap at 70, mark missing).

OUTPUT RULES:
  - Output a single JSON object. No prose outside JSON.
  - Score is integer 0-100. Verdict comes from the score band:
      80-100 strong | 60-79 moderate | 40-59 weak | 0-39 avoid
  - "key_factor" is a single short sentence — the dominant reason for the score.
  - "summary" is 1-3 sentences walking through the dominant trend / state / macro / context decision.
  - "factors" must include all 13 axis keys (T1, T2, T3, T4, M1, M2, M3, G1, G2, G3, C1, C2, C3) with one short string per axis. If an axis is impossible to judge from the data, write "data_quality_limited: <why>" rather than omit.
  - "missing" is a list of what data was unavailable. Empty list if everything was present.
  - "data_quality" is "full" if you had all inputs; "reduced" otherwise.

Be honest. If the direction faces structural headwinds, score it low. If trend, regime, and macro all align with the direction, score it high. Calibration matters more than agreement with the channel.
"""


def _parse_iso_age_seconds(iso_str: str | None) -> int | None:
    """Seconds since `iso_str` (UTC). Returns None if unparseable. Mirrors
    state_summary's _age_seconds — kept local to avoid the cross-import."""
    if not iso_str:
        return None
    try:
        s = iso_str.replace(" ", "T")
        if not (s.endswith("Z") or "+" in s[10:] or "-" in s[10:]):
            s = s + "+00:00"
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        ts = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return int((datetime.now(timezone.utc) - ts).total_seconds())


def _get_market_snapshot(
    conn: sqlite3.Connection,
    symbol: str = "XAUUSD",
    stale_seconds: int = _SNAPSHOT_STALE_SECONDS,
) -> tuple[dict | None, int | None]:
    """Return (snapshot_dict, age_seconds) or (None, None) when missing.

    snapshot_dict shape (when present, with directional-rubric extensions):
        {"m15": {open, high, low, close, atr14},
         "h1":  {open, high, low, close, atr14},
         "h4":  {open, high, low, close, atr14},
         "d1":  {open, high, low, close, atr14, sma50, sma200},   # optional
         "d1_prev": {...},                                          # optional
         "adr20": float,                                            # optional
         "adx_h1": float,                                           # optional
         "h1_recent_closes": [float, ...]}                          # optional

    The caller decides what to do when age > stale_seconds — typically
    pass the dict through anyway with a note that data is stale.
    """
    sym = symbol.upper()
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?,?)",
            (f"market_snapshot_{sym}", f"market_snapshot_{sym}_at"),
        ).fetchall()
    }
    raw = rows.get(f"market_snapshot_{sym}")
    at = rows.get(f"market_snapshot_{sym}_at")
    if not raw:
        return None, None
    try:
        snap = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    age = _parse_iso_age_seconds(at)
    return snap, age


def _get_market_mid(
    conn: sqlite3.Connection, symbol: str = "XAUUSD"
) -> tuple[float | None, int | None]:
    """Live bid/ask mid from /market/price. (mid, age_seconds) or (None, None)."""
    sym = symbol.upper()
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?,?,?)",
            (f"market_{sym}_bid", f"market_{sym}_ask", f"market_{sym}_at"),
        ).fetchall()
    }
    bid = rows.get(f"market_{sym}_bid")
    ask = rows.get(f"market_{sym}_ask")
    at = rows.get(f"market_{sym}_at")
    if bid is None or ask is None:
        return None, None
    try:
        mid = (float(bid) + float(ask)) / 2.0
    except (TypeError, ValueError):
        return None, None
    return mid, _parse_iso_age_seconds(at)


def _get_macro_snapshot(
    conn: sqlite3.Connection,
    stale_seconds: int = _MACRO_STALE_SECONDS,
) -> tuple[dict | None, int | None]:
    """Macro cross-asset snapshot from settings.macro_snapshot. Populated
    by the bot's macro_feed_loop (Step 2). Returns (None, None) until that
    ships; returns (snap, age) with age potentially > stale_seconds when
    the feed went silent — caller decides whether to use stale data.

    Expected shape when present:
        {"dxy": float, "dxy_chg_pct": float,
         "tnx": float, "tnx_chg_pct": float,
         "vix": float, "vix_chg_pct": float,
         "jpy": float, "jpy_chg_pct": float,
         "oil": float, "oil_chg_pct": float,
         "fetched_at": ISO-8601 UTC}
    """
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            ("macro_snapshot", "macro_snapshot_at"),
        ).fetchall()
    }
    raw = rows.get("macro_snapshot")
    at = rows.get("macro_snapshot_at")
    if not raw:
        return None, None
    try:
        snap = json.loads(raw)
    except (TypeError, ValueError):
        return None, None
    age = _parse_iso_age_seconds(at)
    return snap, age


def _session_label(now: datetime | None = None) -> str:
    """Trading-session label for gold based on UTC hour. Boundaries match
    practitioner conventions (London open 07:00 UTC, NY open 12:00 UTC,
    overlap window 12:00-16:00 UTC, NY close 21:00 UTC, Asian open 23:00).

    Returns one of: "asian", "london", "ny_overlap", "ny_afternoon",
    "late", "weekend".
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() >= 5:  # Sat=5, Sun=6
        return "weekend"
    h = now.hour
    if 0 <= h < 7:    return "asian"
    if 7 <= h < 12:   return "london"
    if 12 <= h < 16:  return "ny_overlap"  # the prime directional window
    if 16 <= h < 21:  return "ny_afternoon"
    return "late"  # 21:00-23:59 UTC


def _today_range_used(d1: dict | None, mid: float | None) -> dict | None:
    """Range-exhaustion summary derived from D1 OHLC + current mid.

    Returns a dict the prompt can read directly:
        {"range_today": float,
         "position_in_range_pct": float,  # 0=at low, 100=at high
         "from_open_pct": float}          # negative = below open

    None when D1 OHLC or mid is missing or the day's range is degenerate.
    """
    if not d1 or mid is None:
        return None
    high = d1.get("high")
    low = d1.get("low")
    open_ = d1.get("open")
    if high is None or low is None or open_ is None or high <= low or open_ <= 0:
        return None
    rng = high - low
    pos_in_range = ((mid - low) / rng) * 100.0
    from_open = ((mid - open_) / open_) * 100.0
    return {
        "range_today": rng,
        "position_in_range_pct": pos_in_range,
        "from_open_pct": from_open,
    }


def _get_recent_precedent(
    conn: sqlite3.Connection,
    symbol: str = "XAUUSD",
    lookback_hours: int = 12,
) -> dict:
    """Recent-precedent summary for the C3 axis: how have trades on this
    symbol fared in the recent window, and was the most recent close a SL hit?

    Output:
        {"trades_today": int,
         "last_close_reason": str | None,
         "last_close_side": str | None,
         "minutes_since_last_close": int | None,
         "consecutive_losses": int}   # contiguous SL hits at the head

    "trades_today" is a slight misnomer — it's "trades within lookback_hours" —
    but the AI reads it conceptually and the lookback default (12h) covers
    a typical session.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).isoformat()
    rows = conn.execute(
        "SELECT side, close_reason, closed_at "
        "FROM positions "
        "WHERE symbol = ? AND status = 'closed' AND closed_at IS NOT NULL "
        "  AND closed_at >= ? "
        "ORDER BY closed_at DESC",
        (symbol, cutoff),
    ).fetchall()
    if not rows:
        return {
            "trades_today": 0,
            "last_close_reason": None,
            "last_close_side": None,
            "minutes_since_last_close": None,
            "consecutive_losses": 0,
        }
    last = rows[0]
    last_age = _parse_iso_age_seconds(last["closed_at"])
    minutes_since = int(last_age / 60) if last_age is not None else None
    # SL hits on gold come back as 'mt5_sl' from the EA reconciler.
    consec = 0
    for r in rows:
        if r["close_reason"] == "mt5_sl":
            consec += 1
        else:
            break
    return {
        "trades_today": len(rows),
        "last_close_reason": last["close_reason"],
        "last_close_side": last["side"],
        "minutes_since_last_close": minutes_since,
        "consecutive_losses": consec,
    }


def _fmt_tf_block(tf_name: str, row: dict) -> str:
    """Format one timeframe row from the snapshot for the prompt. Includes
    sma50/sma200 only when populated (D1 today, possibly more later)."""
    base = (
        f"  {tf_name}: O={row.get('open', 0):.2f} H={row.get('high', 0):.2f} "
        f"L={row.get('low', 0):.2f} C={row.get('close', 0):.2f} "
        f"ATR14={row.get('atr14', 0):.2f}"
    )
    sma_parts = []
    if row.get("sma50") is not None:  sma_parts.append(f"SMA50={row['sma50']:.2f}")
    if row.get("sma200") is not None: sma_parts.append(f"SMA200={row['sma200']:.2f}")
    if sma_parts:
        base += "  " + " ".join(sma_parts)
    return base


def build_evaluator_input(
    signal: dict, conn: sqlite3.Connection, symbol: str = "XAUUSD"
) -> tuple[str, list[str]]:
    """Compose the user-facing input string for the evaluator LLM call,
    plus a list of `missing` data-element names. The LLM receives the
    string verbatim alongside EVALUATOR_SYSTEM_PROMPT.

    Direction-only: only `signal["side"]` is used. entry_low/high/sl/tps
    are intentionally NOT included (the evaluator no longer judges signal
    mechanics — see module docstring).
    """
    missing: list[str] = []

    side = signal.get("side", "?")
    mid, mid_age = _get_market_mid(conn, symbol)
    snapshot, snap_age = _get_market_snapshot(conn, symbol)
    macro, macro_age = _get_macro_snapshot(conn)
    precedent = _get_recent_precedent(conn, symbol)
    now = datetime.now(timezone.utc)
    session = _session_label(now)

    parts: list[str] = []

    # 1. Direction
    parts.append("PROPOSED DIRECTION:")
    parts.append(f"  side={side}  (signal mechanics ignored — evaluate the direction only)")
    parts.append("")

    # 2. Current market price
    parts.append("CURRENT MARKET:")
    if mid is None:
        parts.append("  (no recent quote — EA not heartbeating)")
        missing.append("market_price")
    else:
        age_str = f"age={mid_age}s" if mid_age is not None else "age=unknown"
        parts.append(f"  mid={mid:.2f}  {age_str}")
    parts.append("")

    # 3. Multi-timeframe OHLC
    parts.append("MULTI-TIMEFRAME OHLC + ATR(14):")
    if snapshot is None:
        parts.append("  (no snapshot — EA hasn't pushed /market/snapshot yet)")
        missing.append("ohlc_snapshot")
    elif snap_age is not None and snap_age > _SNAPSHOT_STALE_SECONDS:
        parts.append(f"  STALE: age={snap_age}s > {_SNAPSHOT_STALE_SECONDS}s — DO NOT trust trend/volatility axes")
        missing.append("ohlc_snapshot_stale")
    else:
        for tf in ("m15", "h1", "h4"):
            row = snapshot.get(tf, {})
            parts.append(_fmt_tf_block(tf, row))
        # Directional-rubric extensions — present only when EA pushes them.
        d1 = snapshot.get("d1")
        if d1:
            parts.append(_fmt_tf_block("d1", d1))
        else:
            missing.append("d1_snapshot")
        d1_prev = snapshot.get("d1_prev")
        if d1_prev:
            parts.append(_fmt_tf_block("d1_prev", d1_prev))
        adr20 = snapshot.get("adr20")
        if adr20 is not None:
            parts.append(f"  ADR20 (avg daily range, last 20 D1): {adr20:.2f}")
        else:
            missing.append("adr20")
        adx_h1 = snapshot.get("adx_h1")
        if adx_h1 is not None:
            parts.append(f"  ADX(14) H1: {adx_h1:.2f}  (<20 chop, 20-25 transitional, >25 trend)")
        else:
            missing.append("adx_h1")
        recent_closes = snapshot.get("h1_recent_closes")
        if recent_closes:
            closes_str = " -> ".join(f"{c:.2f}" for c in recent_closes)
            parts.append(f"  H1 recent closes (oldest -> newest): {closes_str}")
        # Range-used summary (derived).
        used = _today_range_used(d1, mid)
        if used:
            parts.append(
                f"  RANGE USED TODAY: {used['range_today']:.2f}  "
                f"(mid is at {used['position_in_range_pct']:.0f}% of today's range, "
                f"{used['from_open_pct']:+.2f}% from today's open)"
            )
    parts.append("")

    # 4. Macro
    parts.append("MACRO (cross-asset, today's daily change):")
    if macro is None:
        parts.append("  (unavailable — Step 2 macro feed not running)")
        missing.append("macro_snapshot")
    elif macro_age is not None and macro_age > _MACRO_STALE_SECONDS:
        parts.append(f"  STALE: age={macro_age}s > {_MACRO_STALE_SECONDS}s — macro factors are last-known, treat with care")
        missing.append("macro_snapshot_stale")
        for k, label in (("dxy", "DXY"), ("tnx", "10Y"), ("vix", "VIX"),
                         ("jpy", "JPY"), ("oil", "Oil")):
            v = macro.get(k); chg = macro.get(f"{k}_chg_pct")
            if v is not None and chg is not None:
                parts.append(f"  {label}={v:.2f} ({chg:+.2f}%)")
    else:
        for k, label in (("dxy", "DXY"), ("tnx", "10Y yield"), ("vix", "VIX"),
                         ("jpy", "JPY"), ("oil", "Oil")):
            v = macro.get(k); chg = macro.get(f"{k}_chg_pct")
            if v is not None and chg is not None:
                arrow = "↑" if chg > 0 else ("↓" if chg < 0 else "→")
                parts.append(f"  {label}={v:.2f} ({chg:+.2f}% {arrow})")
    parts.append("")

    # 5. Session + time
    parts.append("SESSION + TIME:")
    parts.append(
        f"  session={session}  utc={now.strftime('%H:%M')}  "
        f"day={now.strftime('%a')}"
    )
    parts.append("")

    # 6. News window (Step 4) — feeds the C1 axis veto.
    from src.news_calendar import time_to_next_event, time_since_last_event
    nxt = time_to_next_event(now)
    last = time_since_last_event(now)
    parts.append("NEWS:")
    if nxt is None and last is None:
        parts.append("  (no calendar entries — running without news veto)")
    else:
        if nxt is None:
            parts.append("  next_event=(none scheduled)")
        else:
            parts.append(
                f"  next_event={nxt['event']!r} impact={nxt['impact']} "
                f"in {nxt['minutes']} min"
            )
        if last is None:
            parts.append("  last_event=(none in history)")
        else:
            parts.append(
                f"  last_event={last['event']!r} impact={last['impact']} "
                f"({last['minutes']} min ago)"
            )
    parts.append("")

    # 7. Recent precedent
    parts.append("RECENT PRECEDENT (XAUUSD, last 12h):")
    if precedent["trades_today"] == 0:
        parts.append("  (no recent trades)")
    else:
        parts.append(
            f"  trades={precedent['trades_today']}  "
            f"last_close={precedent['last_close_reason']!r} ({precedent['last_close_side']}, "
            f"{precedent['minutes_since_last_close']}min ago)  "
            f"consecutive_sl_hits_at_head={precedent['consecutive_losses']}"
        )
    parts.append("")

    parts.append("Output JSON only.")
    return "\n".join(parts), missing


def evaluate_signal(
    signal: dict,
    conn: sqlite3.Connection,
    ai_client: Any,
    *,
    symbol: str = "XAUUSD",
) -> dict:
    """Synchronous evaluator call. Returns the evaluation dict ready to be
    written into the action's payload_json under the 'evaluation' key.

    `ai_client` is duck-typed: must expose `.chat(system_prompt, user_text)
    -> str` returning JSON-shaped text. The orchestrator wraps the
    project's existing AIClient so the same model+config is reused.
    Errors are caught and rendered as a low-confidence stub so the
    dashboard can still display *something* when the LLM call fails.
    """
    try:
        user_input, missing = build_evaluator_input(signal, conn, symbol)
        raw = ai_client.chat(EVALUATOR_SYSTEM_PROMPT, user_input)
    except Exception as e:
        log.exception("evaluate_signal: input/LLM call failed")
        return _stub_evaluation(error=f"input_or_llm_error: {e}")

    parsed = _parse_evaluator_response(raw)
    if parsed is None:
        return _stub_evaluation(error="unparseable_llm_response", raw=raw[:300])

    parsed.setdefault("missing", [])
    for m in missing:
        if m not in parsed["missing"]:
            parsed["missing"].append(m)
    parsed["data_quality"] = "reduced" if missing else parsed.get("data_quality", "full")
    if missing and parsed.get("score", 0) > 70:
        parsed["score"] = 70
        parsed["verdict"] = _verdict_from_score(70)
    parsed["evaluated_at"] = datetime.now(timezone.utc).isoformat()
    return parsed


def _verdict_from_score(score: int) -> str:
    if score >= 80:
        return "strong"
    if score >= 60:
        return "moderate"
    if score >= 40:
        return "weak"
    return "avoid"


def _stub_evaluation(*, error: str, raw: str = "") -> dict:
    return {
        "score": 0,
        "verdict": "unavailable",
        "key_factor": f"evaluator failed: {error}",
        "summary": "Directional-bias evaluation could not be produced. Trade proceeds based on triage+interpreter only.",
        "factors": {},
        "missing": ["evaluator_response"],
        "data_quality": "reduced",
        "raw_excerpt": raw,
        "evaluated_at": datetime.now(timezone.utc).isoformat(),
    }


def _parse_evaluator_response(raw: str) -> dict | None:
    """Tolerant JSON extraction (markdown fences, preambles)."""
    if not raw:
        return None
    if "```" in raw:
        start = raw.find("```")
        end = raw.find("```", start + 3)
        if end > start:
            inner = raw[start + 3:end].strip()
            if inner.startswith("json"):
                inner = inner[4:].lstrip()
            raw = inner
    obj_start = raw.find("{")
    if obj_start < 0:
        return None
    depth = 0
    obj_end = -1
    for i in range(obj_start, len(raw)):
        c = raw[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                obj_end = i
                break
    if obj_end < 0:
        return None
    try:
        parsed = json.loads(raw[obj_start:obj_end + 1])
    except (TypeError, ValueError):
        return None
    if not isinstance(parsed, dict):
        return None
    parsed.setdefault("score", 0)
    parsed.setdefault("factors", {})
    parsed.setdefault("missing", [])
    if "verdict" not in parsed:
        parsed["verdict"] = _verdict_from_score(int(parsed.get("score", 0)))
    return parsed
