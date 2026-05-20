"""Generic feature store backed by the `settings` table.

The evaluator's `required_features` taxonomy (see `trading_style.py`)
names individual features — `silver`, `real_yield_10y`, `cot_managed_money`.
This module lets feed workers put each one as its own row and lets the
evaluator read it with a freshness check, without inventing a new table
or coordinating five blob shapes.

Layout per feature `name`:

  - `feature_<name>`       -> JSON-encoded value (any type)
  - `feature_<name>_at`    -> ISO-8601 UTC timestamp

Why JSON-encoded values: most features are floats but a few are dicts
(e.g. `key_levels_intraday`) or lists (`h1_recent_closes`). One serializer
for all of them keeps the store interface trivial. The handful that are
plain floats pay one `json.loads` per read, which is negligible against
the rest of the evaluator's work.

Stale handling is the caller's responsibility. `get_with_age` returns the
age so the evaluator can decide per-feature whether the value is fresh
enough to use (intraday-relevant features tolerate seconds; macro
features tolerate hours). One global stale threshold doesn't work across
the spread of features this store holds.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger(__name__)


def _now_iso() -> str:
    """ISO-8601 UTC with explicit timezone — matches the project's
    convention (all timestamps tz-aware) so age comparisons against
    other settings rows are correct."""
    return datetime.now(timezone.utc).isoformat()


def put(conn: sqlite3.Connection, name: str, value: Any) -> None:
    """Write `value` under `feature_<name>`, stamping `feature_<name>_at`
    with the current UTC time.

    Atomic: both rows commit together. The connection's isolation_level
    is None in the project's `db.connect`, so an explicit transaction is
    needed; we wrap with BEGIN/COMMIT and roll back on serialization
    failure (rare, but the feed worker writes from a separate thread).

    Silently logs and returns on JSON-serialization failure — a bad
    feature value must not crash the feed worker for the rest of the
    features. The store stays in last-known state for that name.
    """
    try:
        encoded = json.dumps(value)
    except (TypeError, ValueError) as e:
        log.warning("feature_store.put(%r): unserializable value: %s", name, e)
        return
    now = _now_iso()
    try:
        conn.execute("BEGIN")
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"feature_{name}", encoded),
        )
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"feature_{name}_at", now),
        )
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        log.warning("feature_store.put(%r): DB error: %s", name, e)
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def get(conn: sqlite3.Connection, name: str) -> Any | None:
    """Return the decoded value, or None when missing/unparseable.

    Convenience for callers that only need the value (most consumers).
    For freshness-aware reads use `get_with_age`.
    """
    value, _ = get_with_age(conn, name)
    return value


def get_with_age(
    conn: sqlite3.Connection, name: str
) -> tuple[Any | None, int | None]:
    """Return (value, age_seconds). Either component may be None.

    `value is None and age is None` -> feature has never been written.
    `value is None and age is not None` -> stored value is unparseable
       (corruption); the timestamp row survived. Callers should treat
       this as "missing" but the breadcrumb helps diagnostics.
    `value is not None and age is None` -> stored value is parseable but
       its `_at` row is missing. Treat as stale.
    """
    rows = {
        r["key"]: r["value"]
        for r in conn.execute(
            "SELECT key, value FROM settings WHERE key IN (?, ?)",
            (f"feature_{name}", f"feature_{name}_at"),
        ).fetchall()
    }
    raw = rows.get(f"feature_{name}")
    at = rows.get(f"feature_{name}_at")
    value: Any | None
    if raw is None:
        value = None
    else:
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            value = None
    age: int | None
    if at is None:
        age = None
    else:
        try:
            iso = at.replace(" ", "T")
            if not (iso.endswith("Z") or "+" in iso[10:] or "-" in iso[10:]):
                iso = iso + "+00:00"
            if iso.endswith("Z"):
                iso = iso[:-1] + "+00:00"
            ts = datetime.fromisoformat(iso)
            age = int((datetime.now(timezone.utc) - ts).total_seconds())
        except (ValueError, TypeError):
            age = None
    return value, age


def put_many(conn: sqlite3.Connection, features: dict[str, Any]) -> None:
    """Bulk-put. Each entry stamps its own timestamp at write time, so a
    batch of 6 features all share the same `_at` to single-second
    resolution. Useful when a feed worker fetches several features in
    one round-trip and wants them treated as a coherent snapshot."""
    now = _now_iso()
    pairs: list[tuple[str, str]] = []
    for name, value in features.items():
        try:
            pairs.append((f"feature_{name}", json.dumps(value)))
        except (TypeError, ValueError) as e:
            log.warning("feature_store.put_many(%r): skipping unserializable: %s", name, e)
            continue
        pairs.append((f"feature_{name}_at", now))
    if not pairs:
        return
    try:
        conn.execute("BEGIN")
        for key, val in pairs:
            conn.execute(
                "INSERT INTO settings(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, val),
            )
        conn.execute("COMMIT")
    except sqlite3.Error as e:
        log.warning("feature_store.put_many: DB error: %s", e)
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error:
            pass


def names_present(conn: sqlite3.Connection) -> list[str]:
    """List feature names currently stored. The GUI status panel uses
    this to show which feeds are alive. Strips the `feature_` prefix
    and excludes the `_at` companion rows."""
    rows = conn.execute(
        "SELECT key FROM settings WHERE key LIKE 'feature_%' AND key NOT LIKE 'feature_%\\_at' ESCAPE '\\'"
    ).fetchall()
    return [r["key"][len("feature_"):] for r in rows]
