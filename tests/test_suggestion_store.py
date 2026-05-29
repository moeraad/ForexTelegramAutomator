from __future__ import annotations

from src import suggestion_store
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_add_and_list(tmp_path):
    conn = _setup(tmp_path)
    sid = suggestion_store.add(
        conn, source_channel_id="c", rule_kind="noise", target_layer="triage",
        payload={"phrase": "مبروك"}, evidence={"would_suppress": 12})
    assert sid is not None
    items = suggestion_store.list_proposed(conn, "c")
    assert len(items) == 1
    assert items[0].rule_kind == "noise"
    assert items[0].payload == {"phrase": "مبروك"}
    assert items[0].evidence["would_suppress"] == 12


def test_add_dedups_same_pending(tmp_path):
    conn = _setup(tmp_path)
    suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                         target_layer="triage", payload={"phrase": "مبروك"},
                         evidence={})
    again = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                                 target_layer="triage", payload={"phrase": "مبروك"},
                                 evidence={})
    assert again is None
    assert len(suggestion_store.list_proposed(conn, "c")) == 1


def test_set_status_frees_dedupe(tmp_path):
    conn = _setup(tmp_path)
    sid = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                              target_layer="triage", payload={"phrase": "x"},
                              evidence={})
    assert suggestion_store.set_status(conn, sid, "dismissed") is True
    assert suggestion_store.list_proposed(conn, "c") == []
    # Same payload can be proposed again now that the prior one is decided.
    assert suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                                target_layer="triage", payload={"phrase": "x"},
                                evidence={}) is not None


def test_expire_old(tmp_path):
    conn = _setup(tmp_path)
    sid = suggestion_store.add(conn, source_channel_id="c", rule_kind="noise",
                              target_layer="triage", payload={"phrase": "x"},
                              evidence={})
    conn.execute(
        "UPDATE learning_suggestions SET created_at=datetime('now','-30 days') "
        "WHERE id=?", (sid,))
    assert suggestion_store.expire_old(conn, ttl_days=14) == 1
    assert suggestion_store.list_proposed(conn, "c") == []
