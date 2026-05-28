"""On-disk pre-v2 DB migration tests (Day-5 cleanup).

Catches the bug class that bit us after Step 2:

    sqlite3.OperationalError: no such column: source_channel_id

``init_schema()`` runs ``conn.executescript(SCHEMA_PATH.read_text())``
BEFORE the migration functions. On a freshly-created DB both work
(table create + index create both succeed). On an EXISTING operator DB,
``CREATE TABLE IF NOT EXISTS`` is a no-op, so the table still lacks the
new column when ``CREATE INDEX`` on that column runs.

All Step-1/2 tests used ``connect(":memory:")`` — always-fresh DBs.
The bug only surfaced on real operator DBs on first GUI launch.

These tests materialise the OLD schema to a real on-disk file and run
``init_schema()`` against it. If a future schema.sql change introduces
the same pattern, these tests fail immediately.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db import connect, init_schema


# Historical schema shapes — capture how the DB looked BEFORE the v2
# multi-channel work landed. Used to verify that init_schema upgrades
# these old shapes cleanly.

_PRE_V2_SCHEMA = """\
CREATE TABLE messages (
    id              INTEGER PRIMARY KEY,
    tg_message_id   INTEGER NOT NULL,
    chat_id         INTEGER NOT NULL,
    sender          TEXT,
    text            TEXT NOT NULL,
    received_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
    is_backfill     INTEGER DEFAULT 0,
    UNIQUE(chat_id, tg_message_id)
);
CREATE TABLE actions (
    id              INTEGER PRIMARY KEY,
    source_msg_id   INTEGER REFERENCES messages(id),
    action_type     TEXT NOT NULL CHECK(action_type IN (
                      'OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT',
                      'MOVE_SL_BE','MOVE_SL','CLOSE_PARTIAL','CLOSE_FULL',
                      'REOPEN_LAST','REINFORCE','TIGHTEN_SL','MODIFY_TPS',
                      'OPEN_INSTANT','ATTACH_SIGNAL','CANCEL_PENDING'
                    )),
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK(status IN ('pending','cancelled','sent','claimed',
                                     'watching','executed','failed','rejected')),
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    notified_at     DATETIME,
    execute_after   DATETIME,
    claimed_at      DATETIME,
    executed_at     DATETIME,
    ea_response     TEXT,
    fingerprint     TEXT
);
CREATE TABLE positions (
    id                   INTEGER PRIMARY KEY,
    action_id            INTEGER REFERENCES actions(id),
    mt5_ticket           INTEGER UNIQUE,
    symbol               TEXT NOT NULL,
    side                 TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
    volume               REAL NOT NULL,
    original_volume      REAL,
    partial_close_count  INTEGER NOT NULL DEFAULT 0,
    sl_moved_at          DATETIME,
    entry_price          REAL,
    sl                   REAL,
    tp                   REAL,
    exit_price           REAL,
    realized_pnl         REAL,
    status               TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
    opened_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
    closed_at            DATETIME,
    close_reason         TEXT,
    is_naked             INTEGER NOT NULL DEFAULT 0,
    naked_opened_at      DATETIME
);
CREATE TABLE signal_memory (
    id              INTEGER PRIMARY KEY,
    message_id      INTEGER REFERENCES messages(id),
    chat_id         INTEGER NOT NULL DEFAULT 0,
    category        TEXT NOT NULL CHECK(category IN ('context','signal','partial_signal')),
    summary         TEXT NOT NULL,
    created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
    cleared_at      DATETIME
);
CREATE TABLE settings (
    key             TEXT PRIMARY KEY,
    value           TEXT NOT NULL
);
"""


def _make_pre_v2_db(path: Path, *, seed_data: bool = True) -> None:
    """Write the pre-v2 schema to a real file on disk and (optionally)
    seed it with a couple of typical operator rows.

    The seeded data deliberately mimics a real production DB so the
    migration's backfill behavior is exercised against non-empty tables.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(_PRE_V2_SCHEMA)
        if seed_data:
            conn.execute(
                "INSERT INTO messages(tg_message_id, chat_id, sender, text) "
                "VALUES (?, ?, ?, ?)",
                (101, -1001234, "channel", "BUY GOLD entry 4000-4002 SL 3990"),
            )
            conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, "
                "payload_json, status) VALUES (?, 'OPEN', ?, 'executed')",
                (1, json.dumps({"symbol": "XAUUSD", "side": "BUY"})),
            )
            conn.execute(
                "INSERT INTO settings(key, value) VALUES "
                "('tg_phone', '+961234567'),"
                "('tg_session_name', 'session_main'),"
                "('tg_watched_chat_id', '-1001234'),"
                "('api_port', '8765')"
            )
        conn.commit()
    finally:
        conn.close()


# ---- The core regression test ---------------------------------------------


def test_init_schema_upgrades_pre_v2_db_without_error(tmp_path: Path) -> None:
    """The specific bug we hit: schema.sql contains CREATE INDEX on
    source_channel_id (added in Step 2). On a pre-v2 DB the column
    doesn't yet exist, and the index create runs BEFORE the migration
    that adds the column → executescript fails.

    This test materialises the OLD schema and calls init_schema; if any
    schema.sql change introduces a future column-before-migration trap,
    this test catches it.
    """
    db_path = tmp_path / "stack" / "copytrades.db"
    _make_pre_v2_db(db_path)

    conn = connect(str(db_path))
    # The bug manifested here:
    init_schema(conn)
    # If we got here, the migration completed.

    # Verify the new columns exist after migration.
    msg_cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(messages)").fetchall()}
    assert "source_channel_id" in msg_cols
    act_cols = {r["name"] for r in
                conn.execute("PRAGMA table_info(actions)").fetchall()}
    assert "source_channel_id" in act_cols
    assert "route_id" in act_cols

    # Verify the new bot_outbox table exists after migration.
    tables = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "bot_outbox" in tables

    # Verify the new indexes exist (the migration creates them; schema.sql
    # MUST NOT — see plan log "Post-Step-10 hot-fix").
    indexes = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}
    assert "idx_messages_source_channel" in indexes
    assert "idx_actions_source_channel" in indexes
    assert "idx_actions_route" in indexes
    assert "idx_bot_outbox_undelivered" in indexes

    conn.close()


def test_seeded_rows_survive_migration(tmp_path: Path) -> None:
    """Existing data must survive the executescript + migration pass."""
    db_path = tmp_path / "stack" / "copytrades.db"
    _make_pre_v2_db(db_path)

    conn = connect(str(db_path))
    init_schema(conn)

    msg_count = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    act_count = conn.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    settings_count = conn.execute(
        "SELECT COUNT(*) FROM settings WHERE key IN "
        "('tg_phone', 'tg_session_name', 'tg_watched_chat_id', 'api_port')"
    ).fetchone()[0]
    assert msg_count == 1
    assert act_count == 1
    assert settings_count == 4

    # The new column should default to NULL on legacy rows.
    msg = conn.execute(
        "SELECT source_channel_id FROM messages WHERE tg_message_id=101"
    ).fetchone()
    assert msg["source_channel_id"] is None
    act = conn.execute(
        "SELECT source_channel_id, route_id FROM actions LIMIT 1"
    ).fetchone()
    assert act["source_channel_id"] is None
    assert act["route_id"] is None

    conn.close()


def test_init_schema_idempotent_on_already_upgraded_db(tmp_path: Path) -> None:
    """Running init_schema twice on the same DB must not fail."""
    db_path = tmp_path / "stack" / "copytrades.db"
    _make_pre_v2_db(db_path)

    conn = connect(str(db_path))
    init_schema(conn)
    init_schema(conn)  # second pass
    conn.close()


def test_empty_pre_v2_db_upgrades_cleanly(tmp_path: Path) -> None:
    """The 'fresh install where someone manually pre-created the old schema'
    edge case — empty tables, no rows."""
    db_path = tmp_path / "stack" / "copytrades.db"
    _make_pre_v2_db(db_path, seed_data=False)

    conn = connect(str(db_path))
    init_schema(conn)

    # Tables exist, all empty.
    for tbl in ("messages", "actions", "positions", "signal_memory",
                "bot_outbox"):
        n = conn.execute(f"SELECT COUNT(*) FROM {tbl}").fetchone()[0]
        assert n == 0, f"expected {tbl} empty after migration on empty DB"
    conn.close()
