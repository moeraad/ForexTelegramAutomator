"""Input gathering for the evaluator's deterministic scorers.

Two layers:

  - `gather_inputs(conn)` — pull everything available into one flat dict
    keyed by feature name. The scorers read from this dict; the dict
    abstracts over the two backing storages (feature_store + the EA's
    market_snapshot blob).
  - `AxisScore` — the return shape every scorer produces. Score is a
    direction-relative scalar in [-1, +1], where +1 means "totally
    favorable for the proposed direction" and -1 means "totally against".
    The composer averages these with style-specific weights.

Direction relativity is critical: a BUY signal in an uptrend gets the
SAME positive trend_score as a SELL signal in a downtrend. The scorers
return "how favorable for what was asked," not "what direction the
market is in." This lets the composer combine families without sign-
chasing.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any

from src import feature_store


@dataclass(frozen=True)
class AxisScore:
    """Output of one sub-scorer.

    `score` is direction-relative in [-1, +1]. The composer multiplies
    by the trading-style's axis weight; weights sum to 1.0, so the
    weighted sum is also in [-1, +1] and maps linearly to a 0-100 verdict.

    `rationale` is a single short sentence — the dashboard rendering
    line for that axis. Keep it tight; the composer concatenates the
    full set into the evaluator output.

    `inputs_used` and `inputs_missing` enable data-quality bookkeeping:
    a scorer that ran with 2 of 4 inputs available should mark the other
    two so the composer can downgrade the overall data_quality flag.
    """
    score: float
    rationale: str
    inputs_used: list[str] = field(default_factory=list)
    inputs_missing: list[str] = field(default_factory=list)


def gather_inputs(
    conn: sqlite3.Connection, symbol: str = "XAUUSD"
) -> dict[str, Any]:
    """Pull every available evaluator input into one flat dict.

    Sources, in priority order:
      1. `feature_store` rows — DXY, VIX, COT, ETF flows, geopolitical
         index, etc. Each `feature_<name>` becomes key `<name>` in the
         output.
      2. The EA's `market_snapshot_<SYMBOL>` settings blob — OHLC across
         m15/h1/h4/d1/d1_prev plus ATR, ADR20, ADX H1, recent closes.
         Surfaced under the top-level keys the scorers expect (`m15`,
         `h1`, `h4`, `d1`, `d1_prev`, `adr20`, `adx_h1`,
         `h1_recent_closes`).
      3. `market_<SYMBOL>_bid/ask` for the live mid price.

    Returns a dict; missing inputs are simply absent. Scorers must
    `.get(name)` and handle None without raising.
    """
    out: dict[str, Any] = {}

    # 1. Feature store: macro, COT, ETF, news.
    for name in feature_store.names_present(conn):
        out[name] = feature_store.get(conn, name)

    # 2. EA market snapshot — the OHLC + ATR blob lives outside the
    #    feature_store namespace (legacy compat). Unpack into the top-
    #    level keys the scorers expect, mirroring the shape state_summary
    #    surfaces.
    sym = symbol.upper()
    snap_rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            (f"market_snapshot_{sym}", f"market_snapshot_{sym}_at"),
        ).fetchall()
    }
    raw_snap = snap_rows.get(f"market_snapshot_{sym}")
    if raw_snap:
        try:
            snap = json.loads(raw_snap)
            for k in (
                "m15", "h1", "h4", "d1", "d1_prev", "adr20", "adx_h1",
                "h1_recent_closes",
            ):
                if k in snap:
                    out[k] = snap[k]
        except (TypeError, ValueError):
            pass
    snap_at = snap_rows.get(f"market_snapshot_{sym}_at")
    if snap_at:
        out["snapshot_at"] = snap_at

    # 3. Live mid price.
    price_rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?, ?)",
            (f"market_{sym}_bid", f"market_{sym}_ask", f"market_{sym}_at"),
        ).fetchall()
    }
    bid = price_rows.get(f"market_{sym}_bid")
    ask = price_rows.get(f"market_{sym}_ask")
    if bid is not None and ask is not None:
        try:
            out["market_mid"] = (float(bid) + float(ask)) / 2.0
        except (TypeError, ValueError):
            pass
    if f"market_{sym}_at" in price_rows:
        out["market_at"] = price_rows[f"market_{sym}_at"]

    # 4. News window — derived state, recomputed every call. The
    # GDELT-based feeds publish a 4-hour-tone-aggregated geopolitical
    # index, but the catalyst sub-scorer and regime classifier need the
    # *imminent-event* view (next high-impact release in N minutes).
    # That view changes by the minute as events approach, so caching it
    # in feature_store would always be stale. We call into news_calendar
    # directly here. Import is local to avoid pulling the calendar
    # module's parsing into the evaluator's import graph at startup.
    try:
        from src.news_calendar import time_since_last_event, time_to_next_event
        nxt = time_to_next_event()
        last = time_since_last_event()
        if nxt is not None or last is not None:
            out["news_window"] = {
                "next_event": nxt,
                "last_event": last,
            }
    except Exception:
        # Missing calendar file or parsing failure -> no news_window in
        # the dict. Catalyst sub-scorer treats absence as "clear", same
        # as before this fix landed.
        pass

    return out


def unwrap_macro_feature(value: Any) -> tuple[float | None, float | None]:
    """The macro feed writes each ticker as `{"value": float, "chg_pct":
    float}`. Scorers want both pieces — `value` for level reads, `chg_pct`
    for direction reads. Returns (value, chg_pct) with either None on
    missing/malformed.

    Also tolerates the bare-float shape used by some COT / news features
    (`geopolitical_index` is a plain float in [0, 1]) — those return
    (value, None).
    """
    if value is None:
        return None, None
    if isinstance(value, (int, float)):
        return float(value), None
    if isinstance(value, dict):
        v = value.get("value")
        c = value.get("chg_pct")
        try:
            v = float(v) if v is not None else None
        except (TypeError, ValueError):
            v = None
        try:
            c = float(c) if c is not None else None
        except (TypeError, ValueError):
            c = None
        return v, c
    return None, None


def clamp(value: float, lo: float = -1.0, hi: float = 1.0) -> float:
    """Clamp a scorer's working sum into the [-1, +1] band. The
    composer assumes inputs stay in range; without clamping a stack of
    +0.5s could overflow."""
    return max(lo, min(hi, value))
