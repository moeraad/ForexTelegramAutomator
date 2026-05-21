"""Macro cross-asset feed for the directional-bias evaluator (Step 2).

Pulls DXY (US Dollar Index), US 10Y nominal yield, US 10Y REAL yield, VIX,
JPY, and oil (WTI) values + intraday % change. yfinance for everything
except real yields, which come from FRED's public CSV endpoint (DFII10:
10-Year Treasury Inflation-Indexed Security, Constant Maturity). Output
is consumed by `src/ai_evaluator.py::_get_macro_snapshot` after the bot's
`macro_feed_loop` writes it to the `settings` table.

Why these tickers (gold-specific):
  - DXY (^DXY / DX-Y.NYB): gold is dollar-inverse (the strongest
    short-horizon macro driver for XAUUSD).
  - 10Y nominal (^TNX): yield-inverse — gold pays no yield, so rising
    nominal yields are a structural headwind.
  - 10Y REAL yield (FRED DFII10): empirically the strongest macro
    predictor of gold (R^2 ~2-3x stronger than nominal on daily horizon).
    Gold has no carry, so opportunity cost vs real yields drives flow.
  - VIX (^VIX): risk-off regime support (gold benefits in flight-to-safety).
  - JPY (JPY=X): co-haven; the *direction* of JPY tells us whether a
    gold rally is haven-driven or industrial/inflation-driven. Still
    fetched but no longer rendered in the evaluator prompt (dropped
    2026-05-11). Kept for backfill / future use.
  - Oil (CL=F): inflation proxy. Same — fetched, not rendered.

Design:
  - Per-ticker fetches (5 yfinance + 1 FRED) instead of multi-ticker.
    Simpler error handling; a single bad symbol doesn't poison the
    whole snapshot.
  - All fetches run concurrently via asyncio.to_thread + asyncio.gather.
    Total wall time on a healthy network is 1-2 seconds.
  - The function never raises — any exception (network, yfinance internal,
    missing ticker, FRED CSV format change) is caught and the field is
    omitted from the result.
  - chg_pct is computed from the LAST TWO daily values. On weekends the
    "today" value is Friday's; chg_pct is Friday vs Thursday. FRED CSVs
    use "." for missing observations on weekends/holidays — those rows
    are skipped before computing chg_pct.
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
#
# Expanded 2026-05-20 for the trading-style-aware evaluator: silver and
# copper are inter-market confirmations for gold (G/S ratio extremes
# mean-revert; G/C decoupling flags regime shifts); USDJPY is a regime
# tag (yen flips correlation in risk-off); S&P futures are the risk-on/
# risk-off pulse. JPY=X kept under the legacy `jpy` label even though
# it's the same instrument — switching the label would break consumers
# reading the macro_snapshot blob.
_TICKERS: dict[str, str] = {
    "dxy": "DX-Y.NYB",
    "tnx": "^TNX",
    "vix": "^VIX",
    "jpy": "JPY=X",
    "oil": "CL=F",
    "silver": "SI=F",       # gold/silver ratio + inter-metal confirmation
    "copper": "HG=F",       # gold/copper monetary-vs-industrial split
    "usdjpy": "JPY=X",      # alias for evaluator clarity; same underlying
    "sp500_futures": "ES=F",  # risk-on/off pulse
}


# FRED series IDs for daily-frequency rates pulled via the public CSV
# endpoint (no API key required). Mapped to the feature-store names the
# trading_style spec lists.
_FRED_SERIES: dict[str, str] = {
    "real_yield": "DFII10",       # 10Y real yield (TIPS) — strongest gold driver
    "tnx2": "DGS2",               # 2Y nominal — front-end policy expectations
    "breakevens_5y5y": "T5YIFR",  # 5y5y forward inflation expectations
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


def _fetch_fred_csv_sync(
    label: str, series_id: str
) -> tuple[str, float, float] | None:
    """Sync fetch of a daily FRED series via the public CSV endpoint.

    Returns (label, last_value, chg_pct_today) or None on any error. No
    API key required — `fredgraph.csv` is a public endpoint. Skips rows
    where the value is "." (FRED's missing-observation marker for
    weekends and holidays). Uses urllib from stdlib so we don't add a
    new dependency just for one feed.
    """
    try:
        import urllib.request

        # Route through certifi's bundled CAs so the request works
        # under the PyInstaller-bundled exe + NSSM service context
        # where the OS cert store isn't reachable. Same fix applied
        # in src.feeds.cot and src.feeds.news_scan.
        from src.feeds._http import SSL_CONTEXT
        url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}"
        with urllib.request.urlopen(url, timeout=10, context=SSL_CONTEXT) as resp:
            data = resp.read().decode("utf-8")
        lines = [ln for ln in data.splitlines() if ln.strip()]
        if len(lines) < 3:
            log.warning("macro FRED %s: <2 data rows", series_id)
            return None
        values: list[float] = []
        for ln in lines[1:]:  # skip header
            parts = ln.split(",")
            if len(parts) != 2:
                continue
            v = parts[1].strip()
            if v in ("", "."):
                continue
            try:
                values.append(float(v))
            except ValueError:
                continue
        if len(values) < 2:
            log.warning("macro FRED %s: <2 numeric values after filter", series_id)
            return None
        prev = values[-2]
        last = values[-1]
        if prev == 0:
            return None
        chg_pct = ((last - prev) / prev) * 100.0
        return label, last, chg_pct
    except Exception as e:
        log.warning("macro FRED %s fetch failed: %r", series_id, e)
        return None


async def fetch_macro_snapshot() -> dict | None:
    """Fetch all macro tickers concurrently and return the snapshot dict.

    Returns the same shape `_get_macro_snapshot` in `ai_evaluator.py` expects:
        {"dxy": float, "dxy_chg_pct": float,
         "tnx": float, "tnx_chg_pct": float,
         "real_yield": float, "real_yield_chg_pct": float,
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
    # FRED series — fetched from a different source (public CSV endpoint)
    # but same return shape, so they join the gather and slot into the
    # same result-aggregation path. real_yield is the strongest macro
    # driver; tnx2 + breakevens give the Fed-expectations picture.
    for label, series_id in _FRED_SERIES.items():
        tasks.append(asyncio.to_thread(_fetch_fred_csv_sync, label, series_id))
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
