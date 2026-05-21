"""Download and parse the ForexFactory weekly economic calendar.

The calendar feeds the evaluator's catalyst sub-scorer via
`src.news_calendar.time_to_next_event` / `_since_last_event`. Without
this feed, every evaluation runs with `news_window` absent and the
catalyst axis is a silent zero — the operator loses the "skip trades
during NFP" veto.

Source: https://nfs.faireconomy.media/ff_calendar_thisweek.xml
  - free, no auth, refreshes constantly
  - 1-week rolling window
  - dates in US Eastern (EST in winter / EDT in summer); we convert
    to UTC because the rest of the project lives in UTC ISO-8601

Output shape (matches what `news_calendar._load_calendar` expects):

    {
      "events": [
        {"event": "Federal Funds Rate", "impact": "high",
         "ts": "2026-05-21T18:00:00+00:00"},
        ...
      ]
    }

Failure semantics: NEVER raises. Returns None on any HTTP / parse /
timezone failure; the loop logs and keeps the previous file in place.
"""
from __future__ import annotations

import logging
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from typing import Any

from src.feeds._http import SSL_CONTEXT

log = logging.getLogger(__name__)


_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.xml"

# Impact mapping. ForexFactory uses "High" / "Medium" / "Low" / "Holiday".
# We collapse to our 3-level taxonomy; Holidays are dropped (they're
# market-closed days, not news events the evaluator should veto on).
_IMPACT_MAP = {
    "high": "high",
    "medium": "medium",
    "low": "low",
}


def _us_eastern_offset_hours(d: date) -> int:
    """Return UTC offset hours for America/New_York on date d.

    -4 in EDT (mid-March → early November), -5 in EST otherwise.

    Done by hand instead of `zoneinfo.ZoneInfo("America/New_York")`
    because zoneinfo on Windows requires the `tzdata` package, which
    we'd otherwise have to add as a runtime dep just for this one call.
    DST rules below are stable since 2007; the next change requires a
    code edit (acceptable for a 4-line table).
    """
    year = d.year
    march_first = date(year, 3, 1)
    march_first_dow = march_first.weekday()  # Mon=0 .. Sun=6
    days_to_first_sunday = (6 - march_first_dow) % 7
    dst_start = date(year, 3, 1 + days_to_first_sunday + 7)  # 2nd Sunday in March
    nov_first = date(year, 11, 1)
    nov_first_dow = nov_first.weekday()
    days_to_first_sunday_nov = (6 - nov_first_dow) % 7
    dst_end = date(year, 11, 1 + days_to_first_sunday_nov)   # 1st Sunday in November
    if dst_start <= d < dst_end:
        return -4
    return -5


def _parse_ff_time(time_str: str) -> tuple[int, int] | None:
    """Parse a ForexFactory time string (e.g. "2:00pm", "12:30am",
    "All Day", "Tentative") into a (hour, minute) 24h tuple.

    Returns None when the time isn't a concrete clock value — those
    rows are skipped because we can't anchor them to a UTC instant.
    """
    s = (time_str or "").strip().lower()
    if not s or s in ("all day", "tentative", "day 1", "day 2"):
        return None
    is_pm = s.endswith("pm")
    is_am = s.endswith("am")
    if not (is_pm or is_am):
        return None
    core = s[:-2].strip()
    parts = core.split(":")
    if len(parts) not in (1, 2):
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) == 2 else 0
    except ValueError:
        return None
    if not (0 <= hour <= 12) or not (0 <= minute < 60):
        return None
    # Convert 12h -> 24h.
    if is_am:
        if hour == 12:
            hour = 0
    else:  # pm
        if hour != 12:
            hour += 12
    return hour, minute


def _to_utc(date_str: str, time_str: str) -> str | None:
    """Combine ForexFactory date (MM-DD-YYYY) + time strings into an
    ISO-8601 UTC string. Returns None when the row can't be anchored
    (vague time, malformed date, etc.)."""
    if not date_str or not time_str:
        return None
    try:
        month, day, year = date_str.strip().split("-")
        d = date(int(year), int(month), int(day))
    except (ValueError, AttributeError):
        return None
    hm = _parse_ff_time(time_str)
    if hm is None:
        return None
    hour, minute = hm
    offset = _us_eastern_offset_hours(d)
    # Wallclock in ET; subtract the negative offset to get UTC.
    et = datetime(d.year, d.month, d.day, hour, minute,
                  tzinfo=timezone(timedelta(hours=offset)))
    return et.astimezone(timezone.utc).isoformat()


def parse_ff_xml(xml_text: str) -> dict[str, Any]:
    """Parse ForexFactory weekly XML to our calendar JSON shape.

    Skips rows with non-concrete times (All Day / Tentative) and
    'low' impact (the evaluator only acts on high/medium). Holidays
    are skipped — they're market state, not events.
    """
    events: list[dict] = []
    root = ET.fromstring(xml_text)
    for ev in root.iter("event"):
        title = (ev.findtext("title") or "").strip()
        impact_raw = (ev.findtext("impact") or "").strip().lower()
        date_str = (ev.findtext("date") or "").strip()
        time_str = (ev.findtext("time") or "").strip()
        country = (ev.findtext("country") or "").strip()
        if not title:
            continue
        impact = _IMPACT_MAP.get(impact_raw)
        if impact not in ("high", "medium"):
            continue
        ts_iso = _to_utc(date_str, time_str)
        if ts_iso is None:
            continue
        events.append({
            "event": f"{country} {title}" if country else title,
            "impact": impact,
            "ts": ts_iso,
        })
    return {"events": events}


def _fetch_sync() -> str | None:
    """One synchronous GET. Wrapped in asyncio.to_thread by the caller."""
    try:
        req = urllib.request.Request(
            _URL,
            headers={"User-Agent": "copytrades-calendar/1.0"},
        )
        with urllib.request.urlopen(req, timeout=20, context=SSL_CONTEXT) as resp:
            return resp.read().decode("utf-8", errors="replace")
    except Exception as e:  # noqa: BLE001
        log.warning("calendar_fetch: HTTP failed: %s", e)
        return None


async def fetch_economic_calendar() -> dict[str, Any] | None:
    """Async wrapper: fetch + parse. Returns the calendar dict or None.

    Never raises — every failure path returns None and lets the caller
    keep the previous calendar in place.
    """
    import asyncio
    xml_text = await asyncio.to_thread(_fetch_sync)
    if not xml_text:
        return None
    try:
        return parse_ff_xml(xml_text)
    except ET.ParseError as e:
        log.warning("calendar_fetch: XML parse failed: %s", e)
        return None
    except Exception as e:  # noqa: BLE001
        log.warning("calendar_fetch: unexpected parse failure: %s", e)
        return None
