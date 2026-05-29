from __future__ import annotations

import json

from src import suggestion_store
from src.db import connect, init_schema
from src.gui.views.suggestions_logic import rank_suggestions, accept_suggestion


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
