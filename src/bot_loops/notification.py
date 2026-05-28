"""Legacy notification loops moved out of ``src/bot.py``.

Two loops live here:

  - ``notification_dispatcher``: polls ``actions`` for newly-terminal rows
    and DMs the operator. The v2-aware path is the new OutboxTailer
    (``src.bot_outbox_tailer``); ``post_init`` picks ONE of the two at
    startup based on whether a v2 binding exists for this destination.
  - ``position_close_notifier``: polls ``positions`` for newly-closed
    rows and DMs the operator. Not migrated to the v2 dispatcher yet —
    runs unconditionally regardless of v2 binding state.
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3

from telegram.ext import Application

from src import config
from src.bot_keyboards import _kb_for_relaunch
from src.db import get_setting, set_setting


log = logging.getLogger("bot")


async def notification_dispatcher(app: Application):
    """Polls for actions that just reached a terminal state and DMs the owner.

    Per project policy (fully automated, no human approval gate): the bot
    only DMs about actions that have ACTUALLY HAPPENED — executed, failed,
    or rejected. The pre-execution approval prompt is gone; the promoter
    auto-promotes pending → sent without operator interaction.
    """
    from src.telegram_format import render_action_terminal
    conn: sqlite3.Connection = app.bot_data["conn"]

    # First-run guard: any terminal action sitting in the DB at startup
    # was completed before this dispatcher existed (or before the new
    # behavior shipped). Treat them as already-seen so we don't flood
    # the operator with backlog DMs every time the bot restarts.
    conn.execute(
        "UPDATE actions SET notified_at = CURRENT_TIMESTAMP "
        "WHERE notified_at IS NULL "
        "  AND status IN ('executed','failed','rejected')"
    )

    while True:
        try:
            # Parked rows: status='pending' with execute_after=NULL come from
            # the backfill-management guard in orchestrator (REVIEW.md P1 /
            # Q5). DM them with the relaunch keyboard so the operator can
            # Execute (re-arm) or Ignore (reject).
            parked = conn.execute(
                "SELECT id, action_type, payload_json, source_msg_id, "
                "       created_at, ea_response "
                "FROM actions "
                "WHERE status='pending' AND execute_after IS NULL "
                "  AND notified_at IS NULL "
                "  AND COALESCE(ea_response,'') = 'backfill_management_review_required' "
                "ORDER BY id ASC LIMIT 20"
            ).fetchall()
            for r in parked:
                try:
                    payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
                except (TypeError, ValueError):
                    payload = {}
                from src.telegram_format import _payload_summary
                summary = _payload_summary(r["action_type"], payload) or "(no payload)"
                src_text = ""
                if r["source_msg_id"]:
                    mrow = conn.execute(
                        "SELECT text, received_at FROM messages WHERE id=?",
                        (r["source_msg_id"],),
                    ).fetchone()
                    if mrow is not None:
                        snippet = (mrow["text"] or "").strip().replace("\n", " ")
                        if len(snippet) > 240:
                            snippet = snippet[:240] + "..."
                        src_text = (
                            f"\nSignal at {mrow['received_at']}:\n  \"{snippet}\""
                        )
                text = (
                    f"♻️ Backfill review - action #{r['id']}\n"
                    f"Type: {r['action_type']}\n"
                    f"Created: {r['created_at']}\n"
                    f"Payload: {summary}"
                    f"{src_text}\n\n"
                    f"This management action arrived via backfill replay "
                    f"(listener was offline when the original signal posted). "
                    f"Auto-execution was suppressed to avoid re-firing a "
                    f"stale instruction. Execute now or ignore?"
                )
                try:
                    await app.bot.send_message(
                        chat_id=config.TG_BOT_OWNER_USER_ID,
                        text=text,
                        reply_markup=_kb_for_relaunch(r["id"]),
                    )
                    conn.execute(
                        "UPDATE actions SET notified_at=CURRENT_TIMESTAMP "
                        "WHERE id=?",
                        (r["id"],),
                    )
                except Exception as e:
                    log.warning("backfill-review DM failed for #%s: %s", r["id"], e)
            rows = conn.execute(
                "SELECT id, action_type, payload_json, status, ea_response, source_msg_id "
                "FROM actions WHERE notified_at IS NULL "
                "AND status IN ('executed','failed','rejected') "
                "ORDER BY id ASC LIMIT 20"
            ).fetchall()
            for r in rows:
                # EA-side no-op markers: when EnableAiPartialAndBe is off the
                # EA writes status=executed with this ea_response so the
                # rejected counter doesn't bump. The operator explicitly
                # asked for these to be invisible — ack and move on.
                if (r["ea_response"] or "").startswith("noop_"):
                    conn.execute(
                        "UPDATE actions SET notified_at=CURRENT_TIMESTAMP "
                        "WHERE id=?",
                        (r["id"],),
                    )
                    continue
                try:
                    payload = json.loads(r["payload_json"])
                except (TypeError, ValueError):
                    payload = {}
                # For executed OPEN actions, look up the broker fill price
                # AND the filled lot size from the resulting position row
                # so the DM shows both the channel signal's entry zone /
                # the actual entry / the filled lots. Prefer original_volume
                # so a fast TP1-partial doesn't shrink the reported lot
                # size on the OPEN notification.
                actual_entry: float | None = None
                actual_volume: float | None = None
                if r["action_type"] == "OPEN" and r["status"] == "executed":
                    pos_row = conn.execute(
                        "SELECT entry_price, "
                        "       COALESCE(original_volume, volume) AS vol "
                        "FROM positions "
                        "WHERE action_id = ? "
                        "ORDER BY id DESC LIMIT 1",
                        (r["id"],),
                    ).fetchone()
                    if pos_row is not None:
                        if pos_row["entry_price"] is not None:
                            actual_entry = float(pos_row["entry_price"])
                        if pos_row["vol"] is not None:
                            actual_volume = float(pos_row["vol"])
                # Pull the reply parent's text when the source message
                # was a Telegram reply, so the DM echoes the antecedent
                # the AI used to interpret pronouns.
                from src.bot_outbox_tailer import _lookup_reply_parent_text
                reply_parent_text = _lookup_reply_parent_text(
                    conn, r["source_msg_id"],
                )
                text = render_action_terminal(
                    r["id"], r["action_type"], r["status"],
                    payload, r["ea_response"] or "",
                    actual_entry=actual_entry,
                    actual_volume=actual_volume,
                    reply_parent_text=reply_parent_text,
                )
                await app.bot.send_message(
                    chat_id=config.TG_BOT_OWNER_USER_ID,
                    text=text,
                )
                conn.execute(
                    "UPDATE actions SET notified_at=CURRENT_TIMESTAMP WHERE id=?",
                    (r["id"],),
                )
        except Exception as e:
            log.exception("notification_dispatcher error: %s", e)
        await asyncio.sleep(1.0)


async def position_close_notifier(app: Application):
    """Polls for newly-closed positions and DMs the owner.

    Tracks progress via settings.position_close_last_notified_at (ISO-8601
    UTC string). On each tick, any positions with closed_at > that
    watermark get a one-line DM and the watermark advances to the most
    recent closed_at.
    """
    from src.telegram_format import render_position_closed
    conn: sqlite3.Connection = app.bot_data["conn"]

    # Watermark by positions.id, not closed_at. Two closes in the same
    # second produce identical closed_at strings; with `closed_at > cursor`
    # the second one was being silently skipped on the next tick. Using
    # the monotonic primary key removes the tie-break ambiguity entirely.
    # First-run guard seeds the cursor past the existing backlog so the
    # operator doesn't get flooded with historical close DMs on first
    # launch.
    if get_setting(conn, "position_close_last_notified_id") is None:
        row = conn.execute(
            "SELECT COALESCE(MAX(id), 0) AS m FROM positions "
            "WHERE status='closed'"
        ).fetchone()
        set_setting(conn, "position_close_last_notified_id", str(row["m"]))

    while True:
        try:
            cursor = int(
                get_setting(conn, "position_close_last_notified_id") or "0"
            )
            rows = conn.execute(
                "SELECT id, mt5_ticket, side, symbol, volume, original_volume, "
                "       entry_price, sl, tp, closed_at, close_reason "
                "FROM positions WHERE status='closed' "
                "  AND closed_at IS NOT NULL AND id > ? "
                "ORDER BY id ASC LIMIT 20",
                (cursor,),
            ).fetchall()
            for r in rows:
                text = render_position_closed(
                    ticket=r["mt5_ticket"], side=r["side"], symbol=r["symbol"],
                    volume=r["volume"], original_volume=r["original_volume"],
                    entry=r["entry_price"], sl=r["sl"], tp=r["tp"],
                    closed_at=r["closed_at"], reason=r["close_reason"] or "",
                )
                await app.bot.send_message(
                    chat_id=config.TG_BOT_OWNER_USER_ID,
                    text=text,
                )
                set_setting(conn, "position_close_last_notified_id", str(r["id"]))
        except Exception as e:
            log.exception("position_close_notifier error: %s", e)
        await asyncio.sleep(1.0)
