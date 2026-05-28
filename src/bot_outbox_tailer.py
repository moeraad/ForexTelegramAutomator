"""Per-bot outbox tailer (Step 7 of multi-channel plan).

Each Bot process instantiates one ``OutboxTailer`` for its v2 ``bot_id``.
The tailer polls ``bot_outbox`` for undelivered rows targeting this bot,
renders each as a Telegram DM, sends it via the injected ``send_message``
async function, and marks ``delivered_at``.

Architecture:

  - Polling interval: 1 second (matches the legacy notification_dispatcher
    loop cadence; trades a small amount of latency for a tiny DB load).
  - Atomicity: ``delivered_at`` is set AFTER ``send_message`` succeeds.
    If send fails, the row stays undelivered and is retried on the next
    poll. The bot's Telegram API rate-limit handling lives in the send
    function (python-telegram-bot's defaults are fine for normal volume).
  - Crash recovery: undelivered rows queued before a bot crash are picked
    up on restart automatically — same mechanism as v1's
    ``notified_at IS NULL`` poll. No first-run flood guard needed because
    Step 7's outbox table is created empty by the migration in Step 2.

The renderer is dispatched on ``event_type``:

  - ``action_terminal`` → fetch action + maybe-related position, call
    ``telegram_format.render_action_terminal``
  - ``action_parked`` → render backfill-review prompt with relaunch
    keyboard (same as legacy "parked" path)
  - ``alert`` → render free-form alert text from event_payload

Unknown event_types are logged and skipped (the row stays undelivered so
a future build of the tailer can pick them up).
"""
from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from typing import Any, Awaitable, Callable


log = logging.getLogger("bot_outbox_tailer")


# send_message_fn signature: async (chat_id: int, text: str, *, reply_markup=None) -> None
SendMessageFn = Callable[..., Awaitable[Any]]


def _resolve_channel_name(source_channel_id: str | None) -> str:
    """Map a v2 Channel.id to a human-friendly display name.

    Step 12: used by DM renderers to label which channel triggered each
    notification (aggregate routing surfaces this). Returns "" when:
      - source_channel_id is None / empty (legacy row, single-channel
        destination — caller renders plain DM without channel label)
      - v2 config can't be loaded (fresh install pre-migration)
      - Channel id isn't in the v2 config (orphan tag)

    Caller treats "" as "don't label this DM" — matches the legacy DM
    shape so single-channel deployments don't gain noise.
    """
    if not source_channel_id:
        return ""
    try:
        from src import config_v2
        cfg_path = config_v2.config_path()
        if not config_v2.is_v2(cfg_path):
            return ""
        cfg = config_v2.load_v2(cfg_path)
        if cfg is None:
            return ""
        c = cfg.channel(source_channel_id)
        return c.name if c is not None else ""
    except Exception:
        log.exception("channel-name lookup failed for %s", source_channel_id)
        return ""


def _lookup_reply_parent_text(
    conn: sqlite3.Connection, source_msg_id: int | None,
) -> str | None:
    """Return the parent message text when the action's source message
    was a Telegram reply. None when source_msg_id is missing, the
    message isn't a reply, or the parent isn't in the local archive.

    Used by render_action_terminal so the operator DM can echo the
    antecedent the AI used (e.g. CANCEL_PENDING fired because the
    operator's "cancel that order" replied to a specific signal).
    """
    if not source_msg_id:
        return None
    try:
        src = conn.execute(
            "SELECT chat_id, reply_to_tg_message_id FROM messages "
            "WHERE id=?",
            (source_msg_id,),
        ).fetchone()
        if src is None:
            return None
        parent_tg_id = src["reply_to_tg_message_id"]
        if not parent_tg_id:
            return None
        parent = conn.execute(
            "SELECT text FROM messages "
            "WHERE chat_id=? AND tg_message_id=?",
            (src["chat_id"], parent_tg_id),
        ).fetchone()
        if parent is None:
            return None
        return (parent["text"] or "").strip()
    except sqlite3.Error:
        log.exception(
            "reply-parent lookup failed for source_msg_id=%s",
            source_msg_id,
        )
        return None


class OutboxTailer:
    """Polls bot_outbox for one bot_id and DMs each undelivered row.

    Designed to run as an asyncio background task in the Bot process.
    """

    def __init__(
        self,
        *,
        bot_id: str,
        conn: sqlite3.Connection | None = None,
        conns: list[sqlite3.Connection] | None = None,
        owner_chat_id: int,
        send_message_fn: SendMessageFn,
        poll_interval_sec: float = 1.0,
        batch_size: int = 20,
    ) -> None:
        """Tail bot_outbox across one OR many destination DBs.

        ``conn`` (singular) is the back-compat single-destination form —
        Steps 7-13 deployments use this exclusively.

        ``conns`` (plural) is the Step 14 multi-destination form for
        bots whose bindings span destinations (``scope=global``, or
        ``scope=channel`` for a channel that mirrors). Each conn is
        polled independently; rows in conn A's DB are delivered using
        conn A (so ``delivered_at`` updates land in the same DB the row
        came from).

        Exactly one of ``conn`` / ``conns`` must be provided.
        """
        if conn is not None and conns is not None:
            raise ValueError("OutboxTailer: pass either conn= or conns=, not both")
        if conn is None and not conns:
            raise ValueError("OutboxTailer: must pass conn= or conns=")
        self._bot_id = bot_id
        self._conns: list[sqlite3.Connection] = list(conns) if conns else [conn]  # type: ignore[list-item]
        self._owner_chat_id = owner_chat_id
        self._send = send_message_fn
        self._poll = poll_interval_sec
        self._batch = batch_size

    @property
    def bot_id(self) -> str:
        return self._bot_id

    @property
    def _conn(self) -> sqlite3.Connection:
        """Back-compat shim for tests that introspect the single conn.

        Returns the first conn in multi-mode (caller knows it's a
        legitimate destination DB)."""
        return self._conns[0]

    def mark_existing_delivered(self) -> int:
        """First-run guard: mark all currently-undelivered rows for this
        bot_id as delivered, across EVERY connected DB, so a restart
        doesn't flood the operator with backlog DMs queued before the
        tailer was running.

        Mirrors the legacy ``notification_dispatcher``'s startup behavior
        (it sets ``actions.notified_at = CURRENT_TIMESTAMP`` on terminal
        rows at startup for the same reason).

        Returns the total number of rows updated across all conns.
        """
        total = 0
        for conn in self._conns:
            cur = conn.execute(
                "UPDATE bot_outbox SET delivered_at = CURRENT_TIMESTAMP "
                "WHERE bot_id = ? AND delivered_at IS NULL",
                (self._bot_id,),
            )
            total += cur.rowcount or 0
            conn.commit()
        return total

    async def run_forever(self) -> None:
        """Main loop: poll → render → send → mark delivered."""
        log.info(
            "OutboxTailer started: bot_id=%s dbs=%d",
            self._bot_id, len(self._conns),
        )
        while True:
            try:
                await self._tick()
            except Exception:
                log.exception("OutboxTailer tick failed; will retry")
            await asyncio.sleep(self._poll)

    async def _tick(self) -> None:
        # Poll each connected DB in turn. Each conn is independent; one
        # slow/locked DB shouldn't starve the others (the legacy single-DB
        # case is still a one-iteration loop).
        for conn in self._conns:
            rows = conn.execute(
                "SELECT id, event_type, event_payload, source_channel_id, "
                "       route_id, action_id, created_at "
                "FROM bot_outbox "
                "WHERE bot_id = ? AND delivered_at IS NULL "
                "ORDER BY id ASC LIMIT ?",
                (self._bot_id, self._batch),
            ).fetchall()
            for row in rows:
                await self._deliver_one(conn, row)

    async def _deliver_one(
        self, conn: sqlite3.Connection, row: sqlite3.Row,
    ) -> None:
        try:
            payload = json.loads(row["event_payload"]) if row["event_payload"] else {}
        except (TypeError, ValueError):
            payload = {}

        text, reply_markup = self._render(conn, row, payload)
        if text is None:
            # Unknown event_type — leave the row for a future build to handle.
            log.warning(
                "OutboxTailer: unknown event_type=%s row=%s; skipping",
                row["event_type"], row["id"],
            )
            return

        try:
            kwargs: dict[str, Any] = {}
            if reply_markup is not None:
                kwargs["reply_markup"] = reply_markup
            await self._send(self._owner_chat_id, text, **kwargs)
        except Exception as e:
            log.warning(
                "OutboxTailer: DM failed for row=%s bot=%s err=%s",
                row["id"], self._bot_id, e,
            )
            # Don't mark delivered — next tick retries.
            return

        # Mark delivered IN THE SAME DB the row came from. Step 14: this
        # matters when a multi-dest tailer is serving N DBs — the row's
        # outbox lives in conn X's DB, so the UPDATE has to land there.
        conn.execute(
            "UPDATE bot_outbox SET delivered_at = CURRENT_TIMESTAMP "
            "WHERE id = ?",
            (row["id"],),
        )
        # Mirror to actions.notified_at when an action is involved so the
        # legacy notification_dispatcher (if accidentally also running)
        # doesn't re-DM the same event. Belt-and-suspenders against the
        # mutex enforcement in bot.py.
        if row["action_id"] is not None:
            conn.execute(
                "UPDATE actions SET notified_at = CURRENT_TIMESTAMP "
                "WHERE id = ? AND notified_at IS NULL",
                (row["action_id"],),
            )
        conn.commit()

    # ---- renderers -------------------------------------------------------

    def _render(
        self, conn: sqlite3.Connection, row: sqlite3.Row, payload: dict,
    ) -> tuple[str | None, Any]:
        """Return (text, reply_markup) or (None, None) for unknown event_type."""
        event_type = row["event_type"]
        if event_type == "action_terminal":
            return self._render_action_terminal(conn, row, payload), None
        if event_type == "action_parked":
            return self._render_action_parked(conn, row, payload)
        if event_type == "alert":
            return self._render_alert(row, payload), None
        if event_type == "position_closed":
            return self._render_position_closed(conn, row, payload), None
        return None, None

    def _render_action_terminal(
        self, conn: sqlite3.Connection, row: sqlite3.Row, payload: dict,
    ) -> str | None:
        from src.telegram_format import render_action_terminal
        action_row = conn.execute(
            "SELECT id, action_type, status, payload_json, ea_response, "
            "source_msg_id "
            "FROM actions WHERE id = ?",
            (row["action_id"],),
        ).fetchone()
        if action_row is None:
            return f"(action #{row['action_id']} disappeared before DM)"
        try:
            action_payload = json.loads(action_row["payload_json"])
        except (TypeError, ValueError):
            action_payload = {}
        # EA-side "noop_*" responses are silent ack per operator pref.
        if (action_row["ea_response"] or "").startswith("noop_"):
            return None  # treat as "nothing to send" — caller will not deliver
        actual_entry: float | None = None
        actual_volume: float | None = None
        if action_row["action_type"] == "OPEN" and action_row["status"] == "executed":
            pos = conn.execute(
                "SELECT entry_price, "
                "       COALESCE(original_volume, volume) AS vol "
                "FROM positions WHERE action_id = ? "
                "ORDER BY id DESC LIMIT 1",
                (action_row["id"],),
            ).fetchone()
            if pos is not None:
                if pos["entry_price"] is not None:
                    actual_entry = float(pos["entry_price"])
                if pos["vol"] is not None:
                    actual_volume = float(pos["vol"])
        # Step 12 (aggregate routing): annotate which channel triggered
        # this action. Only labels when the source_channel_id resolves
        # to a name in v2 config — single-channel destinations get the
        # clean legacy DM (no '[from: ...]' clutter).
        source_channel_name = _resolve_channel_name(row["source_channel_id"])
        # Pull the source message's reply parent (if any) so the DM can
        # echo the antecedent the AI used. Tail when no parent / not a
        # reply.
        reply_parent_text = _lookup_reply_parent_text(
            conn, action_row["source_msg_id"],
        )
        return render_action_terminal(
            action_row["id"], action_row["action_type"], action_row["status"],
            action_payload, action_row["ea_response"] or "",
            actual_entry=actual_entry,
            actual_volume=actual_volume,
            source_channel_name=source_channel_name,
            reply_parent_text=reply_parent_text,
        )

    def _render_action_parked(
        self, conn: sqlite3.Connection, row: sqlite3.Row, payload: dict,
    ) -> tuple[str | None, Any]:
        """Backfill-review parked action: identical text + keyboard to the
        legacy notification_dispatcher's parked branch."""
        from src.telegram_format import _payload_summary
        action_row = conn.execute(
            "SELECT id, action_type, payload_json, source_msg_id, created_at "
            "FROM actions WHERE id = ?",
            (row["action_id"],),
        ).fetchone()
        if action_row is None:
            return f"(action #{row['action_id']} disappeared before DM)", None
        try:
            action_payload = json.loads(action_row["payload_json"]) if action_row["payload_json"] else {}
        except (TypeError, ValueError):
            action_payload = {}
        summary = _payload_summary(action_row["action_type"], action_payload) or "(no payload)"
        src_text = ""
        if action_row["source_msg_id"]:
            mrow = conn.execute(
                "SELECT text, received_at FROM messages WHERE id = ?",
                (action_row["source_msg_id"],),
            ).fetchone()
            if mrow is not None:
                snippet = (mrow["text"] or "").strip().replace("\n", " ")
                if len(snippet) > 240:
                    snippet = snippet[:240] + "..."
                src_text = f"\nSignal at {mrow['received_at']}:\n  \"{snippet}\""
        text = (
            f"♻️ Backfill review - action #{action_row['id']}\n"
            f"Type: {action_row['action_type']}\n"
            f"Created: {action_row['created_at']}\n"
            f"Payload: {summary}"
            f"{src_text}\n\n"
            f"This management action arrived via backfill replay "
            f"(listener was offline when the original signal posted). "
            f"Auto-execution was suppressed to avoid re-firing a "
            f"stale instruction. Execute now or ignore?"
        )
        from src.bot_keyboards import _kb_for_relaunch
        return text, _kb_for_relaunch(action_row["id"])

    def _render_alert(self, row: sqlite3.Row, payload: dict) -> str:
        level = payload.get("level", "info")
        prefix = {"warning": "⚠️", "critical": "🚨"}.get(level, "ℹ️")
        text = payload.get("text", "(no alert text)")
        return f"{prefix} {text}"

    def _render_position_closed(
        self, conn: sqlite3.Connection, row: sqlite3.Row, payload: dict,
    ) -> str | None:
        """Day-3 cleanup: position-close DM now flows through the dispatcher.

        Replaces the legacy ``position_close_notifier`` polling loop.
        The dispatcher writes one outbox row per close with
        ``event_payload={'position_id': N}``; the tailer fetches the
        latest position row at render time so a fast follow-up edit
        (e.g. realized_pnl arriving via a separate POST) is reflected.
        """
        from src.telegram_format import render_position_closed
        pos_id = payload.get("position_id")
        if pos_id is None:
            log.warning(
                "position_closed event without position_id in payload: %s", payload,
            )
            return None
        pos = conn.execute(
            "SELECT mt5_ticket, side, symbol, volume, original_volume, "
            "       entry_price, sl, tp, closed_at, close_reason "
            "FROM positions WHERE id = ?",
            (pos_id,),
        ).fetchone()
        if pos is None:
            return f"(position #{pos_id} disappeared before DM)"
        return render_position_closed(
            ticket=pos["mt5_ticket"], side=pos["side"], symbol=pos["symbol"],
            volume=pos["volume"], original_volume=pos["original_volume"],
            entry=pos["entry_price"], sl=pos["sl"], tp=pos["tp"],
            closed_at=pos["closed_at"], reason=pos["close_reason"] or "",
        )
