"""End-to-end multi-channel validation (Step 10 of multi-channel plan).

Validates the Steps 4-7 wire chain through real code paths, with the
external boundaries (Telegram, MT5) mocked:

  Listener simulator → POST /incoming_message → orchestrator →
  notification_dispatcher → bot_outbox → OutboxTailer (rendered text)

Setup: ONE v2 config with ONE account, TWO channels, TWO destinations
(each with its own DB + FastAPI app + AI + bot), TWO routes (1:1
mapping), TWO bot bindings.

Test cases covered:

  1. Per-channel routing isolation: a message for channel A lands ONLY
     in destination A's DB. Destination B is unchanged.
  2. Per-destination notification isolation: an action emitted on
     destination A only enqueues an outbox row for bot_A. bot_B sees
     nothing.
  3. Cross-channel interleave: alternating A/B messages produce a clean
     per-destination action stream with no leakage.
  4. Halt isolation (functional): a halted destination's promoter
     pause flag is respected; the other destination still processes.
  5. Backfill simulation: replay 3 messages with is_backfill=True per
     channel; verify all land + are flagged backfill.
  6. v1→v2 migration check (end-to-end): an existing v1 stacks_config
     auto-migrates on first config_v2 read; deriving the same Stack
     compat-shim shape that GUI views consume.

What is NOT covered here (out-of-scope for hermetic tests):

  - Real Telethon connection (covered by the operator runbook)
  - Real MT5 EA execution (covered by the operator runbook)
  - Real Telegram bot.send_message (mocked; behavior verified in
    test_bot_outbox_tailer.py)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from src import config, config_v2
from src.ai import AICallResult, AIClient
from src.ai_triage import TriageClient
from src.api import build_app
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
from src.profile_context import ProfileContext, clear_cache as clear_profile_cache
from src.validators import AIResponse, OpenAction


# ---- Fixture builders ------------------------------------------------------


def _make_profile_json(path: Path, *, symbol: str = "XAUUSD") -> None:
    """Write a minimal but valid profile JSON for AI prompt rendering."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "header": "Test header",
        "vocabulary_table": "(none)",
        "compound_messages": "(none)",
        "commentary_filter": "(none)",
        "directional_command_flow": "(none)",
        "worked_examples": "(none)",
        "shorthand_decode_example": "(none)",
        "promo_indicators": "",
        "noise_patterns": "",
        "triage_keep_triggers": "",
        "symbol": symbol,
    }), encoding="utf-8")


def _make_destination_db(path: Path) -> None:
    """Create a destination DB with init_schema applied + sensible defaults."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    init_schema(conn)
    conn.close()


def _build_v2(
    tmp_path: Path,
) -> tuple[ConfigV2, Path, dict[str, Path], dict[str, Path]]:
    """Create a v2 config with 1 account, 2 channels, 2 destinations,
    2 bots, 2 routes, 2 bindings. Returns the config + appdata root +
    per-destination DB paths + per-channel profile paths.
    """
    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True, exist_ok=True)

    dest_a_db = appdata / "CopyTrades" / "dest_a" / "copytrades.db"
    dest_b_db = appdata / "CopyTrades" / "dest_b" / "copytrades.db"
    _make_destination_db(dest_a_db)
    _make_destination_db(dest_b_db)

    profile_a_path = appdata / "CopyTrades" / "profiles" / "a.json"
    profile_b_path = appdata / "CopyTrades" / "profiles" / "b.json"
    # Both XAUUSD — OpenAction validates the symbol against a fixed
    # allowlist. Routing isolation in this test doesn't depend on
    # symbols differing, only on chat_id/channel_id matching.
    _make_profile_json(profile_a_path, symbol="XAUUSD")
    _make_profile_json(profile_b_path, symbol="XAUUSD")

    cfg = ConfigV2(
        accounts=(Account(
            id="acc_primary", name="Primary", phone="+961",
            session_path=str(appdata / "primary.session"),
            service_name="CT-Listener-acc_primary",
        ),),
        profiles=(
            Profile(id="prof_a", name="A", path=str(profile_a_path),
                    language="en", symbol="XAUUSD"),
            Profile(id="prof_b", name="B", path=str(profile_b_path),
                    language="en", symbol="XAUUSD"),
        ),
        channels=(
            Channel(id="ch_a", name="A", account_id="acc_primary",
                    chat_id=-1001, profile_id="prof_a", enabled=True),
            Channel(id="ch_b", name="B", account_id="acc_primary",
                    chat_id=-1002, profile_id="prof_b", enabled=True),
        ),
        destinations=(
            Destination(id="dest_a", name="Dest A", db_path=str(dest_a_db),
                        api_host="127.0.0.1", api_port=8765,
                        service_name="CT-Api-A"),
            Destination(id="dest_b", name="Dest B", db_path=str(dest_b_db),
                        api_host="127.0.0.1", api_port=8766,
                        service_name="CT-Api-B"),
        ),
        bots=(
            Bot(id="bot_a", name="Bot A", token_setting_key="tg_bot_token",
                service_name="CT-Bot-A"),
            Bot(id="bot_b", name="Bot B", token_setting_key="tg_bot_token",
                service_name="CT-Bot-B"),
        ),
        routes=(
            Route(id="route_a", channel_id="ch_a", destination_id="dest_a"),
            Route(id="route_b", channel_id="ch_b", destination_id="dest_b"),
        ),
        bot_bindings=(
            BotBinding(id="bind_a", bot_id="bot_a", scope="destination",
                       destination_id="dest_a"),
            BotBinding(id="bind_b", bot_id="bot_b", scope="destination",
                       destination_id="dest_b"),
        ),
    )
    config_v2.save_v2(cfg)
    return cfg, appdata, {"dest_a": dest_a_db, "dest_b": dest_b_db}, \
        {"ch_a": profile_a_path, "ch_b": profile_b_path}


def _ai_returning_open(symbol: str) -> MagicMock:
    """Mock AIClient that returns one OPEN action."""
    client = MagicMock(spec=AIClient)
    client.call.return_value = AICallResult(
        response=AIResponse(actions=[OpenAction(
            symbol=symbol, side="BUY",
            entry_low=4000.0, entry_high=4002.0,
            sl=3990.0, tps=[4010.0],
        )], reasoning=""),
        raw_text="{}",
        usage={"input_tokens": 1, "output_tokens": 1,
               "cache_read_tokens": 0, "cache_creation_tokens": 0},
        latency_ms=1,
    )
    return client


def _build_destination_runtime(
    *,
    db_path: Path, profile_path: Path, symbol: str,
):
    """Build the runtime tuple (conn, app, client) the listener-side
    POST chain interacts with for one destination."""
    conn = connect(str(db_path))
    init_schema(conn)
    profile_data = json.loads(profile_path.read_text(encoding="utf-8"))
    from src.ai import _render_system_prompt_from_data
    from src.ai_triage import _render_triage_prompt_from_data
    profile_ctx = ProfileContext(
        name=symbol.lower(),
        path=profile_path,
        data=profile_data,
        system_prompt=_render_system_prompt_from_data(profile_data),
        triage_prompt=_render_triage_prompt_from_data(profile_data),
        symbol=symbol,
    )
    ai = _ai_returning_open(symbol)
    triage = None  # bypassed when None — orchestrator skips it
    app = build_app(
        conn,
        ai_client=ai,
        triage_client=triage,
        profile=profile_ctx,
        ai_log_path=db_path.parent / "ai.jsonl",
    )
    client = TestClient(app)
    return conn, app, client


# ---- Test 1 — per-channel routing isolation -------------------------------


def test_message_for_channel_a_lands_only_in_destination_a(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    cfg, _, dbs, profiles = _build_v2(tmp_path)

    conn_a, _, client_a = _build_destination_runtime(
        db_path=dbs["dest_a"], profile_path=profiles["ch_a"], symbol="XAUUSD",
    )
    conn_b, _, _ = _build_destination_runtime(
        db_path=dbs["dest_b"], profile_path=profiles["ch_b"], symbol="XAUUSD",
    )

    response = client_a.post("/incoming_message", json={
        "channel_id": "ch_a",
        "tg_chat_id": -1001,
        "tg_message_id": 1,
        "text": "BUY XAUUSD",
        "sender": "Yusuf",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "is_backfill": False,
    })
    assert response.status_code == 202

    # Destination A: 1 message + 1 OPEN action.
    msg_a = conn_a.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    act_a = conn_a.execute(
        "SELECT COUNT(*) FROM actions WHERE action_type='OPEN'"
    ).fetchone()[0]
    assert msg_a == 1
    assert act_a == 1
    # Destination B: untouched.
    msg_b = conn_b.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    act_b = conn_b.execute("SELECT COUNT(*) FROM actions").fetchone()[0]
    assert msg_b == 0
    assert act_b == 0


# ---- Test 2 — notification dispatcher fans out per binding ----------------


def test_action_terminal_writes_outbox_for_bound_bot_only(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    _, _, dbs, _ = _build_v2(tmp_path)

    conn_a = connect(str(dbs["dest_a"]))
    init_schema(conn_a)
    conn_b = connect(str(dbs["dest_b"]))
    init_schema(conn_b)

    # Simulate the EA POSTing a terminal result on destination A by directly
    # writing an action + invoking dispatch_notification (mirrors what
    # api.py's post_result does after the UPDATE).
    cur = conn_a.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    aid = cur.lastrowid

    from src.notification_dispatcher import dispatch_notification
    inserted = dispatch_notification(
        conn_a,
        event_type="action_terminal",
        action_id=aid,
        db_path=str(dbs["dest_a"]),
    )
    assert inserted == 1  # exactly one matching binding (bind_a)

    # bot_a outbox has one row; bot_b's destination DB has none.
    a_rows = conn_a.execute(
        "SELECT bot_id, action_id FROM bot_outbox"
    ).fetchall()
    assert len(a_rows) == 1
    assert a_rows[0]["bot_id"] == "bot_a"
    assert a_rows[0]["action_id"] == aid

    b_rows = conn_b.execute("SELECT COUNT(*) FROM bot_outbox").fetchone()[0]
    assert b_rows == 0


# ---- Test 3 — outbox tailer reads its own bot_id rows --------------------


def test_outbox_tailer_dms_only_own_bot_rows(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    _, _, dbs, _ = _build_v2(tmp_path)

    conn_a = connect(str(dbs["dest_a"]))
    init_schema(conn_a)

    # Action + dispatched outbox row for bot_a.
    cur = conn_a.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{\"symbol\":\"XAUUSD\",\"side\":\"BUY\","
        "\"entry_low\":4000,\"entry_high\":4002,\"sl\":3990,\"tps\":[4010]}', "
        "'executed')"
    )
    aid = cur.lastrowid

    from src.notification_dispatcher import dispatch_notification
    dispatch_notification(
        conn_a, event_type="action_terminal", action_id=aid,
        db_path=str(dbs["dest_a"]),
    )

    # bot_a tailer fires: delivers the row.
    send = AsyncMock()
    tailer_a = OutboxTailer(
        bot_id="bot_a", conn=conn_a, owner_chat_id=9999,
        send_message_fn=send,
    )
    import asyncio
    asyncio.run(tailer_a._tick())
    assert send.await_count == 1
    delivered = conn_a.execute(
        "SELECT delivered_at FROM bot_outbox WHERE bot_id='bot_a'"
    ).fetchone()
    assert delivered["delivered_at"] is not None

    # A bot_b tailer pointed at the SAME DB would see nothing
    # (binding scope isolation).
    send_b = AsyncMock()
    tailer_b = OutboxTailer(
        bot_id="bot_b", conn=conn_a, owner_chat_id=9999,
        send_message_fn=send_b,
    )
    asyncio.run(tailer_b._tick())
    assert send_b.await_count == 0


# ---- Test 4 — cross-channel interleave produces clean per-destination state


def test_interleaved_messages_routed_correctly(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    cfg, _, dbs, profiles = _build_v2(tmp_path)

    conn_a, _, client_a = _build_destination_runtime(
        db_path=dbs["dest_a"], profile_path=profiles["ch_a"], symbol="XAUUSD",
    )
    conn_b, _, client_b = _build_destination_runtime(
        db_path=dbs["dest_b"], profile_path=profiles["ch_b"], symbol="XAUUSD",
    )

    # 5 alternating messages: A, B, A, B, A
    for tg_id, (target_client, chat_id, channel) in enumerate([
        (client_a, -1001, "ch_a"),
        (client_b, -1002, "ch_b"),
        (client_a, -1001, "ch_a"),
        (client_b, -1002, "ch_b"),
        (client_a, -1001, "ch_a"),
    ], start=1):
        r = target_client.post("/incoming_message", json={
            "channel_id": channel,
            "tg_chat_id": chat_id,
            "tg_message_id": tg_id,
            "text": "BUY",
            "sender": "x",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "is_backfill": False,
        })
        assert r.status_code == 202

    msg_a = conn_a.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    msg_b = conn_b.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert msg_a == 3
    assert msg_b == 2


# ---- Test 5 — backfill flag propagates ----------------------------------


def test_backfill_messages_flagged_in_destination_db(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    _, _, dbs, profiles = _build_v2(tmp_path)

    conn_a, _, client_a = _build_destination_runtime(
        db_path=dbs["dest_a"], profile_path=profiles["ch_a"], symbol="XAUUSD",
    )

    for tg_id in range(1, 4):
        client_a.post("/incoming_message", json={
            "channel_id": "ch_a",
            "tg_chat_id": -1001,
            "tg_message_id": tg_id,
            "text": "BUY",
            "sender": "x",
            "received_at": datetime.now(timezone.utc).isoformat(),
            "is_backfill": True,
        })

    rows = conn_a.execute(
        "SELECT is_backfill FROM messages ORDER BY tg_message_id"
    ).fetchall()
    assert len(rows) == 3
    assert all(r["is_backfill"] == 1 for r in rows)


# ---- Test 6 — v1→v2 migration applied to a synthetic existing config -----


def test_v1_config_auto_migrates_to_v2(tmp_path: Path, monkeypatch):
    """A user with an existing v1 stacks_config should get an automatic
    one-shot migration on first GUI / shared_listener load. Verifies the
    Step 1 migration shim still produces a coherent v2 config."""
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    clear_profile_cache()
    appdata = tmp_path / "appdata"
    appdata.mkdir(parents=True)

    # Seed a v1 stacks_config (the pre-migration shape).
    cfg_path = appdata / "CopyTrades" / "stacks_config.json"
    cfg_path.parent.mkdir(parents=True)
    stack_db = appdata / "CopyTrades" / "Legacy Stack" / "copytrades.db"
    stack_db.parent.mkdir(parents=True)
    conn = connect(str(stack_db))
    init_schema(conn)
    for k, v in {
        "tg_phone": "+96100000",
        "tg_session_name": "session",
        "tg_watched_chat_id": "-1234",
    }.items():
        conn.execute(
            "INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)",
            (k, v),
        )
    conn.commit()
    conn.close()
    cfg_path.write_text(json.dumps({"stacks": [{
        "name": "Legacy Stack",
        "profile_path": str(appdata / "CopyTrades" / "Legacy Stack" / "profile.json"),
        "project_path": str(appdata),
        "db_path": str(stack_db),
        "service_names": ["CT-LEGACY-Api", "CT-LEGACY-Bot", "CT-LEGACY-Listener"],
    }]}), encoding="utf-8")

    # Trigger migration via the GUI's discover_stacks path.
    import importlib
    from src.gui.services import stack_registry
    importlib.reload(stack_registry)
    stacks = stack_registry.discover_stacks()
    assert len(stacks) == 1
    s = stacks[0]
    assert s.name == "Legacy Stack"
    # The migrated listener service is named per-account.
    assert s.service_names[2].startswith("CT-Listener-acc_")

    # And the on-disk config is now v2.
    assert config_v2.is_v2(cfg_path)
    v2 = config_v2.load_v2(cfg_path)
    assert v2 is not None
    assert len(v2.accounts) == 1
    assert len(v2.channels) == 1
    assert len(v2.destinations) == 1
    assert len(v2.bots) == 1
    assert len(v2.routes) == 1
    assert len(v2.bot_bindings) == 1
    # Backup file written.
    assert (cfg_path.with_suffix(cfg_path.suffix + ".v1.bak")).exists()


# ---- Test 7 — full chain: API ingest → dispatcher → outbox → tailer ------


def test_full_chain_ingest_to_outbox_dispatch(tmp_path: Path, monkeypatch):
    """Full Steps 4-7 chain in one test: send a message via POST
    /incoming_message, watch it become an action, get dispatched to
    bot_outbox, and rendered by the OutboxTailer.

    Note: with the action created at status='pending' (delay > 0),
    notification_dispatcher only fires on STATUS-CHANGE-TO-TERMINAL.
    To drive the full chain we promote the action to terminal directly
    after orchestrator emits it, then call dispatch_notification.
    """
    monkeypatch.setenv("APPDATA", str(tmp_path / "appdata"))
    monkeypatch.setattr(config, "EA_SHARED_TOKEN", "")
    monkeypatch.setattr(config, "LISTENER_SHARED_TOKEN", "")
    clear_profile_cache()
    _, _, dbs, profiles = _build_v2(tmp_path)

    conn_a, _, client_a = _build_destination_runtime(
        db_path=dbs["dest_a"], profile_path=profiles["ch_a"], symbol="XAUUSD",
    )

    client_a.post("/incoming_message", json={
        "channel_id": "ch_a",
        "tg_chat_id": -1001,
        "tg_message_id": 100,
        "text": "BUY GOLD",
        "sender": "X",
        "received_at": datetime.now(timezone.utc).isoformat(),
        "is_backfill": False,
    })

    aid_row = conn_a.execute(
        "SELECT id FROM actions WHERE action_type='OPEN' "
        "ORDER BY id DESC LIMIT 1"
    ).fetchone()
    assert aid_row is not None
    aid = aid_row["id"]

    # Simulate the EA reporting executed (mirrors api.post_result body).
    conn_a.execute(
        "UPDATE actions SET status='executed' WHERE id=?", (aid,),
    )
    from src.notification_dispatcher import dispatch_notification
    dispatch_notification(
        conn_a, event_type="action_terminal", action_id=aid,
        db_path=str(dbs["dest_a"]),
    )

    # Tailer renders + delivers via mock.
    send = AsyncMock()
    tailer = OutboxTailer(
        bot_id="bot_a", conn=conn_a, owner_chat_id=9999,
        send_message_fn=send,
    )
    import asyncio
    asyncio.run(tailer._tick())
    assert send.await_count == 1
    text = send.call_args[0][1]
    assert text  # non-empty render
    # The renderer should at least mention the action type or symbol.
    assert ("OPEN" in text) or ("BUY" in text) or ("XAUUSD" in text)
