"""CFTC Commitments of Traders (COT) feed for the gold futures market.

CFTC publishes the Disaggregated COT every Friday afternoon ET with data
from the prior Tuesday. We pull the **last 52 weekly reports** for COMEX
gold (market code 088691), extract managed-money long/short positions,
compute the **net position** and its **percentile within the rolling
52-week window**. Both numbers go into the feature store:

  - `cot_managed_money`         -> net long contracts (long − short)
  - `cot_extremes_percentile`   -> 0-100, where the latest net sits

Percentile is the swing/position trader's key read: managed-money net
longs at the 85th percentile are stretched; net shorts at the 15th
percentile = bearish extreme, often a contrarian buy setup.

Source: CFTC Public Reporting API (Socrata)
  - Endpoint: https://publicreporting.cftc.gov/resource/72hh-3qpy.json
  - Dataset: Disaggregated Futures Only Reports
  - No API key required; rate limit generous (we pull weekly, 52 rows max).

Failure semantics: the function NEVER raises. Network errors, HTTP
non-200, malformed JSON, and parsing failures all return None; the
loop logs and tries again next cycle. The feature store keeps its
last-known value, so a brief CFTC outage doesn't reset the percentile.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)

# CFTC market code for COMEX gold futures (88691) on the disaggregated
# report. The Socrata dataset stores this in `cftc_contract_market_code`.
# Hardcoded because it's stable for decades — if it ever changes, that's
# an investigation, not a config edit.
_GOLD_MARKET_CODE = "088691"

# Disaggregated Futures Only — the report shape that splits managed
# money from producer/merchant from swap dealers. The "futures + options"
# variant exists too but options open-interest gets noisy at expiry.
_ENDPOINT = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"

# Rolling window used for the percentile rank. 52 weeks ≈ 1 year, which
# captures one full cyclical regime for gold positioning without going
# so far back that structural shifts (e.g. pre-2020 vs post-2020) wash
# out the signal.
_PERCENTILE_WINDOW = 52


def _fetch_raw_sync() -> list[dict] | None:
    """One synchronous HTTP GET. Returns the JSON list or None on any
    failure. Wrapped in asyncio.to_thread by the caller.

    Uses urllib from stdlib (no new dep). Timeout 15s — CFTC's public
    endpoint is reliable but occasionally slow; longer than the 10s
    used for FRED because the response is a larger array.
    """
    params = {
        "$where": f"cftc_contract_market_code='{_GOLD_MARKET_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(_PERCENTILE_WINDOW),
    }
    url = f"{_ENDPOINT}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "copytrades-cot/1.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8")
    except Exception as e:  # noqa: BLE001 — network/IO
        log.warning("cot: fetch failed: %s", e)
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as e:
        log.warning("cot: JSON parse failed: %s", e)
        return None
    if not isinstance(data, list) or not data:
        log.warning("cot: empty/non-list response")
        return None
    return data


def _to_int(row: dict, key: str) -> int | None:
    """Socrata returns numeric strings. Parse defensively — a single
    malformed row shouldn't poison the series."""
    v = row.get(key)
    if v is None:
        return None
    try:
        # Some columns come as floats with .00 suffix; int(float(...))
        # is the robust path.
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _percentile_rank(values: list[int], latest: int) -> float | None:
    """Return the percentile rank (0-100) of `latest` within `values`.

    Uses the "strictly less than" definition: percentile = 100 × (count
    strictly below) / (n - 1). At n=52 this gives sensible spread for
    the extremes we care about (top decile = stretched longs; bottom
    decile = capitulation).

    Returns None when n < 4 (not enough history to be meaningful).
    """
    if not values or len(values) < 4:
        return None
    below = sum(1 for v in values if v < latest)
    return round(100.0 * below / (len(values) - 1), 1)


def parse_cot_rows(rows: list[dict]) -> dict[str, Any] | None:
    """Pure-function parser: rows -> feature dict. Exposed for tests.

    Expects `rows` ordered newest-first (matches the Socrata response).
    Returns None when the latest row can't yield a net position (the
    columns are missing or unparseable).
    """
    if not rows:
        return None
    latest = rows[0]
    longs = _to_int(latest, "m_money_positions_long_all")
    shorts = _to_int(latest, "m_money_positions_short_all")
    if longs is None or shorts is None:
        log.warning("cot: latest row missing managed-money positions")
        return None
    net_latest = longs - shorts
    # Build the series for percentile rank.
    nets: list[int] = []
    for r in rows:
        L = _to_int(r, "m_money_positions_long_all")
        S = _to_int(r, "m_money_positions_short_all")
        if L is not None and S is not None:
            nets.append(L - S)
    pct = _percentile_rank(nets, net_latest)
    return {
        "cot_managed_money": net_latest,
        "cot_managed_money_long": longs,
        "cot_managed_money_short": shorts,
        "cot_extremes_percentile": pct,
        "cot_report_date": latest.get("report_date_as_yyyy_mm_dd"),
        "cot_window_size": len(nets),
        "cot_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_cot() -> dict[str, Any] | None:
    """Async wrapper: fetch + parse. Returns the feature dict or None.

    Empty `latest` net or insufficient history both return None at the
    parser layer; this function just adds the asyncio.to_thread hop and
    the network-failure path.
    """
    import asyncio
    rows = await asyncio.to_thread(_fetch_raw_sync)
    if rows is None:
        return None
    return parse_cot_rows(rows)
