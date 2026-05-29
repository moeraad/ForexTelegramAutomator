"""learning_samples + learning_suggestions tables exist with the right shape."""
from __future__ import annotations

from src.db import connect, init_schema


def _cols(conn, table):
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def test_learning_samples_table_shape(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    cols = _cols(conn, "learning_samples")
    assert {
        "id", "source_channel_id", "route_id", "source_msg_id",
        "text", "norm_text", "category", "action_types",
        "triage_decision", "seen_count", "embedding_json",
        "created_at", "last_seen_at",
    } <= cols
    # uniqueness on (source_channel_id, norm_text)
    conn.execute(
        "INSERT INTO learning_samples(source_channel_id, norm_text, text, "
        "category, action_types, triage_decision, seen_count) "
        "VALUES('c','n','t','ignore','','keep',1)"
    )
    cur = conn.execute(
        "INSERT OR IGNORE INTO learning_samples(source_channel_id, norm_text, text, "
        "category, action_types, triage_decision, seen_count) "
        "VALUES('c','n','t2','ignore','','keep',1)"
    )
    assert cur.rowcount == 0  # duplicate (c,n) ignored
