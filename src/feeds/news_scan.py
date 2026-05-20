"""Geopolitical risk index via GDELT Project 2.0 article tone.

GDELT (https://www.gdeltproject.org/) is a public dataset of media
coverage worldwide, refreshed every 15 minutes. The free DOC 2.0 API
returns articles matching a query with metadata including **tone**
(positive-to-negative sentiment on a roughly −10 to +10 scale).

We construct a geopolitical risk score that gold actually cares about:

  - Query a fixed set of crisis-linked keywords (Iran-Israel, Russia-
    Ukraine, China-Taiwan, North Korea, oil shock, terror) over the
    last 4 hours.
  - Count articles + compute average tone (negative tones over a
    geopolitical query = crisis coverage).
  - Map to a 0-1 risk score where 0 = quiet news, 1 = saturation
    coverage of a negative-tone event.

The resulting feature `geopolitical_index` feeds the swing/position
evaluator's macro family. The evaluator can also see the headline list
via `geopolitical_recent_headlines` (top 5) for the rationale string.

Failure semantics: NEVER raises. GDELT is free, public, and
occasionally rate-limited or temporarily down — the loop logs and
retries next cycle. Feature store retains last-known on outage.
"""
from __future__ import annotations

import json
import logging
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


_ENDPOINT = "https://api.gdeltproject.org/api/v2/doc/doc"

# Keywords chosen for **gold-relevance**, not generic news. These map
# to the geopolitical risk premium gold prices in: military escalation,
# energy supply shocks, currency-system stress, and terror. Generic
# "Trump" / "election" coverage is noisy and excluded.
_QUERY = (
    "(Iran Israel) OR (Russia Ukraine) OR (China Taiwan) OR "
    "(North Korea) OR (oil shock) OR (terror attack) OR "
    "(Federal Reserve emergency) OR (banking crisis) OR (sanctions)"
)

# Article count where we saturate the index to 1.0. Calibrated from
# observation: 25 articles in 4h on these keywords is "elevated chatter";
# 50+ is wartime / crisis coverage. The score is monotonic but saturating
# so single bursts don't pin the dashboard at red forever.
_SATURATION_COUNT = 50


def _build_url() -> str:
    """Construct the GDELT DOC API URL. timespan=4h is the smallest
    reasonable window for "recent news" while still gathering enough
    articles for tone to be statistically meaningful."""
    params = {
        "query": _QUERY,
        "mode": "artlist",
        "format": "json",
        "maxrecords": "50",
        "timespan": "4h",
        "sort": "tonedesc",  # most negative-toned first
    }
    return f"{_ENDPOINT}?{urllib.parse.urlencode(params)}"


def _fetch_raw_sync() -> dict | None:
    """One synchronous GET. Returns the decoded JSON dict or None.

    GDELT occasionally returns an empty body on dry queries — that's
    different from a network error: we treat it as "no risk events"
    and let `parse_articles` produce a zero score.
    """
    try:
        req = urllib.request.Request(
            _build_url(),
            headers={"User-Agent": "copytrades-news/1.0"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        log.warning("news_scan: fetch failed: %s", e)
        return None
    if not raw.strip():
        # GDELT returns empty body on quiet queries — return a synthetic
        # "no articles" payload so the parser produces a zero-risk score.
        return {"articles": []}
    try:
        return json.loads(raw)
    except (TypeError, ValueError) as e:
        log.warning("news_scan: JSON parse failed: %s; raw=%r",
                    e, raw[:200])
        return None


def _saturating_score(count: int, avg_tone: float) -> float:
    """Map (article_count, avg_tone) -> [0, 1] geopolitical risk.

    Logic:
      - Count drives the upper bound (more coverage = more risk).
      - Tone modulates: very negative tone (< -3) amplifies; near-neutral
        tone (around 0) means coverage is procedural/factual and risk
        is lower than count alone would suggest.

    Calibrated, not learned. Replace with a regression once we have
    months of `geopolitical_index` vs realized gold moves to fit.
    """
    if count <= 0:
        return 0.0
    count_factor = min(1.0, count / _SATURATION_COUNT)
    # Tone in [-10, +10]. -3 or worse is "crisis-toned"; >= 0 is calm.
    tone_factor = max(0.0, min(1.0, (-avg_tone + 1.0) / 5.0))
    return round(count_factor * 0.6 + tone_factor * 0.4, 3)


def parse_articles(payload: dict) -> dict[str, Any] | None:
    """Pure parser: GDELT JSON payload -> feature dict. Exposed for tests.

    The GDELT response shape is `{"articles": [{"title": ..., "tone": ...,
    "url": ..., ...}, ...]}`. Missing/empty articles list returns the
    zero-risk feature dict — that IS valid data, not a failure.
    """
    if not isinstance(payload, dict):
        return None
    articles = payload.get("articles") or []
    if not isinstance(articles, list):
        return None
    tones: list[float] = []
    headlines: list[dict] = []
    for art in articles[:50]:
        if not isinstance(art, dict):
            continue
        tone_raw = art.get("tone")
        if tone_raw is not None:
            try:
                tones.append(float(tone_raw))
            except (TypeError, ValueError):
                pass
        title = (art.get("title") or "").strip()
        if title and len(headlines) < 5:
            headlines.append({
                "title": title,
                "url": art.get("url", ""),
                "tone": tone_raw,
                "source": art.get("domain", ""),
            })
    count = len(articles)
    avg_tone = sum(tones) / len(tones) if tones else 0.0
    score = _saturating_score(count, avg_tone)
    return {
        "geopolitical_index": score,
        "geopolitical_article_count": count,
        "geopolitical_avg_tone": round(avg_tone, 2),
        "geopolitical_recent_headlines": headlines,
        "geopolitical_fetched_at": datetime.now(timezone.utc).isoformat(),
    }


async def fetch_geopolitical_index() -> dict[str, Any] | None:
    """Async wrapper. Returns the feature dict or None on hard failure."""
    import asyncio
    payload = await asyncio.to_thread(_fetch_raw_sync)
    if payload is None:
        return None
    return parse_articles(payload)
