"""Orphan-pending-action recovery moved out of ``src/bot.py``.

Called once on bot startup (NOT a long-running loop) but lives in
``bot_loops`` because it's the same class of post-init wiring as the
loops: imported + invoked from ``src.bot.post_init``.
"""
from __future__ import annotations

import json
import logging
import sqlite3

from telegram.ext import Application

from src import config
from src.bot_keyboards import _kb_for_relaunch


log = logging.getLogger("bot")


async def recover_orphan_pending_actions(app: Application) -> int:
    """At bot startup, find pending actions left over from a prior session
    (listener / bot crashed before they could be promoted) and ask the
    operator whether to execute or ignore each one.

    REVIEW.md P1 (Q2 / Q5):
      - Q2: "When listener crashes it should check the last non-executed
            actions from the prior session and ask me execute/ignore with
            full context."
      - Q5: "An old REINFORCE reminder arriving on listener relaunch must
            NOT re-close-and-reopen automatically."

    Strategy:
      1. Parks every pending action by NULLing execute_after — the promoter
         filter (`execute_after IS NOT NULL`) ignores parked rows, so they
         cannot auto-fire while the operator decides.
      2. DMs the operator one message per orphan, with an inline keyboard
         (Execute / Ignore). The callbacks (`relexec:` / `relskip:`) flip
         the action back to a promotable state or mark it
         operator_skipped_after_relaunch.

    Returns the number of orphan actions parked + DMed.
    """
    from src.telegram_format import _payload_summary
    conn: sqlite3.Connection = app.bot_data["conn"]
    rows = conn.execute(
        "SELECT id, action_type, payload_json, source_msg_id, "
        "       created_at, execute_after "
        "FROM actions "
        "WHERE status='pending' AND execute_after IS NOT NULL "
        "ORDER BY id ASC"
    ).fetchall()
    if not rows:
        return 0
    # Park each row so the promoter cannot pick them up between now and
    # the operator's button press.
    ids = [r["id"] for r in rows]
    conn.executemany(
        "UPDATE actions SET execute_after=NULL WHERE id=? AND status='pending'",
        [(i,) for i in ids],
    )
    sent = 0
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        summary = _payload_summary(r["action_type"], payload) or "(no payload)"
        # Original message text for context, if we have it.
        src_text = ""
        if r["source_msg_id"]:
            mrow = conn.execute(
                "SELECT text, received_at FROM messages WHERE id=?",
                (r["source_msg_id"],),
            ).fetchone()
            if mrow is not None:
                snippet = (mrow["text"] or "").strip().replace("\n", " ")
                if len(snippet) > 240:
                    snippet = snippet[:240] + "…"
                src_text = (
                    f"\nSignal at {mrow['received_at']}:\n  \"{snippet}\""
                )
        text = (
            f"♻️ Recovery — orphan action #{r['id']}\n"
            f"Type: {r['action_type']}\n"
            f"Created: {r['created_at']}\n"
            f"Was due: {r['execute_after']}\n"
            f"Payload: {summary}"
            f"{src_text}\n\n"
            f"This action did not run in the previous session. "
            f"Execute it now or ignore?"
        )
        try:
            await app.bot.send_message(
                chat_id=config.TG_BOT_OWNER_USER_ID,
                text=text,
                reply_markup=_kb_for_relaunch(r["id"]),
            )
            sent += 1
        except Exception as e:
            log.warning("recovery DM failed for action #%s: %s", r["id"], e)
    log.info("recover_orphan_pending_actions parked=%s dmed=%s", len(ids), sent)
    return sent
