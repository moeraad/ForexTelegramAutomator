"""Tests for the unmatched-messages capture queue."""
from __future__ import annotations

from dataclasses import dataclass

import pytest

from src import unmatched_store
from src.db import connect, init_schema


def _setup(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    return conn


@dataclass
class _FakeAction:
    """Stand-in for validators.Action — duck-types `.type`."""
    type: str


def test_record_inserts_when_action_is_emittable(tmp_path):
    conn = _setup(tmp_path)
    row_id = unmatched_store.record(
        conn,
        text="احجز نصف وأمن دخولك",
        source_msg_id=123,
        actions=[_FakeAction(type="CLOSE_PARTIAL"), _FakeAction(type="MOVE_SL_BE")],
    )
    assert row_id is not None
    pending = unmatched_store.list_pending(conn)
    assert len(pending) == 1
    assert pending[0].text == "احجز نصف وأمن دخولك"
    # First emittable type wins as the suggestion.
    assert pending[0].suggested_action_type == "CLOSE_PARTIAL"
    assert pending[0].source_msg_id == 123


def test_record_skips_when_no_emittable_action(tmp_path):
    """OPEN / ALERT / etc. aren't deterministic-emittable from the
    matcher, so capturing them as trigger candidates would be useless."""
    conn = _setup(tmp_path)
    row_id = unmatched_store.record(
        conn, text="some open signal", source_msg_id=1,
        actions=[_FakeAction(type="OPEN"), _FakeAction(type="ALERT")],
    )
    assert row_id is None
    assert unmatched_store.list_pending(conn) == []


def test_record_skips_when_actions_empty(tmp_path):
    conn = _setup(tmp_path)
    assert unmatched_store.record(
        conn, text="hi", source_msg_id=1, actions=[]
    ) is None
    assert unmatched_store.list_pending(conn) == []


def test_record_dedups_by_normalized_text(tmp_path):
    """Same canonical message arriving twice (e.g. resend / quote)
    collapses into a single queued row."""
    conn = _setup(tmp_path)
    first = unmatched_store.record(
        conn, text="احجز نصف", source_msg_id=1,
        actions=[_FakeAction(type="CLOSE_PARTIAL")],
    )
    second = unmatched_store.record(
        conn, text="احجز نصف", source_msg_id=2,
        actions=[_FakeAction(type="CLOSE_PARTIAL")],
    )
    assert first is not None
    assert second is None
    assert len(unmatched_store.list_pending(conn)) == 1


def test_record_skips_empty_text(tmp_path):
    conn = _setup(tmp_path)
    assert unmatched_store.record(
        conn, text="", source_msg_id=1,
        actions=[_FakeAction(type="CLOSE_PARTIAL")],
    ) is None
    assert unmatched_store.record(
        conn, text="   ", source_msg_id=1,
        actions=[_FakeAction(type="CLOSE_PARTIAL")],
    ) is None


def test_prune_drops_oldest_when_over_cap(tmp_path, monkeypatch):
    """Cap is enforced FIFO — oldest rows drop first to make room."""
    conn = _setup(tmp_path)
    monkeypatch.setattr(unmatched_store, "MAX_ROWS", 3)
    # Use words that differ AFTER normalization (which collapses digit
    # runs to <N>) so each insert actually creates a distinct row.
    texts = ["alpha", "beta", "gamma", "delta", "epsilon"]
    for i, t in enumerate(texts):
        unmatched_store.record(
            conn, text=t, source_msg_id=i,
            actions=[_FakeAction(type="CLOSE_PARTIAL")],
        )
    pending = unmatched_store.list_pending(conn)
    assert len(pending) == 3
    seen = {r.text for r in pending}
    assert "alpha" not in seen
    assert "beta" not in seen
    assert "epsilon" in seen


def test_delete_removes_row(tmp_path):
    conn = _setup(tmp_path)
    rid = unmatched_store.record(
        conn, text="x", source_msg_id=1,
        actions=[_FakeAction(type="CLOSE_PARTIAL")],
    )
    assert unmatched_store.delete(conn, rid) is True
    assert unmatched_store.list_pending(conn) == []


def test_delete_returns_false_when_missing(tmp_path):
    conn = _setup(tmp_path)
    assert unmatched_store.delete(conn, 99999) is False


def test_count_pending_matches_list(tmp_path):
    conn = _setup(tmp_path)
    for t in ("first", "second", "third"):
        unmatched_store.record(
            conn, text=t, source_msg_id=1,
            actions=[_FakeAction(type="MOVE_SL_BE")],
        )
    assert unmatched_store.count_pending(conn) == 3


def test_suggested_action_type_filters_to_emittable_only(tmp_path):
    """An emission like [OPEN, MOVE_SL_BE] picks MOVE_SL_BE, not OPEN —
    the matcher can't emit OPEN, so OPEN as a suggestion would be a dead
    end in the Add Trigger dialog."""
    conn = _setup(tmp_path)
    rid = unmatched_store.record(
        conn, text="comp", source_msg_id=1,
        actions=[_FakeAction(type="OPEN"), _FakeAction(type="MOVE_SL_BE")],
    )
    assert rid is not None
    row = unmatched_store.list_pending(conn)[0]
    assert row.suggested_action_type == "MOVE_SL_BE"
