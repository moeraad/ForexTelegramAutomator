"""Bot binding scopes tests (Step 14 of multi-channel plan).

Validates the full four-scope dispatcher + multi-destination tailer:

  - ``ConfigV2.bindings_for_destination`` returns the union of global,
    destination-matching, channel-routes-here, route-targets-here.
  - ``dispatch_notification`` further filters channel/route bindings by
    the event's actual ``source_channel_id`` / ``route_id``.
  - ``ConfigV2.destinations_for_bot`` derives the dest set a bot must
    tail (global → all, destination → one, channel → its destinations
    via routes, route → that route's dest).
  - ``OutboxTailer(conns=[...])`` polls multiple DBs, delivers each row
    against the SAME conn it came from (so ``delivered_at`` lands in
    the right DB).
"""
from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from src import config_v2
from src.bot_outbox_tailer import OutboxTailer
from src.config_v2 import (
    Account,
    Bot,
    BotBinding,
    Channel,
    ConfigV2,
    Destination,
    Profile,
    Route,
)
from src.db import connect, init_schema
from src.notification_dispatcher import dispatch_notification


# ---- Fixtures -------------------------------------------------------------


def _two_dest_cfg(tmp_path: Path) -> tuple[ConfigV2, Path, Path]:
    """ch_a → dest_x; ch_a → dest_y (mirror).  ch_b → dest_x only."""
    db_x = tmp_path / "dest_x.db"
    db_y = tmp_path / "dest_y.db"
    cfg = ConfigV2(
        accounts=(Account(id="a", name="A", phone="",
                          session_path="", service_name="CT-Listener-a"),),
        profiles=(Profile(id="p", name="P", path="/p.json",
                          language="en", symbol="XAUUSD"),),
        channels=(
            Channel(id="ch_a", name="A", account_id="a",
                    chat_id=-1, profile_id="p"),
            Channel(id="ch_b", name="B", account_id="a",
                    chat_id=-2, profile_id="p"),
        ),
        destinations=(
            Destination(id="dest_x", name="X", db_path=str(db_x),
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-X"),
            Destination(id="dest_y", name="Y", db_path=str(db_y),
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-Y"),
        ),
        bots=(
            Bot(id="bot_main", name="Main", token_setting_key="t1",
                service_name="CT-Bot-Main"),
            Bot(id="bot_alert", name="Alert", token_setting_key="t2",
                service_name="CT-Bot-Alert"),
            Bot(id="bot_global", name="Global", token_setting_key="t3",
                service_name="CT-Bot-Global"),
            Bot(id="bot_route_only", name="Route", token_setting_key="t4",
                service_name="CT-Bot-Route"),
        ),
        routes=(
            Route(id="r_ax", channel_id="ch_a", destination_id="dest_x"),
            Route(id="r_ay", channel_id="ch_a", destination_id="dest_y"),
            Route(id="r_bx", channel_id="ch_b", destination_id="dest_x"),
        ),
        bot_bindings=(
            BotBinding(id="bind_main", bot_id="bot_main",
                       scope="destination", destination_id="dest_x"),
            BotBinding(id="bind_alert", bot_id="bot_alert",
                       scope="channel", channel_id="ch_a"),
            BotBinding(id="bind_global", bot_id="bot_global",
                       scope="global"),
            BotBinding(id="bind_route", bot_id="bot_route_only",
                       scope="route", route_id="r_ay"),
        ),
    )
    return cfg, db_x, db_y


# ---- bindings_for_destination: candidate-set widening --------------------


def test_bindings_for_destination_includes_destination_scope_match(tmp_path: Path):
    cfg, _, _ = _two_dest_cfg(tmp_path)
    ids = {b.id for b in cfg.bindings_for_destination("dest_x")}
    assert "bind_main" in ids


def test_bindings_for_destination_includes_global(tmp_path: Path):
    cfg, _, _ = _two_dest_cfg(tmp_path)
    for dest_id in ("dest_x", "dest_y"):
        ids = {b.id for b in cfg.bindings_for_destination(dest_id)}
        assert "bind_global" in ids


def test_bindings_for_destination_includes_channel_route_through(tmp_path: Path):
    """A scope=channel binding for ch_a must surface on BOTH dest_x and
    dest_y because ch_a mirrors to both."""
    cfg, _, _ = _two_dest_cfg(tmp_path)
    ids_x = {b.id for b in cfg.bindings_for_destination("dest_x")}
    ids_y = {b.id for b in cfg.bindings_for_destination("dest_y")}
    assert "bind_alert" in ids_x
    assert "bind_alert" in ids_y


def test_bindings_for_destination_excludes_channel_not_routing_here(tmp_path: Path):
    """If we add a scope=channel binding for ch_b (which only routes to
    dest_x), it must NOT surface on dest_y."""
    cfg, _, _ = _two_dest_cfg(tmp_path)
    extra_binding = BotBinding(id="bind_b_only", bot_id="bot_alert",
                               scope="channel", channel_id="ch_b")
    cfg = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels, destinations=cfg.destinations,
        bots=cfg.bots, routes=cfg.routes,
        bot_bindings=cfg.bot_bindings + (extra_binding,),
    )
    ids_y = {b.id for b in cfg.bindings_for_destination("dest_y")}
    assert "bind_b_only" not in ids_y
    ids_x = {b.id for b in cfg.bindings_for_destination("dest_x")}
    assert "bind_b_only" in ids_x


def test_bindings_for_destination_includes_route_targeting_here(tmp_path: Path):
    """bind_route → r_ay → dest_y only."""
    cfg, _, _ = _two_dest_cfg(tmp_path)
    ids_y = {b.id for b in cfg.bindings_for_destination("dest_y")}
    assert "bind_route" in ids_y
    ids_x = {b.id for b in cfg.bindings_for_destination("dest_x")}
    assert "bind_route" not in ids_x


def test_bindings_for_destination_skips_disabled_routes(tmp_path: Path):
    """Channel/route scope only surfaces for ENABLED routes — disabled
    routes don't cause notifications."""
    cfg, _, _ = _two_dest_cfg(tmp_path)
    # Disable r_ay → ch_a no longer routes to dest_y → bind_alert should
    # no longer surface on dest_y. (It still surfaces on dest_x via r_ax.)
    new_routes = tuple(
        Route(id=r.id, channel_id=r.channel_id,
              destination_id=r.destination_id,
              enabled=False if r.id == "r_ay" else r.enabled,
              halted=r.halted, sizing_multiplier=r.sizing_multiplier)
        for r in cfg.routes
    )
    cfg2 = ConfigV2(
        accounts=cfg.accounts, profiles=cfg.profiles,
        channels=cfg.channels, destinations=cfg.destinations,
        bots=cfg.bots, routes=new_routes,
        bot_bindings=cfg.bot_bindings,
    )
    ids_y = {b.id for b in cfg2.bindings_for_destination("dest_y")}
    # bind_alert removed via route disable, bind_route removed via same
    assert "bind_alert" not in ids_y
    assert "bind_route" not in ids_y
    # dest_x still gets it (r_ax is enabled).
    ids_x = {b.id for b in cfg2.bindings_for_destination("dest_x")}
    assert "bind_alert" in ids_x


# ---- destinations_for_bot: which DBs does a bot need to tail? ------------


def test_destinations_for_bot_global_returns_all(tmp_path: Path):
    cfg, _, _ = _two_dest_cfg(tmp_path)
    dests = cfg.destinations_for_bot("bot_global")
    assert {d.id for d in dests} == {"dest_x", "dest_y"}


def test_destinations_for_bot_destination_scope_returns_one(tmp_path: Path):
    cfg, _, _ = _two_dest_cfg(tmp_path)
    dests = cfg.destinations_for_bot("bot_main")
    assert {d.id for d in dests} == {"dest_x"}


def test_destinations_for_bot_channel_scope_returns_route_destinations(tmp_path: Path):
    """bind_alert → ch_a → r_ax + r_ay → dest_x + dest_y."""
    cfg, _, _ = _two_dest_cfg(tmp_path)
    dests = cfg.destinations_for_bot("bot_alert")
    assert {d.id for d in dests} == {"dest_x", "dest_y"}


def test_destinations_for_bot_route_scope_returns_one(tmp_path: Path):
    cfg, _, _ = _two_dest_cfg(tmp_path)
    dests = cfg.destinations_for_bot("bot_route_only")
    assert {d.id for d in dests} == {"dest_y"}


def test_destinations_for_bot_unknown_id_returns_empty(tmp_path: Path):
    cfg, _, _ = _two_dest_cfg(tmp_path)
    assert cfg.destinations_for_bot("bot_nope") == ()


# ---- dispatch_notification: per-event scope filtering --------------------


def _seed_dest_db(db_path: Path) -> sqlite3.Connection:
    conn = connect(str(db_path))
    init_schema(conn)
    return conn


def _seed_action(conn: sqlite3.Connection) -> int:
    """Seed one action row so dispatch_notification's FK to actions(id) holds."""
    cur = conn.execute(
        "INSERT INTO actions (action_type, payload_json, status) "
        "VALUES ('OPEN', '{}', 'executed')",
    )
    conn.commit()
    return cur.lastrowid


def test_dispatch_writes_for_channel_binding_when_event_matches(
    monkeypatch, tmp_path: Path,
):
    """ch_a-scoped binding should fire when event.source_channel_id == ch_a."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg, db_x, db_y = _two_dest_cfg(tmp_path)
    config_v2.save_v2(cfg, tmp_path / "appdata" / "CopyTrades" / "stacks_config.json")
    conn = _seed_dest_db(db_x)
    aid = _seed_action(conn)

    n = dispatch_notification(
        conn, event_type="action_terminal", action_id=aid,
        db_path=str(db_x), source_channel_id="ch_a", route_id="r_ax",
    )
    # Expected: bind_main (destination), bind_alert (channel ch_a),
    # bind_global (global). bind_route binds to r_ay which doesn't
    # surface on dest_x.
    bot_ids = {
        r["bot_id"] for r in conn.execute(
            "SELECT bot_id FROM bot_outbox").fetchall()
    }
    assert bot_ids == {"bot_main", "bot_alert", "bot_global"}
    assert n == 3


def test_dispatch_skips_channel_binding_when_event_channel_differs(
    monkeypatch, tmp_path: Path,
):
    """ch_a-scoped binding must NOT fire for a ch_b event on dest_x."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg, db_x, _ = _two_dest_cfg(tmp_path)
    config_v2.save_v2(cfg, tmp_path / "appdata" / "CopyTrades" / "stacks_config.json")
    conn = _seed_dest_db(db_x)
    aid = _seed_action(conn)

    dispatch_notification(
        conn, event_type="action_terminal", action_id=aid,
        db_path=str(db_x), source_channel_id="ch_b", route_id="r_bx",
    )
    bot_ids = {
        r["bot_id"] for r in conn.execute(
            "SELECT bot_id FROM bot_outbox").fetchall()
    }
    # bind_alert (channel=ch_a) is skipped because event is ch_b.
    assert "bot_alert" not in bot_ids
    # destination + global still fire.
    assert {"bot_main", "bot_global"} <= bot_ids


def test_dispatch_writes_for_route_binding_when_event_route_matches(
    monkeypatch, tmp_path: Path,
):
    """bind_route is scoped to r_ay; only fires on dest_y events with route_id=r_ay."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg, _, db_y = _two_dest_cfg(tmp_path)
    config_v2.save_v2(cfg, tmp_path / "appdata" / "CopyTrades" / "stacks_config.json")
    conn = _seed_dest_db(db_y)
    aid = _seed_action(conn)

    dispatch_notification(
        conn, event_type="action_terminal", action_id=aid,
        db_path=str(db_y), source_channel_id="ch_a", route_id="r_ay",
    )
    bot_ids = {
        r["bot_id"] for r in conn.execute(
            "SELECT bot_id FROM bot_outbox").fetchall()
    }
    assert "bot_route_only" in bot_ids


def test_dispatch_skips_route_binding_when_route_id_blank(
    monkeypatch, tmp_path: Path,
):
    """Event with no route_id (legacy / single-dest) can't match route-scoped binding."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    cfg, _, db_y = _two_dest_cfg(tmp_path)
    config_v2.save_v2(cfg, tmp_path / "appdata" / "CopyTrades" / "stacks_config.json")
    conn = _seed_dest_db(db_y)
    aid = _seed_action(conn)

    dispatch_notification(
        conn, event_type="action_terminal", action_id=aid,
        db_path=str(db_y), source_channel_id="ch_a", route_id=None,
    )
    bot_ids = {
        r["bot_id"] for r in conn.execute(
            "SELECT bot_id FROM bot_outbox").fetchall()
    }
    assert "bot_route_only" not in bot_ids
    # Global still fires (no scope filter on it).
    assert "bot_global" in bot_ids


# ---- OutboxTailer multi-conn mode ----------------------------------------


def _seed_outbox_row(
    conn: sqlite3.Connection, *,
    bot_id: str, event_type: str = "alert",
    payload: str = '{"level": "info", "text": "hi"}',
) -> int:
    cur = conn.execute(
        "INSERT INTO bot_outbox (bot_id, event_type, event_payload, action_id) "
        "VALUES (?, ?, ?, NULL)",
        (bot_id, event_type, payload),
    )
    conn.commit()
    return cur.lastrowid


@pytest.mark.asyncio
async def test_tailer_multi_conn_polls_all_dbs(tmp_path: Path):
    """One bot, two DBs, one row in each — the tailer delivers both."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    conn_a = _seed_dest_db(db_a)
    conn_b = _seed_dest_db(db_b)
    _seed_outbox_row(conn_a, bot_id="bot_x")
    _seed_outbox_row(conn_b, bot_id="bot_x")

    send_calls: list = []

    async def _send(chat_id, text, **kwargs):
        send_calls.append((chat_id, text))

    tailer = OutboxTailer(
        bot_id="bot_x",
        conns=[conn_a, conn_b],
        owner_chat_id=12345,
        send_message_fn=_send,
    )
    await tailer._tick()

    assert len(send_calls) == 2
    # Each conn marked its own row delivered.
    row_a = conn_a.execute(
        "SELECT delivered_at FROM bot_outbox").fetchone()
    row_b = conn_b.execute(
        "SELECT delivered_at FROM bot_outbox").fetchone()
    assert row_a["delivered_at"] is not None
    assert row_b["delivered_at"] is not None


@pytest.mark.asyncio
async def test_tailer_multi_conn_isolated_failure(tmp_path: Path):
    """If send fails for conn A's row, conn B's row still gets delivered."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    conn_a = _seed_dest_db(db_a)
    conn_b = _seed_dest_db(db_b)
    _seed_outbox_row(conn_a, bot_id="bot_x", payload='{"level": "info", "text": "from-A"}')
    _seed_outbox_row(conn_b, bot_id="bot_x", payload='{"level": "info", "text": "from-B"}')

    async def _send(chat_id, text, **kwargs):
        if "from-A" in text:
            raise RuntimeError("simulated send failure")
        return None

    tailer = OutboxTailer(
        bot_id="bot_x",
        conns=[conn_a, conn_b],
        owner_chat_id=12345,
        send_message_fn=_send,
    )
    await tailer._tick()
    # conn_a's row stays undelivered (retry on next tick).
    assert conn_a.execute(
        "SELECT delivered_at FROM bot_outbox").fetchone()["delivered_at"] is None
    # conn_b's row was delivered.
    assert conn_b.execute(
        "SELECT delivered_at FROM bot_outbox").fetchone()["delivered_at"] is not None


def test_tailer_init_rejects_both_conn_and_conns(tmp_path: Path):
    db = tmp_path / "x.db"
    conn = _seed_dest_db(db)
    with pytest.raises(ValueError, match="conn=|conns="):
        OutboxTailer(
            bot_id="b", conn=conn, conns=[conn],
            owner_chat_id=1, send_message_fn=AsyncMock(),
        )


def test_tailer_init_rejects_neither_conn_nor_conns():
    with pytest.raises(ValueError, match="conn=|conns="):
        OutboxTailer(
            bot_id="b", owner_chat_id=1, send_message_fn=AsyncMock(),
        )


def test_tailer_mark_existing_delivered_across_conns(tmp_path: Path):
    """mark_existing_delivered must suppress backlog in ALL connected DBs."""
    db_a = tmp_path / "a.db"
    db_b = tmp_path / "b.db"
    conn_a = _seed_dest_db(db_a)
    conn_b = _seed_dest_db(db_b)
    _seed_outbox_row(conn_a, bot_id="bot_x")
    _seed_outbox_row(conn_b, bot_id="bot_x")
    _seed_outbox_row(conn_b, bot_id="other_bot")  # different bot, must NOT be touched

    tailer = OutboxTailer(
        bot_id="bot_x",
        conns=[conn_a, conn_b],
        owner_chat_id=1,
        send_message_fn=AsyncMock(),
    )
    suppressed = tailer.mark_existing_delivered()
    assert suppressed == 2
    # other_bot row stays undelivered.
    other_row = conn_b.execute(
        "SELECT delivered_at FROM bot_outbox WHERE bot_id='other_bot'"
    ).fetchone()
    assert other_row["delivered_at"] is None
