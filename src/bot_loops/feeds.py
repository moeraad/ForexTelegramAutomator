"""Macro / fundamental data feed loops moved out of ``src/bot.py``.

Five loops here, grouped because they share the same shape (fetch → put
into ``feature_store`` / settings → sleep) and the same lifecycle (started
by ``post_init``, run forever, log on failure).

  - ``macro_feed_loop`` (60s) — DXY/10Y/VIX/JPY/oil/silver/copper via yfinance
  - ``cot_feed_loop`` (6h) — CFTC Commitments of Traders
  - ``etf_flows_feed_loop`` (1h) — GLD signed-volume flow proxies
  - ``calendar_feed_loop`` (6h) — ForexFactory economic calendar
  - ``news_scan_feed_loop`` (15m) — GDELT geopolitical risk index
"""
from __future__ import annotations

import asyncio
import json
import logging

from telegram.ext import Application


log = logging.getLogger("bot")


async def macro_feed_loop(app: Application):
    """Periodically fetch the macro snapshot (DXY/10Y/VIX/JPY/oil) and
    persist it under settings.macro_snapshot for the directional-bias
    evaluator (src/ai_evaluator.py::_get_macro_snapshot).

    Cadence: 60s. yfinance is rate-tolerant at this rate (5 small
    daily-bar downloads / minute), and the evaluator's stale threshold
    is 300s — so a single failed cycle is invisible to consumers.

    Failure modes (all silent at this layer; logged at WARNING):
      - yfinance not installed -> fetch_macro_snapshot returns None
      - Yahoo Finance down -> fetch_macro_snapshot returns None
      - Partial outage (some tickers fail) -> partial dict is still
        persisted; evaluator renders the present fields.
    """
    from src.macro import fetch_macro_snapshot
    from src.db import set_setting
    from src import feature_store
    conn = app.bot_data["conn"]
    while True:
        try:
            snap = await fetch_macro_snapshot()
            if snap is not None:
                # Legacy blob: kept for back-compat with the current
                # ai_evaluator (which reads macro_snapshot wholesale).
                set_setting(conn, "macro_snapshot", json.dumps(snap))
                set_setting(conn, "macro_snapshot_at", snap["fetched_at"])
                # Per-feature rows: the trading-style-aware evaluator
                # reads these by name. Mirror only the keys that map
                # 1:1 to trading_style.required_features so we don't
                # litter the table with unused names. Both value and
                # chg_pct are stored — the evaluator wants the % move
                # for direction-vs-regime calls.
                features: dict[str, dict] = {}
                for name in (
                    "dxy", "tnx", "vix", "silver", "copper", "usdjpy",
                    "sp500_futures", "real_yield", "tnx2",
                    "breakevens_5y5y",
                ):
                    if name in snap:
                        features[name] = {
                            "value": snap[name],
                            "chg_pct": snap.get(f"{name}_chg_pct"),
                        }
                # Also expose a name the trading_style spec uses literally:
                # required_features lists `real_yield_10y`. Keep both for
                # back-compat during the rollout.
                if "real_yield" in snap:
                    features["real_yield_10y"] = features["real_yield"]
                if features:
                    feature_store.put_many(conn, features)
                log.info(
                    "macro_feed: dxy=%s vix=%s tnx=%s real_yield=%s "
                    "(%d/%d tickers, %d features)",
                    snap.get("dxy"), snap.get("vix"), snap.get("tnx"),
                    snap.get("real_yield"),
                    sum(1 for k in ("dxy", "tnx", "vix", "jpy", "oil",
                                    "silver", "copper", "sp500_futures",
                                    "real_yield", "tnx2", "breakevens_5y5y")
                        if k in snap),
                    11,
                    len(features),
                )
            else:
                log.warning("macro_feed: no tickers fetched this cycle")
        except Exception as e:
            log.exception("macro_feed_loop error: %s", e)
        await asyncio.sleep(60.0)


async def cot_feed_loop(app: Application):
    """Pull CFTC Commitments of Traders for COMEX gold every 6 hours.

    Why 6h cadence: COT is released once a week (Friday afternoon ET).
    Polling weekly is fine in principle but the precise release time
    drifts; 6h means we never miss the new report by more than 6h.
    Off-cycle fetches are cheap (single JSON, ~50 rows) and produce no
    feature change when the report hasn't moved — feature_store updates
    the timestamp, which is fine.
    """
    from src import feature_store
    from src.feeds.cot import fetch_cot
    conn = app.bot_data["conn"]
    while True:
        try:
            snap = await fetch_cot()
            if snap is not None:
                feature_store.put_many(conn, snap)
                log.info(
                    "cot_feed: net=%s percentile=%s report_date=%s",
                    snap.get("cot_managed_money"),
                    snap.get("cot_extremes_percentile"),
                    snap.get("cot_report_date"),
                )
            else:
                log.warning("cot_feed: no data this cycle")
        except Exception as e:
            log.exception("cot_feed_loop error: %s", e)
        await asyncio.sleep(6 * 3600)


async def etf_flows_feed_loop(app: Application):
    """Pull GLD daily history and recompute signed-volume flow proxies
    every hour.

    Why 1h cadence: GLD's intraday volume only matters at the daily-close
    granularity for the signed-flow proxy. Polling more often than that
    is wasteful; less often risks missing the close-of-day update. 1h
    is the conservative middle.
    """
    from src import feature_store
    from src.feeds.etf_flows import fetch_etf_flows
    conn = app.bot_data["conn"]
    while True:
        try:
            snap = await fetch_etf_flows()
            if snap is not None:
                feature_store.put_many(conn, snap)
                log.info(
                    "etf_flows: latest=%s trend_30d=%s window_days=%s",
                    snap.get("etf_flows_gld"),
                    snap.get("etf_flows_30d_trend"),
                    snap.get("etf_flows_window_days"),
                )
            else:
                log.warning("etf_flows_feed: no data this cycle")
        except Exception as e:
            log.exception("etf_flows_feed_loop error: %s", e)
        await asyncio.sleep(3600.0)


async def calendar_feed_loop(app: Application):
    """Refresh the economic calendar (ForexFactory) every 6 hours.

    Why 6h: ForexFactory publishes a 1-week-rolling XML and refreshes
    forecast / actual columns intraday as numbers come in. 6h is
    enough to catch corrections without polling more than necessary.

    Output goes to `<db_dir>/economic_calendar.json` so
    `news_calendar._resolve_default_path()` picks it up in preference
    to the bundled seed. First fetch on startup so a freshly-installed
    stack gets a live calendar within seconds rather than 6 hours.
    """
    from pathlib import Path
    from src import config
    from src.feeds.calendar_fetch import fetch_economic_calendar
    while True:
        try:
            snap = await fetch_economic_calendar()
            if snap is None:
                log.warning("calendar_feed: no data this cycle")
            else:
                events = snap.get("events") or []
                db_path = getattr(config, "DB_PATH", None)
                if db_path:
                    out = Path(db_path).parent / "economic_calendar.json"
                    try:
                        out.write_text(
                            json.dumps({"events": events}, ensure_ascii=False),
                            encoding="utf-8",
                        )
                        log.info(
                            "calendar_feed: wrote %d events to %s",
                            len(events), out,
                        )
                        # Invalidate the news_calendar mtime cache so
                        # the next evaluator call sees the fresh file
                        # without waiting for stat() to land at a
                        # different mtime second.
                        try:
                            from src import news_calendar
                            news_calendar._cache.clear()
                        except Exception:
                            pass
                    except OSError as e:
                        log.warning("calendar_feed: write failed: %s", e)
                else:
                    log.warning("calendar_feed: DB_PATH unset; skipping write")
        except Exception as e:
            log.exception("calendar_feed_loop error: %s", e)
        await asyncio.sleep(6 * 3600)


async def news_scan_feed_loop(app: Application):
    """Refresh GDELT geopolitical risk index every 15 minutes.

    Why 15min cadence: GDELT's underlying dataset refreshes every 15
    minutes, so polling faster is wasted work. 15 minutes is also the
    smallest window where a sudden news burst is detectable but not
    rate-thrashing the public endpoint.

    Failure backoff: GDELT is rate-limited (HTTP 429) and intermittently
    times out at the SSL layer. Hammering it on the same 15-minute
    cadence while it's already throttling us just generates noise and
    keeps us throttled. On any failure we double the sleep up to a cap
    of 4 hours; one successful poll resets it.
    """
    from src import feature_store
    from src.feeds.news_scan import fetch_geopolitical_index
    base_sleep = 15 * 60
    max_sleep = 4 * 3600
    sleep_sec = base_sleep
    conn = app.bot_data["conn"]
    while True:
        ok = False
        try:
            snap = await fetch_geopolitical_index()
            if snap is not None:
                feature_store.put_many(conn, snap)
                log.info(
                    "news_scan: index=%s articles=%s avg_tone=%s",
                    snap.get("geopolitical_index"),
                    snap.get("geopolitical_article_count"),
                    snap.get("geopolitical_avg_tone"),
                )
                ok = True
            else:
                log.warning("news_scan_feed: no data this cycle")
        except Exception as e:
            log.exception("news_scan_feed_loop error: %s", e)
        if ok:
            sleep_sec = base_sleep
        else:
            sleep_sec = min(max_sleep, sleep_sec * 2)
            log.info("news_scan_feed: backing off, next attempt in %ss",
                     sleep_sec)
        await asyncio.sleep(sleep_sec)
