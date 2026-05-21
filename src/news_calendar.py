"""Economic-calendar reader for the directional-bias evaluator (Step 4).

Consumes `data/economic_calendar.json` (manually maintained — see file
header) and exposes two helpers the evaluator's prompt builder uses:

  - time_to_next_event(now=None) -> dict | None
        {"event": str, "impact": str, "minutes": int}
        None when no future event is in the file.
  - time_since_last_event(now=None) -> dict | None
        Same shape; None when no past event is in the file.

Filtering rules:
  - Only `impact in {"high", "medium"}` rows are returned. The C1 veto
    cap-at-50 only fires for `high`; medium is informational.
  - "Future" = ts > now; "past" = ts <= now. Boundary is now().

Cache:
  - JSON is parsed once and cached. The cache is invalidated when the
    file's mtime changes — so the operator can edit the calendar
    without restarting any process.
  - Loading is failure-tolerant: missing file, bad JSON, unexpected
    schema all log at WARNING and return None from the public helpers.
    The evaluator handles None as "no veto known" — same as a clean
    no-events-soon window.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Bundled seed location — read-only after PyInstaller packaging.
# Stays as the fallback when no stack-local writable copy exists yet
# (e.g. fresh install, calendar_feed_loop hasn't ticked).
_BUNDLED_PATH = Path(__file__).resolve().parent.parent / "data" / "economic_calendar.json"


def _resolve_default_path() -> Path:
    """Pick the file the reader uses by default.

    Preference order:
      1. Test override — when callers monkeypatch the module-level
         `_DEFAULT_PATH` to a fixture, honor that. Detected via `is not`
         identity against the bundled path so production code never
         confuses a real file with a test override.
      2. `<DB_PATH parent>/economic_calendar.json` — the writable stack-
         local copy that `calendar_feed_loop` refreshes daily.
      3. `_BUNDLED_PATH` — the read-only seed shipped in the PyInstaller
         exe. Used until the feed worker has populated the writable
         path at least once.

    Falling back to the bundled path even when missing is fine — the
    file-stat in `_load_calendar` will then log the missing-file warning
    once and the catalyst axis goes into reduced mode (no news veto).
    """
    if _DEFAULT_PATH is not _BUNDLED_PATH:
        return Path(_DEFAULT_PATH)
    try:
        from src import config
        db_path = getattr(config, "DB_PATH", None)
        if db_path:
            local = Path(db_path).parent / "economic_calendar.json"
            if local.exists():
                return local
    except Exception:
        pass
    return _BUNDLED_PATH


# Kept as a module-level alias for back-compat with callers that
# import _DEFAULT_PATH directly (tests monkeypatch this). The
# resolver function gets called lazily in _load_calendar when no
# explicit path is supplied so a delayed config.DB_PATH still wins.
_DEFAULT_PATH = _BUNDLED_PATH

# In-memory cache: keyed on the path string so multiple files (test
# fixtures + production) coexist without stomping each other. Value:
# (mtime_at_load, parsed_events_list).
_cache: dict[str, tuple[float, list[dict]]] = {}


def _parse_iso(s: str) -> datetime | None:
    """ISO-8601 UTC parse. Tolerates trailing 'Z'. Returns None on failure
    (the row is then silently dropped from the calendar)."""
    if not s:
        return None
    try:
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        ts = datetime.fromisoformat(s)
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=timezone.utc)
        return ts
    except (ValueError, TypeError):
        return None


def _load_calendar(path: Path | None = None) -> list[dict]:
    """Return the parsed events list, using the mtime-keyed cache. Empty
    list on any failure so callers don't need to special-case missing
    file vs empty file.

    When no path is supplied, resolves writable-first via
    `_resolve_default_path()` so stack-local refreshed copies win over
    the bundled seed.
    """
    p = (path or _resolve_default_path()).resolve()
    key = str(p)
    try:
        mtime = p.stat().st_mtime
    except OSError:
        # File missing — log once on first miss to avoid log spam each
        # call. Track via a sentinel in the cache.
        if _cache.get(key, (None, None))[0] != "missing":
            log.warning("news_calendar: file not found at %s — running without news veto", p)
            _cache[key] = ("missing", [])  # type: ignore[assignment]
        return []
    cached = _cache.get(key)
    if cached is not None and isinstance(cached[0], (int, float)) and cached[0] == mtime:
        return cached[1]
    try:
        raw = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("news_calendar: failed to load %s: %r", p, e)
        _cache[key] = (mtime, [])
        return []
    events = raw.get("events") if isinstance(raw, dict) else None
    if not isinstance(events, list):
        log.warning("news_calendar: %s has no `events` array", p)
        _cache[key] = (mtime, [])
        return []
    # Light-touch validation — silently drop rows that don't have the
    # three required fields so a bad row can't poison the rest.
    cleaned: list[dict] = []
    for row in events:
        if not isinstance(row, dict):
            continue
        ev = row.get("event"); ts = row.get("ts"); imp = row.get("impact")
        if not ev or not ts or imp not in ("high", "medium", "low"):
            continue
        cleaned.append(row)
    _cache[key] = (mtime, cleaned)
    return cleaned


def time_to_next_event(now: datetime | None = None,
                       path: Path | None = None) -> dict | None:
    """Earliest future event with impact in {high, medium}. Returns
    {"event": str, "impact": str, "minutes": int} or None when nothing
    upcoming is on the calendar.
    """
    now = now or datetime.now(timezone.utc)
    events = _load_calendar(path)
    candidates: list[tuple[datetime, dict]] = []
    for row in events:
        if row.get("impact") not in ("high", "medium"):
            continue
        ts = _parse_iso(row.get("ts"))
        if ts is None or ts <= now:
            continue
        candidates.append((ts, row))
    if not candidates:
        return None
    ts, row = min(candidates, key=lambda x: x[0])
    minutes = int((ts - now).total_seconds() / 60)
    return {"event": row["event"], "impact": row["impact"], "minutes": minutes}


def time_since_last_event(now: datetime | None = None,
                          path: Path | None = None) -> dict | None:
    """Most recent past event with impact in {high, medium}. Returns
    {"event": str, "impact": str, "minutes": int} or None.

    Useful for the C1 axis's post-event drift judgement: the first ~15
    minutes after a high-impact release is whipsaw; after that the move
    direction firms up.
    """
    now = now or datetime.now(timezone.utc)
    events = _load_calendar(path)
    candidates: list[tuple[datetime, dict]] = []
    for row in events:
        if row.get("impact") not in ("high", "medium"):
            continue
        ts = _parse_iso(row.get("ts"))
        if ts is None or ts > now:
            continue
        candidates.append((ts, row))
    if not candidates:
        return None
    ts, row = max(candidates, key=lambda x: x[0])
    minutes = int((now - ts).total_seconds() / 60)
    return {"event": row["event"], "impact": row["impact"], "minutes": minutes}
