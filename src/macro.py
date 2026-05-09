"""Macro cross-asset feed for the directional-bias evaluator (Step 2).

Pulls DXY (US Dollar Index), US 10Y nominal yield, VIX, JPY, and oil
(WTI) values + intraday % change from Yahoo Finance via yfinance. Output
is consumed by `src/ai_evaluator.py::_get_macro_snapshot` after the bot's
`macro_feed_loop` writes it to the `settings` table.

Why these tickers (gold-specific):
  - DXY (^DXY / DX-Y.NYB): gold is dollar-inverse (the strongest
    short-horizon macro driver for XAUUSD).
  - 10Y (^TNX): yield-inverse — gold pays no yield, so rising yields
    are a structural headwind.
  - VIX (^VIX): risk-off regime support (gold benefits in flight-to-safety).
  - JPY (JPY=X): co-haven; the *direction* of JPY tells us whether a
    gold rally is haven-driven or industrial/inflation-driven.
  - Oil (CL=F): inflation proxy; sustained oil rally supports gold via
    inflation expectations.

Design:
  - Per-ticker fetches (5 small downloads) instead of multi-ticker
    (one big call with multi-index parsing). Simpler error handling, and
    a single bad symbol doesn't poison the whole snapshot.
  - All fetches run concurrently via asyncio.to_thread + asyncio.gather.
    Total wall time on a healthy network is 1-2 seconds.
  - yfinance is a sync HTTP library; wrapping in to_thread keeps the
    bot's asyncio event loop responsive.
  - The function never raises — any exception (network, yfinance internal,
    missing ticker) is caught and the field is omitted from the result.
    A snapshot with 4/5 fields is more useful than no snapshot at all.
  - chg_pct is computed from the LAST TWO daily closes returned by
    yfinance. On weekends the "today" close is Friday's; chg_pct is
    Friday vs Thursday — still meaningful for the evaluator.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# Ticker map. Key is the short label used in the snapshot dict; value is
# the yfinance symbol. DX-Y.NYB is the canonical free-feed DXY ticker —
# Yahoo also serves "^DXY" but it's intermittently unavailable.
_TICKERS: dict[str, str] = {
    "dxy": "DX-Y.NYB",
    "tnx": "^TNX",
    "vix": "^VIX",
    "jpy": "JPY=X",
    "oil": "CL=F",
}


def _fetch_one_sync(label: str, symbol: str) -> tuple[str, float, float] | None:
    """Sync per-ticker fetch. Returns (label, last_close, chg_pct_today)
    or None on any error. Wrapped in asyncio.to_thread by the caller.

    Uses period="5d" to be defensive against weekends / market holidays
    (a 2d period sometimes returns only 1 row when the prior session was
    a holiday). We always use the LAST two rows of the returned frame.
    """
    try:
        # Lazy import — keeps the rest of the module import-safe even if
        # yfinance is missing in dev environments. The bot logs and skips.
        import yfinance as yf  # type: ignore[import-untyped]
        df = yf.download(
            tickers=symbol,
            period="5d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,  # we're already in a worker thread
        )
        if df is None or len(df) < 2:
            log.warning("macro: %s returned <2 rows", symbol)
            return None
        # When a single ticker is downloaded, df has flat columns. When
        # multiple are downloaded yfinance returns a multi-index. Guard
        # against both shapes.
        close_col = df["Close"]
        if hasattr(close_col, "columns"):
            close_col = close_col.iloc[:, 0]
        # Last two daily closes — chg_pct is today vs yesterday.
        prev = float(close_col.iloc[-2])
        last = float(close_col.iloc[-1])
        if prev == 0:
            return None
        chg_pct = ((last - prev) / prev) * 100.0
        return label, last, chg_pct
    except Exception as e:
        log.warning("macro: %s fetch failed: %r", symbol, e)
        return None


async def fetch_macro_snapshot() -> dict | None:
    """Fetch all macro tickers concurrently and return the snapshot dict.

    Returns the same shape `_get_macro_snapshot` in `ai_evaluator.py` expects:
        {"dxy": float, "dxy_chg_pct": float,
         "tnx": float, "tnx_chg_pct": float,
         "vix": float, "vix_chg_pct": float,
         "jpy": float, "jpy_chg_pct": float,
         "oil": float, "oil_chg_pct": float,
         "fetched_at": ISO-8601 UTC}

    Returns None when EVERY ticker failed (full network outage / yfinance
    install missing). Returns a partial dict when SOME tickers succeeded —
    the evaluator's prompt iterates known keys, so missing ones simply
    don't render. A 3/5 partial is better than no snapshot at all.
    """
    tasks = [
        asyncio.to_thread(_fetch_one_sync, label, sym)
        for label, sym in _TICKERS.items()
    ]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    snap: dict[str, Any] = {}
    for r in results:
        if isinstance(r, BaseException) or r is None:
            continue
        label, last, chg = r
        snap[label] = round(last, 4)
        snap[f"{label}_chg_pct"] = round(chg, 3)

    if not snap:
        return None

    snap["fetched_at"] = datetime.now(timezone.utc).isoformat()
    return snap
