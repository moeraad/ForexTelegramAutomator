from __future__ import annotations

import json

from types import SimpleNamespace

from src import suggestion_store
from src.db import connect, init_schema
from src.gui.views.suggestions_logic import (
    accept_suggestion,
    rank_suggestions,
    resolve_channel_profile_path,
    resolve_profile_path,
)


class _FakeCfg:
    """Minimal duck-typed stand-in for ConfigV2 (channel/profile/route/destination)."""

    def __init__(self, *, channels=(), profiles=(), routes=(), destinations=()):
        self._channels = list(channels)
        self._profiles = list(profiles)
        self._routes = list(routes)
        self._destinations = list(destinations)

    def channel(self, cid):
        return next((c for c in self._channels if c.id == cid), None)

    def profile(self, pid):
        return next((p for p in self._profiles if p.id == pid), None)

    def destination(self, did):
        return next((d for d in self._destinations if d.id == did), None)

    def routes_for_channel(self, cid):
        return tuple(r for r in self._routes if r.channel_id == cid)


def test_rank_puts_accuracy_gains_and_one_tap_first(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                         target_layer="triage", payload={"phrase": "a"},
                         evidence={"one_tap_safe": False, "would_suppress": 3})
    suggestion_store.add(conn, source_channel_id="c", rule_kind="keep_trigger",
                         target_layer="triage", payload={"phrase": "b"},
                         evidence={"one_tap_safe": True})
    suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                         target_layer="triage", payload={"phrase": "d"},
                         evidence={"one_tap_safe": True, "would_suppress": 40})
    ranked = rank_suggestions(suggestion_store.list_proposed(conn, "c"))
    assert ranked[0].rule_kind == "keep_trigger"
    assert ranked[-1].evidence["one_tap_safe"] is False


def test_accept_writes_profile_and_marks_accepted(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"symbol": "XAUUSD"}), encoding="utf-8")
    sid = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                               target_layer="triage", payload={"phrase": "مبروك"},
                               evidence={"one_tap_safe": True})
    accept_suggestion(conn, sid, profile_path=p)
    assert suggestion_store.get(conn, sid).status == "accepted"
    assert "مبروك" in json.loads(p.read_text(encoding="utf-8"))["noise_patterns"]


def test_resolve_profile_path_prefers_db_adjacent_live_file(tmp_path):
    # Live profile.json sits next to the stack DB — the location the matcher
    # and triage actually read at runtime. It must win over the legacy path.
    db_dir = tmp_path / "stack"; db_dir.mkdir()
    db_path = db_dir / "copytrades.db"
    live = db_dir / "profile.json"; live.write_text("{}", encoding="utf-8")
    legacy = tmp_path / "channels" / "x.json"
    legacy.parent.mkdir(); legacy.write_text("{}", encoding="utf-8")
    assert resolve_profile_path(db_path, legacy) == live


def test_resolve_profile_path_falls_back_to_legacy_when_no_live(tmp_path):
    db_dir = tmp_path / "stack"; db_dir.mkdir()
    db_path = db_dir / "copytrades.db"  # no profile.json beside it
    legacy = tmp_path / "channels" / "x.json"
    legacy.parent.mkdir(); legacy.write_text("{}", encoding="utf-8")
    assert resolve_profile_path(db_path, legacy) == legacy


def test_resolve_profile_path_defaults_to_live_location_when_neither_exists(tmp_path):
    db_dir = tmp_path / "stack"; db_dir.mkdir()
    db_path = db_dir / "copytrades.db"
    # Neither exists → return the live (db-adjacent) location so a write targets
    # where the matcher will look.
    assert resolve_profile_path(db_path, None) == db_dir / "profile.json"


def test_resolve_channel_profile_via_v2_uses_destination_db_dir(tmp_path):
    # v2: channel -> route -> destination(db_path) -> db-adjacent profile.json,
    # NOT the (often stale) Profile.path.
    dest_dir = tmp_path / "Forex Engineer"; dest_dir.mkdir()
    db_path = dest_dir / "copytrades.db"
    live = dest_dir / "profile.json"; live.write_text("{}", encoding="utf-8")
    stale_legacy = tmp_path / "_internal" / "channels" / "fe.json"
    stale_legacy.parent.mkdir(parents=True)  # exists but must be ignored in favour of live
    stale_legacy.write_text("{}", encoding="utf-8")
    cfg = _FakeCfg(
        channels=[SimpleNamespace(id="ch_fe", profile_id="prof_fe")],
        profiles=[SimpleNamespace(id="prof_fe", path=str(stale_legacy))],
        routes=[SimpleNamespace(channel_id="ch_fe", destination_id="dest_fe")],
        destinations=[SimpleNamespace(id="dest_fe", db_path=str(db_path))],
    )
    assert resolve_channel_profile_path("ch_fe", config=cfg) == live


def test_resolve_channel_profile_falls_back_when_channel_unknown(tmp_path):
    # Unknown channel in config -> fall back to the active-stack db hint.
    stack_dir = tmp_path / "stack"; stack_dir.mkdir()
    live = stack_dir / "profile.json"; live.write_text("{}", encoding="utf-8")
    cfg = _FakeCfg(channels=[])  # no matching channel
    out = resolve_channel_profile_path(
        "ch_missing", config=cfg, fallback_db_path=stack_dir / "copytrades.db")
    assert out == live


def test_resolve_channel_profile_unknown_channel_uses_fallback_chain(tmp_path):
    # An unmatched channel id falls back to resolve_profile_path(db, legacy).
    # The db-adjacent file doesn't exist but the legacy fallback does, so the
    # legacy path is returned (its preference order is verified independently).
    stack_dir = tmp_path / "stack"; stack_dir.mkdir()
    legacy = tmp_path / "legacy.json"; legacy.write_text("{}", encoding="utf-8")
    cfg = _FakeCfg(channels=[])  # deterministic: no channel matches
    out = resolve_channel_profile_path(
        "whatever", config=cfg,
        fallback_db_path=stack_dir / "copytrades.db", fallback_legacy=legacy)
    assert out == legacy
