"""GLD ETF flow proxy via yfinance daily history.

True gold ETF flows (creations/redemptions of physical bars) are
published by SPDR / iShares with a 1-day lag and are not in any free
clean JSON feed I trust at retail scale. The next-best proxy is the
**dollar volume signed by daily return**: a day with strong price
strength on heavy volume = net buying pressure; weakness on heavy
volume = distribution. Aggregated over 5–30 days this tracks the
official reported physical flows reasonably well for directional bias.

We emit two features:

  - `etf_flows_gld`           -> latest day's signed $-volume in $millions
  - `etf_flows_30d_trend`     -> sum of signed $-volume over last 30 days

Both go into the feature store. The swing/position evaluator reads the
30d trend as a confirmation: trend > 0 + price up = accumulation
(constructive); trend < 0 + price up = late-cycle / distribution.

Cadence: daily. yfinance free; no key required. The bot polls every
hour but skips the actual fetch if the latest bar in the store is
already today's close.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


# 30-day window is the most-cited horizon in WGC/ETF-flow literature
# for separating "noise" from "trend" in gold ETF flows.
_TREND_WINDOW_DAYS = 30


def _fetch_history_sync() -> list[dict] | None:
    """Synchronous yfinance pull of GLD daily history for the trend
    window plus a small buffer for weekends/holidays. Returns a list of
    {"date": ISO, "close": float, "open": float, "volume": float} dicts
    newest-last, or None on any failure.
    """
    try:
        import yfinance as yf  # type: ignore[import-untyped]
        df = yf.download(
            tickers="GLD",
            period=f"{_TREND_WINDOW_DAYS + 10}d",
            interval="1d",
            progress=False,
            auto_adjust=True,
            threads=False,
        )
        if df is None or len(df) < 2:
            log.warning("etf_flows: GLD history <2 rows")
            return None
        # Flatten potential multi-index columns from yfinance.
        def _col(name: str) -> Any:
            c = df[name]
            return c.iloc[:, 0] if hasattr(c, "columns") else c
        closes = _col("Close")
        opens = _col("Open")
        volumes = _col("Volume")
        out: list[dict] = []
        for i in range(len(df)):
            try:
                out.append({
                    "date": df.index[i].isoformat(),
                    "close": float(closes.iloc[i]),
                    "open": float(opens.iloc[i]),
                    "volume": float(volumes.iloc[i]),
                })
            except (TypeError, ValueError):
                continue
        return out if len(out) >= 2 else None
    except Exception as e:  # noqa: BLE001
        log.warning("etf_flows: yfinance fetch failed: %s", e)
        return None


def compute_flow_features(history: list[dict]) -> dict[str, Any] | None:
    """Pure-function: history rows -> feature dict. Exposed for tests.

    Convention for signed $-volume:
        signed_flow_$M = sign(close - open) * close * volume / 1_000_000

    Sign uses open-to-close because intraday direction is what the flow
    represents — an inside bar that ended flat is correctly counted as
    zero, not as the prior bar's leftover.
    """
    if not history:
        return None
    signed_flows: list[float] = []
    for row in history:
        c = row["close"]
        o = row["open"]
        v = row["volume"]
        if c is None or o is None or v is None or v <= 0:
            continue
        direction = 1.0 if c > o else (-1.0 if c < o else 0.0)
        signed_flows.append(direction * c * v / 1_000_000.0)
    if not signed_flows:
        return None
    latest_flow = round(signed_flows[-1], 2)
    # Trend is the trailing-30d sum. Less than 30d of data returns the
    # partial sum — better than nothing for fresh deployments.
    window = signed_flows[-_TREND_WINDOW_DAYS:]
    trend = round(sum(window), 2)
    return {
        "etf_flows_gld": latest_flow,
        "etf_flows_30d_trend": trend,
        "etf_flows_window_days": len(window),
        "etf_flows_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_etf_flows() -> dict[str, Any] | None:
    """Async wrapper. Returns the feature dict or None."""
    import asyncio
    history = await asyncio.to_thread(_fetch_history_sync)
    if history is None:
        return None
    return compute_flow_features(history)
