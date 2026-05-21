"""Tests for the ForexFactory weekly-XML calendar fetcher.

Network-isolated: the pure-function parser is exercised against fixed
XML strings. The async wrapper is a thin urllib+to_thread shim and is
left unmocked here.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from src.feeds.calendar_fetch import (
    _parse_ff_time,
    _to_utc,
    _us_eastern_offset_hours,
    parse_ff_xml,
)


# ---- _us_eastern_offset_hours ---------------------------------------

def test_offset_in_january_is_est():
    assert _us_eastern_offset_hours(date(2026, 1, 15)) == -5


def test_offset_in_july_is_edt():
    assert _us_eastern_offset_hours(date(2026, 7, 4)) == -4


def test_offset_at_dst_start_2nd_sunday_march():
    """2026: 2nd Sunday in March is March 8. EDT starts that day."""
    assert _us_eastern_offset_hours(date(2026, 3, 7)) == -5
    assert _us_eastern_offset_hours(date(2026, 3, 8)) == -4


def test_offset_at_dst_end_1st_sunday_november():
    """2026: 1st Sunday in November is November 1. EST resumes."""
    assert _us_eastern_offset_hours(date(2026, 10, 31)) == -4
    assert _us_eastern_offset_hours(date(2026, 11, 1)) == -5


# ---- _parse_ff_time --------------------------------------------------

def test_parse_pm_time():
    assert _parse_ff_time("2:00pm") == (14, 0)


def test_parse_am_time():
    assert _parse_ff_time("8:30am") == (8, 30)


def test_parse_noon_and_midnight():
    """12am = 0:00, 12pm = 12:00 — common AM/PM landmine."""
    assert _parse_ff_time("12:00am") == (0, 0)
    assert _parse_ff_time("12:00pm") == (12, 0)


def test_parse_skips_all_day_and_tentative():
    assert _parse_ff_time("All Day") is None
    assert _parse_ff_time("Tentative") is None
    assert _parse_ff_time("") is None


def test_parse_rejects_bad_input():
    assert _parse_ff_time("not a time") is None
    assert _parse_ff_time("25:00pm") is None


# ---- _to_utc ----------------------------------------------------------

def test_to_utc_summer_event_uses_edt():
    """2:00pm EDT on 2026-07-04 = 18:00 UTC."""
    iso = _to_utc("07-04-2026", "2:00pm")
    assert iso is not None
    dt = datetime.fromisoformat(iso)
    assert dt == datetime(2026, 7, 4, 18, 0, tzinfo=timezone.utc)


def test_to_utc_winter_event_uses_est():
    """8:30am EST on 2026-01-15 = 13:30 UTC."""
    iso = _to_utc("01-15-2026", "8:30am")
    assert iso is not None
    dt = datetime.fromisoformat(iso)
    assert dt == datetime(2026, 1, 15, 13, 30, tzinfo=timezone.utc)


def test_to_utc_returns_none_for_vague_time():
    assert _to_utc("07-04-2026", "All Day") is None


def test_to_utc_returns_none_for_malformed_date():
    assert _to_utc("not-a-date", "2:00pm") is None


# ---- parse_ff_xml ----------------------------------------------------

def _xml(events_xml: str) -> str:
    return f"<weeklyevents>{events_xml}</weeklyevents>"


def test_parse_high_impact_event_included():
    xml = _xml("""
      <event>
        <title>Federal Funds Rate</title>
        <country>USD</country>
        <date>07-04-2026</date>
        <time>2:00pm</time>
        <impact>High</impact>
      </event>
    """)
    out = parse_ff_xml(xml)
    assert len(out["events"]) == 1
    e = out["events"][0]
    assert "Federal Funds Rate" in e["event"]
    assert e["impact"] == "high"
    # 2pm EDT in July -> 18:00 UTC.
    assert e["ts"].startswith("2026-07-04T18:00:00")


def test_parse_drops_low_impact():
    """Only high/medium events go into the file — the evaluator's
    catalyst veto only fires on those."""
    xml = _xml("""
      <event>
        <title>Mortgage Apps</title>
        <country>USD</country>
        <date>07-04-2026</date>
        <time>7:00am</time>
        <impact>Low</impact>
      </event>
    """)
    out = parse_ff_xml(xml)
    assert out["events"] == []


def test_parse_drops_holiday():
    """Holidays are market state, not news events. Don't surface them
    as veto triggers — closing the market is a different category."""
    xml = _xml("""
      <event>
        <title>Bank Holiday</title>
        <country>USD</country>
        <date>07-04-2026</date>
        <time>All Day</time>
        <impact>Holiday</impact>
      </event>
    """)
    out = parse_ff_xml(xml)
    assert out["events"] == []


def test_parse_drops_event_with_vague_time():
    """Tentative / All Day rows can't be anchored to a UTC instant —
    the evaluator's `minutes_to_event` math needs a concrete timestamp."""
    xml = _xml("""
      <event>
        <title>Some Speech</title>
        <country>USD</country>
        <date>07-04-2026</date>
        <time>Tentative</time>
        <impact>High</impact>
      </event>
    """)
    out = parse_ff_xml(xml)
    assert out["events"] == []


def test_parse_medium_impact_included():
    xml = _xml("""
      <event>
        <title>Trade Balance</title>
        <country>USD</country>
        <date>07-04-2026</date>
        <time>8:30am</time>
        <impact>Medium</impact>
      </event>
    """)
    out = parse_ff_xml(xml)
    assert len(out["events"]) == 1
    assert out["events"][0]["impact"] == "medium"


def test_parse_country_prefixed_in_event_name():
    """Disambiguates a "CPI" release from USD vs EUR. Operator reading
    the events list will want to see which currency it was."""
    xml = _xml("""
      <event>
        <title>CPI</title>
        <country>EUR</country>
        <date>07-04-2026</date>
        <time>10:00am</time>
        <impact>High</impact>
      </event>
    """)
    out = parse_ff_xml(xml)
    assert out["events"][0]["event"] == "EUR CPI"


def test_parse_malformed_xml_raises():
    """Caller (fetch_economic_calendar) catches ET.ParseError;
    pure-function path lets it bubble so the failure surfaces in tests."""
    import xml.etree.ElementTree as ET
    with pytest.raises(ET.ParseError):
        parse_ff_xml("not even close to xml")


def test_parse_empty_calendar_returns_empty_events():
    xml = "<weeklyevents></weeklyevents>"
    out = parse_ff_xml(xml)
    assert out == {"events": []}
