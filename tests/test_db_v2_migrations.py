"""Tests for v2 multi-channel DB scaffolding.

See ``docs/plans/2026-05-23-multi-channel-routing.md`` Step 2. Covers:

  - Fresh schema includes source_channel_id, route_id, bot_outbox
  - Migrations are idempotent (running twice = no-op, no exception)
  - Migrations applied to an "old-shape" DB (without these columns) add
    them without losing existing rows
  - backfill_source_channel_id and backfill_route_id update only NULL rows
"""
from __future__ import annotations

import sqlite3

import pytest

from src.db import (
    _migrate_actions_add_source_channel_route,
    _migrate_create_bot_outbox,
    _migrate_messages_add_source_channel,
    backfill_route_id,
    backfill_source_channel_id,
    connect,
    init_schema,
)


def _table_cols(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _index_names(conn: sqlite3.Connection) -> set[str]:
    return {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index'"
    ).fetchall()}


# ---- Fresh schema -----------------------------------------------------------


def test_fresh_messages_has_source_channel_id():
    conn = connect(":memory:")
    init_schema(conn)
    assert "source_channel_id" in _table_cols(conn, "messages")
    assert "idx_messages_source_channel" in _index_names(conn)


def test_fresh_actions_has_source_channel_and_route():
    conn = connect(":memory:")
    init_schema(conn)
    cols = _table_cols(conn, "actions")
    assert "source_channel_id" in cols
    assert "route_id" in cols
    indexes = _index_names(conn)
    assert "idx_actions_source_channel" in indexes
    assert "idx_actions_route" in indexes


def test_fresh_schema_has_bot_outbox():
    conn = connect(":memory:")
    init_schema(conn)
    tables = {row[0] for row in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()}
    assert "bot_outbox" in tables
    cols = _table_cols(conn, "bot_outbox")
    assert cols == {
        "id", "bot_id", "event_type", "event_payload",
        "source_channel_id", "route_id", "action_id",
        "created_at", "delivered_at",
    }
    assert "idx_bot_outbox_undelivered" in _index_names(conn)


# ---- Migration idempotency on fresh schema ---------------------------------


def test_migrations_idempotent_on_fresh_schema():
    conn = connect(":memory:")
    init_schema(conn)
    # Running each migration a second time must not raise or duplicate columns.
    _migrate_messages_add_source_channel(conn)
    _migrate_actions_add_source_channel_route(conn)
    _migrate_create_bot_outbox(conn)
    # Single occurrence each.
    msg_cols = [r["name"] for r in conn.execute("PRAGMA table_info(messages)").fetchall()]
    assert msg_cols.count("source_channel_id") == 1
    act_cols = [r["name"] for r in conn.execute("PRAGMA table_info(actions)").fetchall()]
    assert act_cols.count("source_channel_id") == 1
    assert act_cols.count("route_id") == 1


# ---- Migration applied to old-shape DB (simulates pre-v2 production) -------


def _make_old_shape_db() -> sqlite3.Connection:
    """A connection where messages/actions exist without v2 columns and
    bot_outbox doesn't exist. Used to verify the migrations work on real
    operator DBs from before this commit."""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        "CREATE TABLE messages ("
        "  id INTEGER PRIMARY KEY,"
        "  tg_message_id INTEGER NOT NULL,"
        "  chat_id INTEGER NOT NULL,"
        "  sender TEXT,"
        "  text TEXT NOT NULL,"
        "  received_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "  is_backfill INTEGER DEFAULT 0,"
        "  UNIQUE(chat_id, tg_message_id)"
        ");"
        "CREATE TABLE actions ("
        "  id INTEGER PRIMARY KEY,"
        "  source_msg_id INTEGER REFERENCES messages(id),"
        "  action_type TEXT NOT NULL,"
        "  payload_json TEXT NOT NULL,"
        "  status TEXT NOT NULL DEFAULT 'pending'"
        ");"
    )
    return conn


def test_messages_migration_preserves_existing_rows():
    conn = _make_old_shape_db()
    conn.execute(
        "INSERT INTO messages(id, tg_message_id, chat_id, text) VALUES (1, 100, -42, 'hi')"
    )
    _migrate_messages_add_source_channel(conn)
    row = conn.execute("SELECT * FROM messages WHERE id=1").fetchone()
    assert row["text"] == "hi"
    assert row["source_channel_id"] is None


def test_actions_migration_preserves_existing_rows():
    conn = _make_old_shape_db()
    conn.execute(
        "INSERT INTO actions(id, action_type, payload_json) "
        "VALUES (1, 'OPEN', '{}')"
    )
    _migrate_actions_add_source_channel_route(conn)
    row = conn.execute("SELECT * FROM actions WHERE id=1").fetchone()
    assert row["action_type"] == "OPEN"
    assert row["source_channel_id"] is None
    assert row["route_id"] is None


def test_bot_outbox_migration_creates_table_when_missing():
    conn = _make_old_shape_db()
    _migrate_create_bot_outbox(conn)
    cols = _table_cols(conn, "bot_outbox")
    assert "bot_id" in cols
    assert "delivered_at" in cols


def test_migrations_idempotent_on_old_shape_db():
    """Running the three v2 migrations twice on an old-shape DB is safe."""
    conn = _make_old_shape_db()
    for _ in range(2):
        _migrate_messages_add_source_channel(conn)
        _migrate_actions_add_source_channel_route(conn)
        _migrate_create_bot_outbox(conn)
    # No exception; columns and table present once.
    assert "source_channel_id" in _table_cols(conn, "messages")
    assert "source_channel_id" in _table_cols(conn, "actions")
    assert "route_id" in _table_cols(conn, "actions")


# ---- Backfill helpers ------------------------------------------------------


def test_backfill_source_channel_id_updates_null_rows():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, text) VALUES (1, -42, 'a')"
    )
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, text, source_channel_id) "
        "VALUES (2, -42, 'b', 'ch_existing')"
    )
    conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES ('OPEN', '{}')"
    )
    msg_updated, act_updated = backfill_source_channel_id(conn, "ch_fe")
    assert msg_updated == 1  # only the NULL row updated
    assert act_updated == 1
    # The pre-set row was not overwritten
    keepers = conn.execute(
        "SELECT source_channel_id FROM messages WHERE tg_message_id=2"
    ).fetchone()
    assert keepers["source_channel_id"] == "ch_existing"
    updated = conn.execute(
        "SELECT source_channel_id FROM messages WHERE tg_message_id=1"
    ).fetchone()
    assert updated["source_channel_id"] == "ch_fe"


def test_backfill_source_channel_id_noop_when_already_set():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute(
        "INSERT INTO messages(tg_message_id, chat_id, text, source_channel_id) "
        "VALUES (1, -42, 'x', 'ch_a')"
    )
    msg_updated, act_updated = backfill_source_channel_id(conn, "ch_b")
    assert msg_updated == 0
    assert act_updated == 0
    row = conn.execute("SELECT source_channel_id FROM messages").fetchone()
    assert row["source_channel_id"] == "ch_a"


def test_backfill_route_id_updates_null_rows():
    conn = connect(":memory:")
    init_schema(conn)
    conn.execute("INSERT INTO actions(action_type, payload_json) VALUES ('OPEN', '{}')")
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, route_id) "
        "VALUES ('CLOSE_FULL', '{}', 'route_existing')"
    )
    updated = backfill_route_id(conn, "route_fe")
    assert updated == 1
    rows = list(conn.execute(
        "SELECT id, route_id FROM actions ORDER BY id"
    ).fetchall())
    assert rows[0]["route_id"] == "route_fe"
    assert rows[1]["route_id"] == "route_existing"


def test_backfill_returns_zero_on_empty_table():
    conn = connect(":memory:")
    init_schema(conn)
    msg_updated, act_updated = backfill_source_channel_id(conn, "ch_fe")
    assert msg_updated == 0
    assert act_updated == 0
    assert backfill_route_id(conn, "route_fe") == 0
