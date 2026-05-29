from __future__ import annotations

import json

from src import profile_writer, suggestion_store
from src.db import connect, init_schema


def _profile(tmp_path, data):
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return p


def _sugg(conn, **kw):
    sid = suggestion_store.add(conn, **kw)
    return suggestion_store.get(conn, sid)


def test_noise_appends_to_noise_patterns(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD", "noise_patterns": "old"})
    s = _sugg(conn, source_channel_id="c", rule_kind="noise",
              target_layer="triage", payload={"phrase": "مبروك"}, evidence={})
    profile_writer.apply_suggestion(p, s)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert "مبروك" in out["noise_patterns"]
    assert "old" in out["noise_patterns"]


def test_action_trigger_appends_trigger_entry(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD", "triggers": []})
    s = _sugg(conn, source_channel_id="c", rule_kind="action_trigger",
              target_layer="matcher",
              payload={"phrase": "احجز نصف", "action_types": ["CLOSE_PARTIAL"],
                       "samples": ["احجز نصف ارباحك"]}, evidence={})
    profile_writer.apply_suggestion(p, s)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert len(out["triggers"]) == 1
    t = out["triggers"][0]
    assert t["action_types"] == ["CLOSE_PARTIAL"]
    assert t["samples"] == ["احجز نصف ارباحك"]


def test_keep_trigger_appends_triage_keep(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD"})
    s = _sugg(conn, source_channel_id="c", rule_kind="keep_trigger",
              target_layer="triage", payload={"phrase": "ادخل الان"}, evidence={})
    profile_writer.apply_suggestion(p, s)
    out = json.loads(p.read_text(encoding="utf-8"))
    assert "ادخل الان" in out["triage_keep_triggers"]


def test_unknown_kind_raises(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    p = _profile(tmp_path, {"symbol": "XAUUSD"})
    s = _sugg(conn, source_channel_id="c", rule_kind="action_trigger",
              target_layer="matcher", payload={"phrase": "x"}, evidence={})
    # missing action_types/samples in payload → invalid
    import pytest
    with pytest.raises(ValueError):
        profile_writer.apply_suggestion(p, s)
