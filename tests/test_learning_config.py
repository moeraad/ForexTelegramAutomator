"""Learning-loop config defaults are seeded and typed."""

from __future__ import annotations

from src.db import connect, init_schema
from src import db_settings


def test_learning_defaults_present_and_typed(tmp_path):
    db = tmp_path / "s.db"
    conn = connect(str(db))
    init_schema(conn)  # runs _migrate_seed_settings_defaults
    conn.close()

    assert db_settings.get_bool(db, "learning_enabled", False) is True
    assert db_settings.get_int(db, "learning_batch_n", 0) == 50
    assert db_settings.get_int(db, "learning_corpus_max", 0) == 2000
    assert db_settings.get_int(db, "learning_min_cluster_size", 0) == 4
    assert db_settings.get_float(db, "learning_embed_threshold", 0.0) == 0.82
    assert db_settings.get_int(db, "learning_min_support", 0) == 5
    assert db_settings.get_float(db, "learning_min_purity", 0.0) == 0.9
    assert db_settings.get_int(db, "learning_suggestion_ttl_days", 0) == 14
