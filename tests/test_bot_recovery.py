"""Tests for the bot's crash-recovery scan (REVIEW.md P1 / Q2).

When the bot starts after a crash, any actions still in 'pending' state
from the prior session must be parked (execute_after=NULL so the promoter
won't auto-fire them) and the operator must be DMed for an explicit
execute-or-ignore decision per row.
"""
import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from src.db import connect, init_schema
from src.bot import (
    recover_orphan_pending_actions,
    _do_relaunch_execute,
    _do_relaunch_skip,
)


def _setup(tmp_path):
    conn = connect(str(tmp_path / "bot.db"))
    init_schema(conn)
    return conn


def _insert_pending(conn, action_type: str, payload: dict, *, age_sec: int = 60) -> int:
    created = datetime.now(timezone.utc).isoformat()
    due = created  # past — would auto-promote on next sweep
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, "
        "execute_after, created_at) VALUES(?,?,?,?,?)",
        (action_type, json.dumps(payload), "pending", due, created),
    )
    return cur.lastrowid


class _BotStub:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, object]] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        self.sent.append((chat_id, text, reply_markup))


def _app_with(conn) -> SimpleNamespace:
    return SimpleNamespace(bot=_BotStub(), bot_data={"conn": conn})


def test_recover_parks_pending_actions(tmp_path):
    """Every pending action with a non-null execute_after gets parked
    (execute_after NULLed) so the promoter can't pick it up before the
    operator decides."""
    conn = _setup(tmp_path)
    aid = _insert_pending(conn, "REINFORCE", {"side": "BUY"})
    app = _app_with(conn)
    sent = asyncio.run(recover_orphan_pending_actions(app))
    assert sent == 1
    row = conn.execute(
        "SELECT status, execute_after FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["execute_after"] is None


def test_recover_dms_one_message_per_orphan(tmp_path):
    """Operator receives one DM per orphan, summarising payload."""
    conn = _setup(tmp_path)
    _insert_pending(conn, "REINFORCE", {"side": "BUY"})
    _insert_pending(conn, "CLOSE_PARTIAL", {"fraction": 0.5})
    app = _app_with(conn)
    asyncio.run(recover_orphan_pending_actions(app))
    assert len(app.bot.sent) == 2
    types_in_dms = [body for _chat, body, _kb in app.bot.sent]
    assert any("REINFORCE" in t for t in types_in_dms)
    assert any("CLOSE_PARTIAL" in t for t in types_in_dms)


def test_recover_skips_already_terminal_or_non_pending(tmp_path):
    """Only `pending` rows are recovery candidates — sent/claimed/executed
    are EA-managed or already done."""
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES('OPEN', '{}', 'sent', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', '{}', 'executed')"
    )
    app = _app_with(conn)
    sent = asyncio.run(recover_orphan_pending_actions(app))
    assert sent == 0
    assert app.bot.sent == []


def test_relaunch_execute_re_arms_pending(tmp_path):
    """`relexec:` callback re-arms a parked action so the promoter picks
    it up on the next sweep."""
    conn = _setup(tmp_path)
    aid = _insert_pending(conn, "REINFORCE", {"side": "BUY"})
    # Simulate recovery parking
    conn.execute("UPDATE actions SET execute_after=NULL WHERE id=?", (aid,))
    replies: list[str] = []

    async def reply(text):
        replies.append(text)

    asyncio.run(_do_relaunch_execute(conn, aid, reply))
    row = conn.execute(
        "SELECT status, execute_after FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "pending"
    assert row["execute_after"] is not None
    assert any("re-armed" in r for r in replies)


def test_relaunch_skip_marks_rejected(tmp_path):
    """`relskip:` callback marks the action rejected with reason
    operator_skipped_after_relaunch."""
    conn = _setup(tmp_path)
    aid = _insert_pending(conn, "CLOSE_PARTIAL", {"fraction": 0.5})
    conn.execute("UPDATE actions SET execute_after=NULL WHERE id=?", (aid,))
    replies: list[str] = []

    async def reply(text):
        replies.append(text)

    asyncio.run(_do_relaunch_skip(conn, aid, reply))
    row = conn.execute(
        "SELECT status, ea_response FROM actions WHERE id=?", (aid,)
    ).fetchone()
    assert row["status"] == "rejected"
    assert row["ea_response"] == "operator_skipped_after_relaunch"


def test_relaunch_execute_noop_if_action_no_longer_pending(tmp_path):
    """If the action transitioned out of pending between the DM and the
    button press, the callback bails cleanly."""
    conn = _setup(tmp_path)
    aid = _insert_pending(conn, "REINFORCE", {"side": "BUY"})
    conn.execute("UPDATE actions SET status='cancelled' WHERE id=?", (aid,))
    replies: list[str] = []

    async def reply(text):
        replies.append(text)

    asyncio.run(_do_relaunch_execute(conn, aid, reply))
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    assert row["status"] == "cancelled"
    assert any("no longer applies" in r for r in replies)
