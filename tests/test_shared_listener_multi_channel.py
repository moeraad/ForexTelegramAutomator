"""Tests for shared_listener's Step 6 multi-channel machinery.

Mocks Telethon entirely so the tests run without network or real session
files. Verifies:

  - `_resolve_dispatch_for_channel` finds the right route/destination
    and builds an _ApiDispatchTarget with correct URL + token
  - Channels with no enabled route are skipped (returns None)
  - Multiple enabled routes log a warning and use the first one (Step 11
    deferred mirror routing)
  - Token reads from listener_shared_token, falls back to ea_shared_token
  - api_port reads from the destination DB's settings table
  - `_backfill_channel_on_startup` archives on first launch (last_seen=0)
  - `_backfill_channel_on_startup` POSTs missed messages on subsequent
    launches, age-caps stale ones, advances last_seen

Note: the full `_run_multi_channel` is integration-tested in Step 10.
Here we keep tests hermetic by mocking the Telethon client + helpers.
"""
from __future__ import annotations

import asyncio
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from src import config, config_v2
from src.config_v2 import (
    Account,
    Channel,
    ConfigV2,
    Destination,
    Route,
)
from src.db import connect, init_schema
from src.shared_listener import (
    _backfill_channel_on_startup,
    _read_destination_api_port,
    _read_destination_listener_token,
    _resolve_dispatch_for_channel,
)


def _stack_db(path: Path, *, settings: dict[str, str] | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    init_schema(conn)
    if settings:
        for k, v in settings.items():
            conn.execute(
                "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
                (k, v),
            )
        conn.commit()
    conn.close()
    return path


def _channel(channel_id: str, *, chat_id: int = -1) -> Channel:
    return Channel(
        id=channel_id, name=channel_id, account_id="acc_primary",
        chat_id=chat_id, profile_id="p", enabled=True,
    )


def _destination(dest_id: str, db_path: Path, *,
                 api_port: int = 0, api_host: str = "127.0.0.1") -> Destination:
    return Destination(
        id=dest_id, name=dest_id, db_path=str(db_path),
        api_host=api_host, api_port=api_port,
        service_name=f"CT-Api-{dest_id}",
    )


# ---- _resolve_dispatch_for_channel ----------------------------------------


def test_resolve_returns_target_with_route_and_destination(tmp_path: Path, monkeypatch):
    db = _stack_db(tmp_path / "main.db", settings={
        "api_port": "8765", "listener_shared_token": "secret123",
    })
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        destinations=(_destination("dest_a", db, api_port=8765),),
        routes=(Route(id="r1", channel_id="ch_a", destination_id="dest_a"),),
    )
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")

    # Step 11: resolver now returns a list (one per enabled route).
    result = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    assert len(result) == 1
    dest_db, target = result[0]
    assert dest_db == db
    assert target.url == "http://127.0.0.1:8765/incoming_message"
    assert target.channel_id == "ch_a"
    assert target.token == "secret123"
    assert target.route_id == "r1"
    assert target.sizing_multiplier == 1.0


def test_resolve_returns_empty_when_no_route(tmp_path: Path):
    db = _stack_db(tmp_path / "main.db")
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        destinations=(_destination("dest_a", db),),
        routes=(),  # no route for ch_a
    )
    assert _resolve_dispatch_for_channel(cfg.channels[0], cfg) == []


def test_resolve_returns_empty_when_route_disabled(tmp_path: Path):
    db = _stack_db(tmp_path / "main.db")
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        destinations=(_destination("dest_a", db),),
        routes=(Route(id="r", channel_id="ch_a", destination_id="dest_a",
                      enabled=False),),
    )
    assert _resolve_dispatch_for_channel(cfg.channels[0], cfg) == []


def test_resolve_returns_all_targets_for_mirror_route(tmp_path: Path):
    """Step 11: multi-route per channel returns all enabled targets."""
    db1 = _stack_db(tmp_path / "a.db", settings={"api_port": "8001"})
    db2 = _stack_db(tmp_path / "b.db", settings={"api_port": "8002"})
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        destinations=(
            _destination("d1", db1, api_port=8001),
            _destination("d2", db2, api_port=8002),
        ),
        routes=(
            Route(id="r1", channel_id="ch_a", destination_id="d1",
                  sizing_multiplier=1.0),
            Route(id="r2", channel_id="ch_a", destination_id="d2",
                  sizing_multiplier=0.5),
        ),
    )
    result = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    assert len(result) == 2
    routes_seen = {target.route_id: target for _, target in result}
    assert "r1" in routes_seen
    assert "r2" in routes_seen
    assert routes_seen["r1"].sizing_multiplier == 1.0
    assert routes_seen["r2"].sizing_multiplier == 0.5
    assert ":8001/" in routes_seen["r1"].url
    assert ":8002/" in routes_seen["r2"].url


def test_resolve_skips_route_with_unknown_destination(tmp_path: Path):
    """A Route pointing at a destination not in cfg.destinations is
    skipped (logged warning) so other routes still resolve."""
    db = _stack_db(tmp_path / "main.db")
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        destinations=(_destination("dest_a", db),),
        routes=(
            Route(id="r1", channel_id="ch_a", destination_id="dest_a"),
            Route(id="r2", channel_id="ch_a", destination_id="dest_missing"),
        ),
    )
    result = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    # Only the valid route resolves.
    assert len(result) == 1
    assert result[0][1].route_id == "r1"


def test_resolve_token_falls_back_to_ea_shared(tmp_path: Path, monkeypatch):
    db = _stack_db(tmp_path / "main.db", settings={
        "api_port": "8765", "ea_shared_token": "ea_secret",
    })
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        destinations=(_destination("dest_a", db, api_port=8765),),
        routes=(Route(id="r", channel_id="ch_a", destination_id="dest_a"),),
    )
    result = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    assert len(result) == 1
    _, target = result[0]
    assert target.token == "ea_secret"


def test_resolve_uses_port_from_destination_settings(tmp_path: Path):
    db = _stack_db(tmp_path / "main.db", settings={"api_port": "8766"})
    cfg = ConfigV2(
        channels=(_channel("ch_a"),),
        # api_port=0 on the Destination entity → fall back to DB settings.
        destinations=(_destination("dest_a", db, api_port=0),),
        routes=(Route(id="r", channel_id="ch_a", destination_id="dest_a"),),
    )
    result = _resolve_dispatch_for_channel(cfg.channels[0], cfg)
    assert len(result) == 1
    _, target = result[0]
    assert ":8766/incoming_message" in target.url


# ---- DB-settings readers ---------------------------------------------------


def test_read_destination_api_port_returns_int(tmp_path: Path):
    db = _stack_db(tmp_path / "x.db", settings={"api_port": "8765"})
    assert _read_destination_api_port(db) == 8765


def test_read_destination_api_port_none_on_missing_file(tmp_path: Path):
    assert _read_destination_api_port(tmp_path / "missing.db") is None


def test_read_destination_listener_token_prefers_listener_over_ea(tmp_path: Path):
    db = _stack_db(tmp_path / "x.db", settings={
        "listener_shared_token": "listener_tok",
        "ea_shared_token": "ea_tok",
    })
    assert _read_destination_listener_token(db) == "listener_tok"


def test_read_destination_listener_token_falls_back_to_ea(tmp_path: Path):
    db = _stack_db(tmp_path / "x.db", settings={"ea_shared_token": "ea_only"})
    assert _read_destination_listener_token(db) == "ea_only"


# ---- _backfill_channel_on_startup ------------------------------------------


def test_backfill_archives_on_first_launch(tmp_path: Path):
    db = _stack_db(tmp_path / "dest.db")
    from src.listener import MissedMessage, _ApiDispatchTarget

    ch = _channel("ch_a", chat_id=-42)
    target = _ApiDispatchTarget(url="x", channel_id="ch_a", token="")

    missed = [
        MissedMessage(tg_message_id=1, sender="s1", text="hi", date=datetime.now(timezone.utc)),
        MissedMessage(tg_message_id=2, sender="s2", text="bye", date=datetime.now(timezone.utc)),
    ]

    async def fake_collect(client, chat_id, min_id):
        return missed

    posted = []
    def fake_post(t, **kw):
        posted.append(kw)
        return True

    asyncio.run(_backfill_channel_on_startup(
        client=MagicMock(),
        channel=ch,
        dest_db=db,
        target=target,
        collect_missed_fn=fake_collect,
        post_message_fn=fake_post,
    ))

    # No POSTs because first launch archives without AI.
    assert posted == []
    # Messages are in the DB.
    conn = sqlite3.connect(str(db))
    rows = conn.execute("SELECT tg_message_id, source_channel_id FROM messages "
                        "WHERE chat_id=? ORDER BY tg_message_id",
                        (-42,)).fetchall()
    conn.close()
    assert [r[0] for r in rows] == [1, 2]
    assert {r[1] for r in rows} == {"ch_a"}
    # last_seen advanced.
    from src import db_settings
    assert db_settings.get_str(db, "last_seen_tg_msg_id", "0") == "2"


def test_backfill_replays_via_post_on_subsequent_launch(tmp_path: Path):
    db = _stack_db(tmp_path / "dest.db", settings={"last_seen_tg_msg_id": "5"})
    from src.listener import MissedMessage, _ApiDispatchTarget

    ch = _channel("ch_a", chat_id=-42)
    target = _ApiDispatchTarget(url="http://x", channel_id="ch_a", token="t")
    fresh_msg = MissedMessage(
        tg_message_id=10, sender="s", text="fresh",
        date=datetime.now(timezone.utc),
    )

    async def fake_collect(client, chat_id, min_id):
        return [fresh_msg]

    posted = []
    def fake_post(t, **kw):
        posted.append(kw)
        return True

    asyncio.run(_backfill_channel_on_startup(
        client=MagicMock(),
        channel=ch,
        dest_db=db,
        target=target,
        collect_missed_fn=fake_collect,
        post_message_fn=fake_post,
    ))

    assert len(posted) == 1
    assert posted[0]["tg_chat_id"] == -42
    assert posted[0]["tg_message_id"] == 10
    assert posted[0]["is_backfill"] is True
    from src import db_settings
    assert db_settings.get_str(db, "last_seen_tg_msg_id", "0") == "10"


def test_backfill_skips_stale_messages_by_age_cap(tmp_path: Path, monkeypatch):
    db = _stack_db(tmp_path / "dest.db", settings={"last_seen_tg_msg_id": "5"})
    # Pin the age cap to 1 minute so stale-vs-fresh is unambiguous.
    monkeypatch.setattr(config, "BACKFILL_MAX_AGE_MIN", 1)
    from src.listener import MissedMessage, _ApiDispatchTarget

    ch = _channel("ch_a", chat_id=-42)
    target = _ApiDispatchTarget(url="x", channel_id="ch_a", token="")
    now = datetime.now(timezone.utc)
    stale = MissedMessage(tg_message_id=8, sender="s", text="old",
                          date=now - timedelta(minutes=30))
    fresh = MissedMessage(tg_message_id=10, sender="s", text="new", date=now)

    async def fake_collect(client, chat_id, min_id):
        return [stale, fresh]

    posted = []
    def fake_post(t, **kw):
        posted.append(kw["tg_message_id"])
        return True

    asyncio.run(_backfill_channel_on_startup(
        client=MagicMock(),
        channel=ch,
        dest_db=db,
        target=target,
        collect_missed_fn=fake_collect,
        post_message_fn=fake_post,
    ))

    assert posted == [10]  # stale one dropped
    from src import db_settings
    # last_seen advances to the MAX collected (even the stale one) so we
    # don't re-fetch it on next startup.
    assert db_settings.get_str(db, "last_seen_tg_msg_id", "0") == "10"


def test_backfill_noop_when_no_missed(tmp_path: Path):
    db = _stack_db(tmp_path / "dest.db", settings={"last_seen_tg_msg_id": "5"})
    from src.listener import _ApiDispatchTarget

    ch = _channel("ch_a", chat_id=-42)
    target = _ApiDispatchTarget(url="x", channel_id="ch_a", token="")

    async def fake_collect(client, chat_id, min_id):
        return []

    def fake_post(t, **kw):
        raise AssertionError("should not POST on empty missed")

    asyncio.run(_backfill_channel_on_startup(
        client=MagicMock(),
        channel=ch,
        dest_db=db,
        target=target,
        collect_missed_fn=fake_collect,
        post_message_fn=fake_post,
    ))
    from src import db_settings
    # last_seen unchanged.
    assert db_settings.get_str(db, "last_seen_tg_msg_id", "0") == "5"
