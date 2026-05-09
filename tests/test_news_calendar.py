"""Tests for src.news_calendar.

Hermetic — every test writes its own fixture JSON to tmp_path and passes
the path explicitly to the helpers. This avoids touching the production
data/economic_calendar.json and isolates the mtime cache between tests.
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path

from src.news_calendar import (
    _load_calendar,
    time_since_last_event,
    time_to_next_event,
)


def _write(tmp_path: Path, events: list[dict]) -> Path:
    p = tmp_path / "cal.json"
    p.write_text(json.dumps({"events": events}), encoding="utf-8")
    return p


# ---- _load_calendar ---------------------------------------------------


def test_load_missing_file_returns_empty(tmp_path):
    """File not present -> empty list, no exception."""
    assert _load_calendar(tmp_path / "does-not-exist.json") == []


def test_load_bad_json_returns_empty(tmp_path):
    p = tmp_path / "bad.json"
    p.write_text("not valid json {{{", encoding="utf-8")
    assert _load_calendar(p) == []


def test_load_drops_malformed_rows(tmp_path):
    """Rows missing event/ts/impact (or with unknown impact) are silently
    dropped — one bad row doesn't poison the rest."""
    p = _write(tmp_path, [
        {"event": "NFP", "ts": "2026-05-09T12:30:00+00:00", "impact": "high"},
        {"event": "X", "ts": "2026-05-09T13:00:00+00:00"},  # missing impact
        {"ts": "2026-05-09T14:00:00+00:00", "impact": "high"},  # missing event
        {"event": "Bad", "ts": "2026-05-09T15:00:00+00:00", "impact": "off"},  # bad impact
        {"event": "CPI", "ts": "2026-05-09T16:00:00+00:00", "impact": "medium"},
    ])
    rows = _load_calendar(p)
    assert len(rows) == 2
    assert rows[0]["event"] == "NFP"
    assert rows[1]["event"] == "CPI"


def test_load_uses_mtime_cache(tmp_path):
    """Repeated _load_calendar calls without file change return the same
    list object (cache hit). After a file edit + mtime bump, the cache
    invalidates and the new content is read."""
    p = _write(tmp_path, [
        {"event": "NFP", "ts": "2026-05-09T12:30:00+00:00", "impact": "high"},
    ])
    first = _load_calendar(p)
    second = _load_calendar(p)
    assert first is second  # cache hit returns the same list reference

    # Edit the file. mtime resolution on Windows can be 1s; sleep + touch.
    time.sleep(0.05)
    p.write_text(json.dumps({"events": [
        {"event": "FOMC", "ts": "2026-06-15T18:00:00+00:00", "impact": "high"},
    ]}), encoding="utf-8")
    # Force mtime change in case the resolution swallowed our sleep.
    new_mtime = p.stat().st_mtime + 5
    import os
    os.utime(p, (new_mtime, new_mtime))

    third = _load_calendar(p)
    assert third is not first
    assert third[0]["event"] == "FOMC"


# ---- time_to_next_event -----------------------------------------------


def _now() -> datetime:
    return datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc)


def test_next_event_picks_earliest_future(tmp_path):
    p = _write(tmp_path, [
        {"event": "NFP",  "ts": "2026-05-09T12:30:00+00:00", "impact": "high"},
        {"event": "FOMC", "ts": "2026-05-09T18:00:00+00:00", "impact": "high"},
    ])
    res = time_to_next_event(_now(), path=p)
    assert res is not None
    assert res["event"] == "NFP"
    assert res["impact"] == "high"
    assert res["minutes"] == 30


def test_next_event_skips_past(tmp_path):
    p = _write(tmp_path, [
        {"event": "Past CPI", "ts": "2026-05-09T08:00:00+00:00", "impact": "high"},
        {"event": "ECB",      "ts": "2026-05-09T13:15:00+00:00", "impact": "medium"},
    ])
    res = time_to_next_event(_now(), path=p)
    assert res is not None
    assert res["event"] == "ECB"
    assert res["minutes"] == 75


def test_next_event_returns_none_when_no_future(tmp_path):
    p = _write(tmp_path, [
        {"event": "Past", "ts": "2026-05-09T08:00:00+00:00", "impact": "high"},
    ])
    assert time_to_next_event(_now(), path=p) is None


def test_next_event_ignores_low_impact(tmp_path):
    """C1 only acts on high+medium; low-impact rows are returned by the
    JSON loader but filtered out by the time-window helpers."""
    p = _write(tmp_path, [
        {"event": "Junk", "ts": "2026-05-09T12:15:00+00:00", "impact": "low"},
        {"event": "FOMC", "ts": "2026-05-09T16:00:00+00:00", "impact": "high"},
    ])
    res = time_to_next_event(_now(), path=p)
    assert res["event"] == "FOMC"


# ---- time_since_last_event --------------------------------------------


def test_last_event_picks_most_recent_past(tmp_path):
    p = _write(tmp_path, [
        {"event": "Old CPI", "ts": "2026-05-08T12:30:00+00:00", "impact": "high"},
        {"event": "Powell",  "ts": "2026-05-09T11:30:00+00:00", "impact": "medium"},
        {"event": "Future",  "ts": "2026-05-09T14:00:00+00:00", "impact": "high"},
    ])
    res = time_since_last_event(_now(), path=p)
    assert res is not None
    assert res["event"] == "Powell"
    assert res["minutes"] == 30


def test_last_event_returns_none_when_no_past(tmp_path):
    p = _write(tmp_path, [
        {"event": "Future", "ts": "2026-05-09T14:00:00+00:00", "impact": "high"},
    ])
    assert time_since_last_event(_now(), path=p) is None
