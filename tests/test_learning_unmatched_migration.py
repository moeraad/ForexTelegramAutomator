"""Pending unmatched_messages rows are folded into learning_samples once."""
from __future__ import annotations

from src.db import connect, init_schema, _migrate_unmatched_into_learning


def test_unmatched_rows_migrated(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    # Simulate a pre-CLL DB with queued unmatched rows lacking channel id.
    conn.execute(
        "INSERT INTO unmatched_messages(text, norm_text, suggested_action_type, "
        "source_msg_id) VALUES('احجز نصف','احجز نصف','CLOSE_PARTIAL',7)"
    )
    n = _migrate_unmatched_into_learning(conn, fallback_channel_id="legacy")
    assert n == 1
    rows = conn.execute(
        "SELECT category, action_types, source_channel_id FROM learning_samples"
    ).fetchall()
    assert rows[0]["action_types"] == "CLOSE_PARTIAL"
    assert rows[0]["category"] == "signal"
    assert rows[0]["source_channel_id"] == "legacy"
    # Idempotent: second run migrates nothing (dedup on norm_text).
    assert _migrate_unmatched_into_learning(conn, fallback_channel_id="legacy") == 0
