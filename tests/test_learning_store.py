"""Tests for the labeled-corpus capture store."""
from __future__ import annotations

from src import learning_store
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


def test_capture_inserts_row(tmp_path):
    conn = _setup(tmp_path)
    sid = learning_store.capture(
        conn, source_channel_id="ch1", route_id="r1", source_msg_id=10,
        text="احجز نصف", category="signal", action_types="CLOSE_PARTIAL",
        triage_decision="keep",
    )
    assert sid is not None
    rows = learning_store.recent(conn, "ch1", 10)
    assert len(rows) == 1
    assert rows[0].category == "signal"
    assert rows[0].action_types == "CLOSE_PARTIAL"
    assert rows[0].seen_count == 1


def test_capture_dedups_and_bumps_seen_count(tmp_path):
    conn = _setup(tmp_path)
    learning_store.capture(
        conn, source_channel_id="ch1", route_id="", source_msg_id=1,
        text="صباح الخير", category="ignore", action_types="", triage_decision="keep")
    learning_store.capture(
        conn, source_channel_id="ch1", route_id="", source_msg_id=2,
        text="صباح الخير", category="ignore", action_types="", triage_decision="keep")
    rows = learning_store.recent(conn, "ch1", 10)
    assert len(rows) == 1
    assert rows[0].seen_count == 2


def test_capture_isolated_per_channel(tmp_path):
    conn = _setup(tmp_path)
    learning_store.capture(conn, source_channel_id="a", route_id="", source_msg_id=1,
                           text="x", category="ignore", action_types="", triage_decision="")
    learning_store.capture(conn, source_channel_id="b", route_id="", source_msg_id=2,
                           text="x", category="ignore", action_types="", triage_decision="")
    assert len(learning_store.recent(conn, "a", 10)) == 1
    assert len(learning_store.recent(conn, "b", 10)) == 1


def test_capture_blank_channel_is_noop(tmp_path):
    conn = _setup(tmp_path)
    assert learning_store.capture(
        conn, source_channel_id="", route_id="", source_msg_id=1,
        text="x", category="ignore", action_types="", triage_decision="") is None


def test_capture_never_raises_on_bad_conn():
    class Boom:
        def execute(self, *a, **k):
            raise RuntimeError("db down")
    assert learning_store.capture(
        Boom(), source_channel_id="c", route_id="", source_msg_id=1,
        text="x", category="ignore", action_types="", triage_decision="") is None


def test_prune_keeps_newest(tmp_path):
    conn = _setup(tmp_path)
    for i in range(10):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text=f"m{i}", category="ignore",
                               action_types="", triage_decision="")
    learning_store.prune(conn, "c", keep=5)
    rows = learning_store.recent(conn, "c", 100)
    assert len(rows) == 5
    assert rows[0].text == "m9"  # newest first


def test_embedding_roundtrip(tmp_path):
    conn = _setup(tmp_path)
    sid = learning_store.capture(conn, source_channel_id="c", route_id="",
                                 source_msg_id=1, text="m", category="ignore",
                                 action_types="", triage_decision="")
    assert learning_store.without_embedding(conn, "c", 10)[0].id == sid
    learning_store.set_embedding(conn, sid, [0.1, 0.2, 0.3])
    assert learning_store.without_embedding(conn, "c", 10) == []
    assert learning_store.recent(conn, "c", 10)[0].embedding == [0.1, 0.2, 0.3]
