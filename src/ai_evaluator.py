"""Signal-quality evaluator for XAUUSD OPEN actions.

Runs ASYNCHRONOUSLY after the orchestrator inserts an OPEN action — i.e.,
the action is already 'pending' / on its way to execution by the time the
evaluator returns its score. This is by design: the evaluator is purely
informational for the dashboard / forensic audit, never gating execution.

Inputs (all read from the live DB at evaluation time):
  - The signal payload (entry_low/entry_high/sl/tps/side/comment)
  - Current market price + age (from settings, written by /market/price)
  - Multi-timeframe OHLC + ATR snapshot (from settings, written by
    /market/snapshot — may be stale or absent; evaluator runs reduced)
  - Channel's recent signal outcomes (computed from positions table)

Output: a dict suitable for json.dumps that gets written back into the
action's payload_json under the 'evaluation' key. Shape:

    {
      "score": 72,                              # 0-100
      "verdict": "moderate",                    # strong | moderate | weak | avoid
      "key_factor": "counter-trend on 1H ...",  # 1-line dominant reason
      "summary": "Acceptable R:R but ...",      # 2-3 sentence rationale
      "factors": {                              # per-axis breakdown
         "rr_ratio": "...",
         "premise": "...",
         "volatility_fit": "...",
         "trend_alignment": "...",
         "channel_recent": "..."
      },
      "data_quality": "full" | "reduced",       # 'reduced' = snapshot stale/missing
      "missing": ["m15_snapshot", ...],         # what was unavailable
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
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# Freshness threshold: snapshot older than this is treated as stale and the
# evaluator falls back to reduced context with an explicit note.
_SNAPSHOT_STALE_SECONDS = 120


EVALUATOR_SYSTEM_PROMPT = """You are an expert XAUUSD (gold) signal-quality evaluator. You read a structured OPEN signal plus current market context and produce a 0-100 conviction score. Your output is informational ONLY — it does NOT gate trade execution. The trade has already been queued by the time you score it. Your job is to give the operator an honest read of how strongly the available evidence supports the signal.

INPUTS YOU WILL RECEIVE:
  1. SIGNAL: side, entry_low, entry_high, sl, tps, comment, computed R:R
  2. CURRENT MARKET: bid/ask/mid (with age) — may be stale or missing
  3. MULTI-TIMEFRAME OHLC: m15, h1, h4 with high/low/close + ATR(14) — MAY BE STALE OR MISSING
  4. CHANNEL RECENT: last N signals' outcomes (W/L), simple win rate
  5. POSITION CONTEXT: open positions (if any), last closed within 24h

EVALUATION FRAMEWORK — judge along five axes:

  A. R:R RATIO
     Compute reward (TP3 - mid for BUY, mid - TP3 for SELL) vs risk (mid - SL for BUY, SL - mid for SELL).
     1:1 is poor. 1:1.5 is acceptable. 1:2.5+ is strong. Below 1:1 is "avoid" territory.

  B. PREMISE FRESHNESS
     Where is current price relative to entry zone? Options:
       in-zone (best — channel premise valid)
       chase-eligible (price past entry on favorable side, still <50% of move taken)
       chased-too-far (price past entry, >50% of move already gone — diminished edge)
       wrong-side (price past entry on UNFAVORABLE side — premise broken)
     Wrong-side ALONE is not auto-fail — comment on it.

  C. VOLATILITY FIT
     SL distance vs current 1H ATR. A signal with SL=$5 on a day with ATR1H=$15 is too tight (will stop on noise). A signal with SL=$50 on ATR1H=$8 is wider than necessary (poor capital efficiency). Sweet spot: SL distance ≈ 0.8x to 2.5x ATR1H.

  D. TREND ALIGNMENT
     Compare signal direction to 1H and 4H direction (close vs open). Same direction = with-trend (better). Counter-trend signals are not always wrong but require stronger other evidence.

  E. CHANNEL RECENT
     Simple sanity check: if channel has lost the last 3-5 trades, recent edge is questionable. If on a winning streak, slight upweight. NEVER score >85 if last 3 channel trades all stopped out.

REDUCED-CONTEXT MODE:
If MULTI-TIMEFRAME OHLC is missing or stale:
  - Compute axes A and B normally (signal data + current price are usually fresh enough)
  - For axes C / D: write "data_quality_limited: snapshot stale" in that axis' string
  - Set "data_quality": "reduced" and add the missing element name to the "missing" list
  - Cap the final score at 70 (we can't credibly assert "strong" without volatility/trend data)

OUTPUT RULES:
  - Output a single JSON object. No prose outside JSON.
  - Score is integer 0-100. Verdict comes from the score band:
      80-100 strong | 60-79 moderate | 40-59 weak | 0-39 avoid
  - "key_factor" is a single short sentence — the dominant reason for the score.
  - "summary" is 1-3 sentences that walk through your reasoning.
  - "factors" has one short string per axis above (skip an axis if the data forces it; mention why).
  - "missing" is a list of what data was unavailable. Empty list if everything was present.
  - "data_quality" is "full" if you had all inputs; "reduced" otherwise.

Be honest. If the signal is poor, score it low. If it's good, score it high. Calibration matters more than agreement with the channel.
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

    snapshot_dict shape (when present):
        {"m15": {open, high, low, close, atr14},
         "h1":  {open, high, low, close, atr14},
         "h4":  {open, high, low, close, atr14}}

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


def _compute_channel_recent_outcomes(
    conn: sqlite3.Connection, n: int = 10
) -> dict:
    """Win/loss summary of the last N closed positions on this channel.

    Heuristic: any partial close => some profit was banked => count as win.
    Otherwise infer from close_reason (`tp` / `ai_close_full` => win;
    `mt5_close` / `mt5_not_found` => default to loss). Without closing
    prices stored on the position row this is a rough proxy; intentionally
    errs simple.
    """
    rows = conn.execute(
        "SELECT side, entry_price, sl, tp, original_volume, volume, "
        "       partial_close_count, close_reason "
        "FROM positions "
        "WHERE status='closed' AND closed_at IS NOT NULL "
        "ORDER BY closed_at DESC LIMIT ?",
        (n,),
    ).fetchall()
    if not rows:
        return {"count": 0, "wins": 0, "losses": 0, "win_rate": None}
    wins = 0
    losses = 0
    for r in rows:
        if (r["partial_close_count"] or 0) > 0:
            wins += 1
        elif r["close_reason"] in ("ai_close_full", "tp"):
            wins += 1
        else:
            losses += 1
    total = wins + losses
    return {
        "count": total,
        "wins": wins,
        "losses": losses,
        "win_rate": (wins / total) if total > 0 else None,
    }


def _compute_rr_ratio(
    side: str,
    entry_low: float,
    entry_high: float,
    sl: float,
    tps: list[float],
) -> dict:
    """Compute risk-to-reward at entry midpoint."""
    if not tps:
        return {"risk": 0.0, "reward_final": 0.0, "ratio_final": 0.0}
    entry = (entry_low + entry_high) / 2.0
    if side == "BUY":
        risk = max(0.01, entry - sl)
        reward_final = max(0.0, tps[-1] - entry)
    else:
        risk = max(0.01, sl - entry)
        reward_final = max(0.0, entry - tps[-1])
    return {
        "entry_mid": entry,
        "risk": risk,
        "reward_final": reward_final,
        "ratio_final": reward_final / risk,
    }


def build_evaluator_input(
    signal: dict, conn: sqlite3.Connection, symbol: str = "XAUUSD"
) -> tuple[str, list[str]]:
    """Compose the user-facing input string for the evaluator LLM call,
    plus a list of `missing` data-element names. The LLM receives the
    string verbatim alongside EVALUATOR_SYSTEM_PROMPT.
    """
    missing: list[str] = []

    rr = _compute_rr_ratio(
        signal["side"], signal["entry_low"], signal["entry_high"],
        signal["sl"], signal.get("tps") or [],
    )
    mid, mid_age = _get_market_mid(conn, symbol)
    snapshot, snap_age = _get_market_snapshot(conn, symbol)
    channel = _compute_channel_recent_outcomes(conn, 10)

    parts: list[str] = []
    parts.append("SIGNAL:")
    tps_str = ",".join(f"{t:g}" for t in (signal.get("tps") or []))
    parts.append(
        f"  side={signal['side']}  entry={signal['entry_low']:.2f}-{signal['entry_high']:.2f}  "
        f"sl={signal['sl']:.2f}  tps=[{tps_str}]"
    )
    if signal.get("comment"):
        parts.append(f"  comment={signal['comment']!r}")
    parts.append(
        f"  computed: entry_mid={rr.get('entry_mid', 0.0):.2f}  "
        f"risk={rr['risk']:.2f}/oz  reward_to_final_tp={rr['reward_final']:.2f}/oz  "
        f"R:R_final=1:{rr['ratio_final']:.2f}"
    )
    parts.append("")

    parts.append("CURRENT MARKET:")
    if mid is None:
        parts.append("  (no recent quote — EA not heartbeating; treat premise check with care)")
        missing.append("market_price")
    else:
        age_str = f"age={mid_age}s" if mid_age is not None else "age=unknown"
        parts.append(f"  mid={mid:.2f}  {age_str}")
    parts.append("")

    parts.append("MULTI-TIMEFRAME OHLC + ATR(14):")
    if snapshot is None:
        parts.append("  (no snapshot — EA hasn't pushed /market/snapshot yet)")
        missing.append("ohlc_snapshot")
    elif snap_age is not None and snap_age > _SNAPSHOT_STALE_SECONDS:
        parts.append(f"  STALE: age={snap_age}s > {_SNAPSHOT_STALE_SECONDS}s — DO NOT trust trend/volatility axes")
        missing.append("ohlc_snapshot_stale")
        parts.append(f"  raw: {snapshot}")
    else:
        for tf in ("m15", "h1", "h4"):
            row = snapshot.get(tf, {})
            parts.append(
                f"  {tf}: O={row.get('open', 0):.2f} H={row.get('high', 0):.2f} "
                f"L={row.get('low', 0):.2f} C={row.get('close', 0):.2f} "
                f"ATR14={row.get('atr14', 0):.2f}"
            )
    parts.append("")

    parts.append("CHANNEL RECENT (last 10 closed positions):")
    if channel["count"] == 0:
        parts.append("  (no history — first signals)")
        missing.append("channel_history")
    else:
        wr = channel["win_rate"]
        wr_str = f"{wr*100:.0f}%" if wr is not None else "n/a"
        parts.append(
            f"  wins={channel['wins']}  losses={channel['losses']}  "
            f"win_rate={wr_str}  (n={channel['count']})"
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
        "summary": "Signal-quality evaluation could not be produced. Trade proceeds based on triage+interpreter only.",
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
