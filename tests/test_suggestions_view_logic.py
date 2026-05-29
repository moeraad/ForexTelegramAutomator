from __future__ import annotations

import json

from src import suggestion_store
from src.db import connect, init_schema
from src.gui.views.suggestions_logic import (
    accept_suggestion,
    rank_suggestions,
    resolve_profile_path,
)


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
