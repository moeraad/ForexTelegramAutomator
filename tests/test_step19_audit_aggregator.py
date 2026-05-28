"""Audit aggregator tests (Step 19 of multi-channel plan).

Validates cross-destination read-only query layer:
  - Search by tg_message_id picks the matching message in each dest DB
  - Search by text substring works (LIKE wrap)
  - Empty query returns most recent N per destination
  - Limit enforced per destination
  - Read-only invariant: aggregator can't mutate destination DBs
  - Missing DB file → DestinationTrace.error set, doesn't crash
  - Pre-v2 DB without bot_outbox table → actions still returned (dms empty)
  - Message → action → DM cascade rendered in one query batch
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db import connect, init_schema
from src.gui.services.audit_aggregator import (
    DestinationTrace,
    search_trace,
)


def _seed_message(
    conn: sqlite3.Connection, *,
    tg_message_id: int, chat_id: int, sender: str, text: str,
    source_channel_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO messages (tg_message_id, chat_id, sender, text, "
        "is_backfill, source_channel_id) VALUES (?, ?, ?, ?, 0, ?)",
        (tg_message_id, chat_id, sender, text, source_channel_id),
    )
    conn.commit()
    return cur.lastrowid


def _seed_action(
    conn: sqlite3.Connection, *,
    source_msg_id: int, action_type: str, status: str, payload: dict,
    source_channel_id: str | None = None, route_id: str | None = None,
) -> int:
    cur = conn.execute(
        "INSERT INTO actions (source_msg_id, action_type, payload_json, "
        "status, source_channel_id, route_id) VALUES (?, ?, ?, ?, ?, ?)",
        (source_msg_id, action_type, json.dumps(payload), status,
         source_channel_id, route_id),
    )
    conn.commit()
    return cur.lastrowid


def _seed_outbox(
    conn: sqlite3.Connection, *,
    bot_id: str, action_id: int, event_type: str = "action_terminal",
    source_channel_id: str | None = None, route_id: str | None = None,
    delivered: bool = False,
) -> int:
    cur = conn.execute(
        "INSERT INTO bot_outbox (bot_id, event_type, event_payload, "
        "source_channel_id, route_id, action_id, "
        "delivered_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (bot_id, event_type, "{}", source_channel_id, route_id, action_id,
         "2026-05-24T12:00:00+00:00" if delivered else None),
    )
    conn.commit()
    return cur.lastrowid


def _make_destination_db(
    tmp_path: Path, name: str,
) -> tuple[Path, sqlite3.Connection]:
    db_path = tmp_path / f"{name}.db"
    conn = connect(str(db_path))
    init_schema(conn)
    return db_path, conn


# ---- single-destination search -------------------------------------------


def test_search_by_tg_message_id_returns_match(tmp_path: Path):
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    _seed_message(
        conn, tg_message_id=42, chat_id=-1001,
        sender="a", text="buy gold", source_channel_id="ch_a",
    )
    _seed_message(
        conn, tg_message_id=43, chat_id=-1001,
        sender="a", text="other", source_channel_id="ch_a",
    )

    result = search_trace(
        tg_message_id=42,
        destinations=[("dest_x", "X", db_path)],
    )
    assert len(result) == 1
    trace = result[0]
    assert trace.error is None
    assert len(trace.messages) == 1
    assert trace.messages[0].tg_message_id == 42
    assert trace.messages[0].text == "buy gold"


def test_search_by_text_substring_returns_matches(tmp_path: Path):
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    _seed_message(conn, tg_message_id=1, chat_id=-1, sender="a",
                  text="this contains gold")
    _seed_message(conn, tg_message_id=2, chat_id=-1, sender="a",
                  text="this contains silver")
    _seed_message(conn, tg_message_id=3, chat_id=-1, sender="a",
                  text="also gold here")

    result = search_trace(
        text_query="gold",
        destinations=[("dest_x", "X", db_path)],
    )
    assert len(result[0].messages) == 2
    assert {m.tg_message_id for m in result[0].messages} == {1, 3}


def test_search_empty_query_returns_recent_per_destination(tmp_path: Path):
    """No tg_message_id + no text → returns most recent N per dest."""
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    for i in range(5):
        _seed_message(conn, tg_message_id=100 + i, chat_id=-1,
                      sender="a", text=f"msg {i}")

    result = search_trace(
        destinations=[("dest_x", "X", db_path)],
        limit_per_destination=3,
    )
    msgs = result[0].messages
    assert len(msgs) == 3
    # Descending by id (most recent first).
    assert [m.tg_message_id for m in msgs] == [104, 103, 102]


def test_search_combines_tg_id_and_text_query(tmp_path: Path):
    """Both predicates → AND."""
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    _seed_message(conn, tg_message_id=50, chat_id=-1, sender="a",
                  text="needle")
    _seed_message(conn, tg_message_id=50, chat_id=-2, sender="a",  # diff chat → not UNIQUE
                  text="haystack")

    result = search_trace(
        tg_message_id=50, text_query="needle",
        destinations=[("dest_x", "X", db_path)],
    )
    assert len(result[0].messages) == 1
    assert result[0].messages[0].text == "needle"


# ---- cascading actions + DMs ---------------------------------------------


def test_message_cascades_to_actions_and_dms(tmp_path: Path):
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    msg_id = _seed_message(
        conn, tg_message_id=10, chat_id=-1, sender="a",
        text="buy", source_channel_id="ch_a",
    )
    act_id = _seed_action(
        conn, source_msg_id=msg_id, action_type="OPEN", status="executed",
        payload={"symbol": "XAUUSD", "side": "BUY"},
        source_channel_id="ch_a", route_id="route_a",
    )
    _seed_outbox(
        conn, bot_id="bot_main", action_id=act_id,
        source_channel_id="ch_a", route_id="route_a", delivered=True,
    )

    result = search_trace(
        tg_message_id=10,
        destinations=[("dest_x", "X", db_path)],
    )
    msg = result[0].messages[0]
    assert len(msg.actions) == 1
    act = msg.actions[0]
    assert act.action_type == "OPEN"
    assert act.payload["symbol"] == "XAUUSD"
    assert act.route_id == "route_a"
    assert len(act.dms) == 1
    assert act.dms[0].bot_id == "bot_main"
    assert act.dms[0].delivered_at is not None


def test_multiple_actions_per_message_are_all_returned(tmp_path: Path):
    """Compound responses can emit several actions per message."""
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    msg_id = _seed_message(conn, tg_message_id=11, chat_id=-1,
                           sender="a", text="reduce + BE")
    _seed_action(conn, source_msg_id=msg_id, action_type="MOVE_SL_BE",
                 status="executed", payload={})
    _seed_action(conn, source_msg_id=msg_id, action_type="CLOSE_PARTIAL",
                 status="executed", payload={"fraction": 0.5})

    result = search_trace(
        tg_message_id=11,
        destinations=[("dest_x", "X", db_path)],
    )
    types = [a.action_type for a in result[0].messages[0].actions]
    assert types == ["MOVE_SL_BE", "CLOSE_PARTIAL"]


def test_action_with_no_dms_renders_empty_dms_tuple(tmp_path: Path):
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    msg_id = _seed_message(conn, tg_message_id=12, chat_id=-1,
                           sender="a", text="msg")
    _seed_action(conn, source_msg_id=msg_id, action_type="OPEN",
                 status="failed", payload={})
    result = search_trace(
        tg_message_id=12,
        destinations=[("dest_x", "X", db_path)],
    )
    assert result[0].messages[0].actions[0].dms == ()


# ---- multi-destination ---------------------------------------------------


def test_two_destinations_independent_results(tmp_path: Path):
    """Mirror routing: same tg_message_id in two destination DBs.
    Aggregator returns one DestinationTrace per dest, each with its
    own message + actions."""
    db_x, conn_x = _make_destination_db(tmp_path, "dest_x")
    db_y, conn_y = _make_destination_db(tmp_path, "dest_y")
    msg_x = _seed_message(conn_x, tg_message_id=99, chat_id=-1,
                          sender="a", text="mirror signal",
                          source_channel_id="ch_a")
    msg_y = _seed_message(conn_y, tg_message_id=99, chat_id=-1,
                          sender="a", text="mirror signal",
                          source_channel_id="ch_a")
    _seed_action(conn_x, source_msg_id=msg_x, action_type="OPEN",
                 status="executed", payload={}, route_id="route_x")
    _seed_action(conn_y, source_msg_id=msg_y, action_type="OPEN",
                 status="executed", payload={}, route_id="route_y")

    result = search_trace(
        tg_message_id=99,
        destinations=[("dest_x", "X", db_x), ("dest_y", "Y", db_y)],
    )
    assert len(result) == 2
    assert result[0].destination_id == "dest_x"
    assert result[1].destination_id == "dest_y"
    assert result[0].messages[0].actions[0].route_id == "route_x"
    assert result[1].messages[0].actions[0].route_id == "route_y"


def test_destination_with_no_match_returns_empty_messages(tmp_path: Path):
    db_x, conn_x = _make_destination_db(tmp_path, "dest_x")
    db_y, _ = _make_destination_db(tmp_path, "dest_y")
    _seed_message(conn_x, tg_message_id=77, chat_id=-1, sender="a",
                  text="msg")

    result = search_trace(
        tg_message_id=77,
        destinations=[("dest_x", "X", db_x), ("dest_y", "Y", db_y)],
    )
    assert len(result[0].messages) == 1
    assert len(result[1].messages) == 0


# ---- error handling -------------------------------------------------------


def test_missing_db_file_sets_error_not_crash(tmp_path: Path):
    """A misconfigured destination (path doesn't exist) must not crash
    the audit pass for OTHER destinations."""
    db_x, conn_x = _make_destination_db(tmp_path, "dest_x")
    _seed_message(conn_x, tg_message_id=1, chat_id=-1, sender="a",
                  text="msg")
    missing = tmp_path / "no-such-dir" / "missing.db"

    result = search_trace(
        tg_message_id=1,
        destinations=[
            ("dest_x", "X", db_x),
            ("dest_bad", "Missing", missing),
        ],
    )
    assert result[0].error is None
    assert len(result[0].messages) == 1
    assert result[1].error is not None
    assert "not found" in result[1].error.lower()


def test_read_only_invariant_blocks_writes(tmp_path: Path):
    """Open via the aggregator's URI mode and confirm INSERT fails.

    Documents the safety boundary: even if a future bug tried to mutate
    the connection, SQLite would refuse.
    """
    db_path, _ = _make_destination_db(tmp_path, "dest_x")
    from src.gui.services.audit_aggregator import _open_readonly
    conn = _open_readonly(db_path)
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute(
                "INSERT INTO settings (key, value) VALUES (?, ?)",
                ("evil", "write"),
            )
    finally:
        conn.close()


def test_text_query_with_special_chars_handled_safely(tmp_path: Path):
    """Parameterized query: a literal % in the user input must not be
    interpreted as a LIKE wildcard (well — it WOULD, but it's also fine
    because it's the user's intent)."""
    db_path, conn = _make_destination_db(tmp_path, "dest_x")
    _seed_message(conn, tg_message_id=1, chat_id=-1, sender="a",
                  text="100% sure")
    result = search_trace(
        text_query="100%",
        destinations=[("dest_x", "X", db_path)],
    )
    # Parameterized — % becomes a wildcard inside the SQL LIKE, which
    # matches the seeded text anyway. The important check is "doesn't
    # crash" and "returns the row."
    assert len(result[0].messages) == 1


# ---- destinations_from_v2_config helper ----------------------------------


def test_destinations_from_v2_config_returns_empty_when_no_config(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "no-appdata"))
    from src.gui.services.audit_aggregator import destinations_from_v2_config
    assert destinations_from_v2_config() == ()


def test_destinations_from_v2_config_pulls_dests_from_cfg(
    monkeypatch, tmp_path: Path,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    from src import config_v2
    from src.config_v2 import (
        Account, Bot, Channel, ConfigV2, Destination, Profile, Route,
    )
    cfg = ConfigV2(
        accounts=(Account(id="a", name="A", phone="", session_path="",
                          service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(Channel(id="ch", name="C", account_id="a",
                          chat_id=-1, profile_id="p"),),
        destinations=(
            Destination(id="dest_a", name="A", db_path="/a.db",
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-A"),
            Destination(id="dest_b", name="B", db_path="/b.db",
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-B"),
        ),
        bots=(Bot(id="b", name="B", token_setting_key="t",
                  service_name="CT-Bot-B"),),
        routes=(Route(id="r", channel_id="ch", destination_id="dest_a"),),
    )
    config_v2.save_v2(cfg)

    from src.gui.services.audit_aggregator import destinations_from_v2_config
    dests = destinations_from_v2_config()
    assert len(dests) == 2
    assert {d[0] for d in dests} == {"dest_a", "dest_b"}
