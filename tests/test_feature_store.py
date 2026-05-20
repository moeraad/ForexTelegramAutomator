"""Tests for the generic feature store."""
from __future__ import annotations

import time

from src import feature_store
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_put_then_get_roundtrips_float(tmp_path):
    conn = _setup(tmp_path)
    feature_store.put(conn, "silver", 31.42)
    assert feature_store.get(conn, "silver") == 31.42


def test_put_then_get_roundtrips_dict(tmp_path):
    """Most features are floats but a few (key_levels_intraday,
    h1_recent_closes) are nested. One JSON path handles both."""
    conn = _setup(tmp_path)
    payload = {"value": 31.42, "chg_pct": -0.18}
    feature_store.put(conn, "silver", payload)
    assert feature_store.get(conn, "silver") == payload


def test_put_then_get_roundtrips_list(tmp_path):
    conn = _setup(tmp_path)
    feature_store.put(conn, "h1_recent_closes", [4865.0, 4870.5, 4868.2])
    assert feature_store.get(conn, "h1_recent_closes") == [4865.0, 4870.5, 4868.2]


def test_get_missing_returns_none(tmp_path):
    conn = _setup(tmp_path)
    assert feature_store.get(conn, "never_written") is None


def test_get_with_age_returns_value_and_seconds(tmp_path):
    conn = _setup(tmp_path)
    feature_store.put(conn, "dxy", 104.32)
    value, age = feature_store.get_with_age(conn, "dxy")
    assert value == 104.32
    assert age is not None
    assert 0 <= age < 5  # within the test runtime


def test_get_with_age_missing_returns_none_pair(tmp_path):
    conn = _setup(tmp_path)
    value, age = feature_store.get_with_age(conn, "never_written")
    assert value is None
    assert age is None


def test_put_overwrites_previous_value_and_timestamp(tmp_path):
    """A re-put with the same name replaces both rows. Without this, an
    old value with a fresh `_at` would lie about its freshness."""
    conn = _setup(tmp_path)
    feature_store.put(conn, "vix", 14.0)
    time.sleep(1.1)  # ensure timestamp would differ
    feature_store.put(conn, "vix", 21.7)
    value, age = feature_store.get_with_age(conn, "vix")
    assert value == 21.7
    assert age is not None and age < 5


def test_put_many_writes_all_with_shared_timestamp(tmp_path):
    """A batch write stamps all entries with the same `_at` to
    second-resolution. The feed worker uses this so a snapshot's
    features are read as a coherent whole."""
    conn = _setup(tmp_path)
    feature_store.put_many(conn, {
        "dxy": 104.0,
        "vix": 14.2,
        "real_yield": 2.18,
    })
    _, age_d = feature_store.get_with_age(conn, "dxy")
    _, age_v = feature_store.get_with_age(conn, "vix")
    _, age_r = feature_store.get_with_age(conn, "real_yield")
    assert age_d is not None and age_v is not None and age_r is not None
    # Same second; difference must be 0 or 1 seconds.
    assert max(age_d, age_v, age_r) - min(age_d, age_v, age_r) <= 1


def test_put_many_skips_unserializable_but_persists_others(tmp_path):
    """A bad value must not poison the whole batch. Open file is the
    canonical unserializable: it's not JSON, not str, not number."""
    conn = _setup(tmp_path)
    feature_store.put_many(conn, {
        "good": 1.5,
        "bad": object(),  # raw object — not JSON-serializable
        "also_good": "ok",
    })
    assert feature_store.get(conn, "good") == 1.5
    assert feature_store.get(conn, "also_good") == "ok"
    assert feature_store.get(conn, "bad") is None


def test_names_present_returns_only_features(tmp_path):
    """`settings` holds lots of non-feature keys (api_host, etc.).
    `names_present` must list only feature_<name> rows."""
    conn = _setup(tmp_path)
    feature_store.put(conn, "dxy", 104.0)
    feature_store.put(conn, "vix", 14.0)
    names = feature_store.names_present(conn)
    assert set(names) == {"dxy", "vix"}


def test_get_after_corrupted_value_returns_none(tmp_path):
    """If something writes unparseable JSON directly to settings (e.g.
    a manual edit, a migration bug), get must not raise — it returns
    None and lets the caller treat the feature as missing."""
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("feature_dxy", "not-json-at-all"),
    )
    conn.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?)",
        ("feature_dxy_at", "2026-05-20T10:00:00+00:00"),
    )
    value, age = feature_store.get_with_age(conn, "dxy")
    assert value is None
    # The `_at` row survived; the breadcrumb is intentional.
    assert age is not None
