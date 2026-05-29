import asyncio
import json
import logging
import sqlite3
from datetime import datetime, timezone
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from src import config
from src.bot_keyboards import _kb_for_action, _kb_for_relaunch
from src.bot_loops.orphan_recovery import (
    recover_orphan_pending_actions,  # re-exported for back-compat with tests
)
from src.db import connect, init_schema, get_setting, set_setting
from src.logging_setup import configure_logging
from src.telegram_format import render_action_notification
from src.promoter import promote_due_actions, release_stale_claims

log = configure_logging("bot")


def _owner_only(user_id: int) -> bool:
    return user_id == config.TG_BOT_OWNER_USER_ID


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    await update.message.reply_text("CopyTrades bot ready.")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn: sqlite3.Connection = ctx.application.bot_data["conn"]
    ks = get_setting(conn, "kill_switch")
    pend = conn.execute("SELECT COUNT(*) FROM actions WHERE status='pending'").fetchone()[0]
    sent = conn.execute("SELECT COUNT(*) FROM actions WHERE status='sent'").fetchone()[0]
    pos = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'").fetchone()[0]
    await update.message.reply_text(
        f"kill_switch: {ks}\npending: {pend}\nsent: {sent}\nopen positions: {pos}"
    )


async def cmd_halt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    set_setting(conn, "kill_switch", "on")
    # Tag the halt so cost_guard's auto-resume sweeper leaves operator-set
    # halts alone (REVIEW.md Q7).
    set_setting(conn, "kill_switch_reason", "operator")
    await update.message.reply_text("🛑 KILL SWITCH ON. Already-sent actions will still run.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    set_setting(conn, "kill_switch", "off")
    # Clear the halt-reason marker so cost_guard auto-resume logic
    # treats the next halt cleanly (REVIEW.md Q7).
    conn.execute("DELETE FROM settings WHERE key='kill_switch_reason'")
    await update.message.reply_text("✅ Kill switch OFF.")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /cancel <action_id>")
        return
    aid = int(ctx.args[0])
    await _do_cancel(ctx.application.bot_data["conn"], aid, update.message.reply_text)


async def cmd_execute(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    if not ctx.args:
        await update.message.reply_text("Usage: /execute <action_id>")
        return
    aid = int(ctx.args[0])
    await _do_execute(ctx.application.bot_data["conn"], aid, update.message.reply_text)


async def cmd_positions(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    rows = conn.execute(
        "SELECT mt5_ticket, side, symbol, volume, entry_price, sl, tp "
        "FROM positions WHERE status='open' ORDER BY id"
    ).fetchall()
    if not rows:
        await update.message.reply_text("No open positions.")
        return
    lines = ["Open positions:"]
    for r in rows:
        lines.append(
            f"  #{r['mt5_ticket']} {r['side']} {r['symbol']} vol={r['volume']:.2f} "
            f"entry={r['entry_price']} sl={r['sl']} tp={r['tp']}"
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_closeall(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    payload = json.dumps({"symbol": "XAUUSD", "reason": "manual_user_command"})
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status, execute_after) "
        "VALUES('CLOSE_ALL', ?, 'pending', ?)",
        (payload, datetime.now(timezone.utc).isoformat()),
    )
    await update.message.reply_text(f"Queued CLOSE_ALL action #{cur.lastrowid}")


async def on_button(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    q = update.callback_query
    await q.answer()
    # Defensive parse: maxsplit=1 handles future op:arg payloads without
    # crashing, and the `op` whitelist makes a malformed callback fail
    # cleanly instead of leaving the button stuck in a spinner.
    parts = q.data.split(":", 1)
    if len(parts) != 2 or parts[0] not in ("cancel", "execute", "relexec", "relskip"):
        log.warning("on_button: invalid callback_data=%r", q.data)
        await q.edit_message_text("Invalid callback.")
        return
    op, aid_str = parts
    try:
        aid = int(aid_str)
    except ValueError:
        log.warning("on_button: non-int aid in callback_data=%r", q.data)
        await q.edit_message_text("Invalid callback.")
        return
    conn = ctx.application.bot_data["conn"]
    if op == "cancel":
        await _do_cancel(conn, aid, lambda t: q.edit_message_text(t))
    elif op == "execute":
        await _do_execute(conn, aid, lambda t: q.edit_message_text(t))
    elif op == "relexec":
        await _do_relaunch_execute(conn, aid, lambda t: q.edit_message_text(t))
    elif op == "relskip":
        await _do_relaunch_skip(conn, aid, lambda t: q.edit_message_text(t))


async def _do_cancel(conn, aid, reply):
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    if row is None:
        await reply(f"Action #{aid} not found.")
        return
    if row["status"] != "pending":
        await reply(f"Action #{aid} is {row['status']}, cannot cancel.")
        return
    conn.execute("UPDATE actions SET status='cancelled' WHERE id=?", (aid,))
    await reply(f"Cancelled action #{aid}.")


async def _do_execute(conn, aid, reply):
    row = conn.execute("SELECT status FROM actions WHERE id=?", (aid,)).fetchone()
    if row is None:
        await reply(f"Action #{aid} not found.")
        return
    if row["status"] != "pending":
        await reply(f"Action #{aid} is {row['status']}, cannot execute.")
        return
    conn.execute("UPDATE actions SET status='sent' WHERE id=?", (aid,))
    await reply(f"Promoted action #{aid} to sent.")


async def _do_relaunch_execute(conn, aid, reply):
    """Operator chose Execute on a recovery DM (REVIEW.md P1 / Q2).

    The recovery scan parked the action by clearing execute_after; this
    restores it to "now" so the next promoter sweep picks it up. We do
    NOT skip pending → sent here directly — the promoter may still hold
    it back if kill_switch is on, and we want that policy to apply.
    """
    row = conn.execute(
        "SELECT status, action_type FROM actions WHERE id=?", (aid,)
    ).fetchone()
    if row is None:
        await reply(f"Action #{aid} not found.")
        return
    if row["status"] != "pending":
        await reply(
            f"Action #{aid} is {row['status']} now, recovery decision no "
            "longer applies."
        )
        return
    now = datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE actions SET execute_after=? WHERE id=? AND status='pending'",
        (now, aid),
    )
    await reply(f"Recovery: action #{aid} ({row['action_type']}) re-armed for execution.")


async def _do_relaunch_skip(conn, aid, reply):
    """Operator chose Ignore on a recovery DM (REVIEW.md P1 / Q2)."""
    row = conn.execute(
        "SELECT status, action_type FROM actions WHERE id=?", (aid,)
    ).fetchone()
    if row is None:
        await reply(f"Action #{aid} not found.")
        return
    if row["status"] != "pending":
        await reply(
            f"Action #{aid} is {row['status']} now, recovery decision no "
            "longer applies."
        )
        return
    conn.execute(
        "UPDATE actions SET status='rejected', "
        "ea_response='operator_skipped_after_relaunch', "
        "executed_at=? WHERE id=? AND status='pending'",
        (datetime.now(timezone.utc).isoformat(), aid),
    )
    await reply(
        f"Recovery: action #{aid} ({row['action_type']}) skipped "
        "(operator_skipped_after_relaunch)."
    )

# Long-running loops + orphan-recovery routine moved to src.bot_loops
# (Day-1 cleanup: April-May 2026 multi-channel refactor). The imports
# live inside post_init / main rather than at module top to keep bot.py
# importable from tests that don't need to load every feed library.

def _resolve_tailer_conns_for_bot(
    bot_id: str, local_conn,
):
    """Return the list of sqlite3 conns the OutboxTailer needs.

    Step 14: a multi-scope bot may have to tail bot_outbox across N
    destination DBs (``scope=global``, or ``scope=channel`` for a
    channel that mirrors). This helper:

      1. Reads v2 config for the bot's bindings.
      2. Resolves the set of Destinations the bot serves.
      3. Returns ``[local_conn, *new_conns]`` — local always first so
        single-destination bots see the unchanged single-conn path
        (back-compat for callers that introspect ``_conn``).

    Fail-open: any v2 lookup error returns ``[local_conn]`` so a config
    glitch can't break notifications entirely.
    """
    from pathlib import Path as _Path
    try:
        from src import config_v2
        cfg_path = config_v2.config_path()
        if not config_v2.is_v2(cfg_path):
            return [local_conn]
        cfg = config_v2.load_v2(cfg_path)
        if cfg is None:
            return [local_conn]
        dests = cfg.destinations_for_bot(bot_id)
        if len(dests) <= 1:
            return [local_conn]
        # Multi-destination — open the extras that aren't the local DB.
        local_path = _Path(config.DB_PATH).resolve()
        extras: list = []
        for dest in dests:
            if not dest.db_path:
                continue
            dpath = _Path(dest.db_path).resolve()
            if dpath == local_path:
                continue
            if not dpath.exists():
                log.warning(
                    "bot=%s: destination %s db missing at %s; skipping extra conn",
                    bot_id, dest.id, dpath,
                )
                continue
            try:
                extra = connect(str(dpath))
                init_schema(extra)
                extras.append(extra)
            except Exception:
                log.exception(
                    "bot=%s: failed to open extra dest db %s; skipping",
                    bot_id, dpath,
                )
        if extras:
            log.info(
                "bot=%s: multi-destination tailer (local + %d extra dbs)",
                bot_id, len(extras),
            )
        return [local_conn, *extras]
    except Exception:
        log.exception(
            "bot=%s: _resolve_tailer_conns_for_bot failed; using local conn only",
            bot_id,
        )
        return [local_conn]


def _supervise(task: asyncio.Task, name: str) -> None:
    """Attach a done-callback that surfaces silent task deaths.

    Each loop body catches Exception and logs at WARNING/ERROR, so per-tick
    failures recover. But if a BaseException (KeyboardInterrupt, SystemExit,
    asyncio cancellation) escapes the outer `while True`, or if an exception
    fires before the try block, the task dies and asyncio logs to its own
    logger — `bot` doesn't see it. The loop is then silently dead: trades
    queue but no longer get promoted / DM'd. We force a process exit so
    launch.bat / a supervisor restarts us.
    """
    def _cb(t: asyncio.Task) -> None:
        if t.cancelled():
            log.warning("supervised task %s was cancelled", name)
            return
        exc = t.exception()
        if exc is not None:
            log.exception("supervised task %s died: %r", name, exc,
                          exc_info=exc)
            # Forced exit; relies on launch.bat or systemd to restart us.
            # The trader needs a stopped process they can see, not a
            # silently-running bot with one dead loop.
            import os
            os._exit(2)
    task.add_done_callback(_cb)


async def post_init(app: Application):
    # All long-running loops + recovery live in src.bot_loops now.
    # Importing lazily here so bot.py stays cheap to import from tests
    # that only touch command handlers.
    from src.bot_loops import (
        calendar_feed_loop,
        claim_sweeper_loop,
        cost_guard_loop,
        cot_feed_loop,
        etf_flows_feed_loop,
        learning_loop,
        macro_feed_loop,
        news_scan_feed_loop,
        notification_dispatcher,
        position_close_notifier,
        promotion_loop,
        recover_orphan_pending_actions,
        telegram_heartbeat_loop,
    )

    # Startup ping — operator wants to know the system is up. Best-effort:
    # if the owner hasn't /start-ed yet, send_message raises Forbidden which
    # we log and swallow.
    try:
        await app.bot.send_message(
            chat_id=config.TG_BOT_OWNER_USER_ID,
            text="🤖 Bot started — promoter + sweepers + notifiers running",
        )
    except Exception as e:
        log.warning("startup ping failed: %s", e)
    # Crash recovery (REVIEW.md P1 / Q2): scan for pending actions from a
    # prior session and ask the operator before any auto-promotion can
    # fire. Parks orphans by NULLing execute_after BEFORE promotion_loop
    # starts so there is no window where the promoter could pick them up.
    try:
        await recover_orphan_pending_actions(app)
    except Exception as e:
        log.exception("recover_orphan_pending_actions failed: %s", e)
    # Step 7 of multi-channel plan: prefer the v2 per-bot outbox tailer
    # when a binding exists for this destination; otherwise fall back to
    # the legacy notification_dispatcher polling. Mutex enforced here
    # (never both) — the legacy path uses actions.notified_at while the
    # tailer uses bot_outbox.delivered_at, and dispatch_notification in
    # api.py is a no-op when no v2 binding exists, so the two paths
    # cleanly partition the work between v1 (legacy) and v2 (tailer)
    # deployments.
    from src.notification_dispatcher import resolve_bot_id_for_destination
    bot_id = resolve_bot_id_for_destination(config.DB_PATH)
    if bot_id:
        from src.bot_outbox_tailer import OutboxTailer
        async def _send(chat_id, text, *, reply_markup=None):
            await app.bot.send_message(
                chat_id=chat_id, text=text, reply_markup=reply_markup,
            )
        # Step 14: cross-destination tailing. If the bot's bindings span
        # destinations beyond config.DB_PATH (scope=global, or a
        # scope=channel binding for a channel that mirrors), open one
        # extra read+write connection per other destination DB and pass
        # the whole list to OutboxTailer. The local conn stays first
        # so single-destination bots are an unchanged path.
        tailer_conns = _resolve_tailer_conns_for_bot(bot_id, app.bot_data["conn"])
        tailer = OutboxTailer(
            bot_id=bot_id,
            conns=tailer_conns,
            owner_chat_id=config.TG_BOT_OWNER_USER_ID,
            send_message_fn=_send,
        )
        suppressed = tailer.mark_existing_delivered()
        if suppressed:
            log.info(
                "OutboxTailer startup: suppressed %d backlog row(s) for bot_id=%s",
                suppressed, bot_id,
            )
        log.info("notification path: v2 OutboxTailer (bot_id=%s)", bot_id)
        _supervise(asyncio.create_task(tailer.run_forever()),
                   "bot_outbox_tailer")
        # Day-3 cleanup: position-close DMs now flow through the tailer
        # too (api.close_position dispatches event_type='position_closed').
        # No need for the legacy polling notifier here — would just
        # produce duplicate DMs.
    else:
        log.info("notification path: legacy notification_dispatcher "
                 "(no v2 binding found for this destination)")
        _supervise(asyncio.create_task(notification_dispatcher(app)),
                   "notification_dispatcher")
        # No v2 binding → no dispatch_notification fires. Legacy
        # position_close_notifier is the only path that DMs the
        # operator on close.
        _supervise(asyncio.create_task(position_close_notifier(app)),
                   "position_close_notifier")
    _supervise(asyncio.create_task(promotion_loop(app)), "promotion_loop")
    _supervise(asyncio.create_task(claim_sweeper_loop(app)), "claim_sweeper_loop")
    _supervise(asyncio.create_task(macro_feed_loop(app)), "macro_feed_loop")
    _supervise(asyncio.create_task(cot_feed_loop(app)), "cot_feed_loop")
    _supervise(asyncio.create_task(etf_flows_feed_loop(app)), "etf_flows_feed_loop")
    _supervise(asyncio.create_task(news_scan_feed_loop(app)), "news_scan_feed_loop")
    _supervise(asyncio.create_task(calendar_feed_loop(app)), "calendar_feed_loop")
    _supervise(asyncio.create_task(telegram_heartbeat_loop(app)), "telegram_heartbeat_loop")
    _supervise(asyncio.create_task(cost_guard_loop(app)), "cost_guard_loop")
    _supervise(asyncio.create_task(learning_loop(app)), "learning_loop")


def main() -> None:
    if not config.TG_BOT_OWNER_USER_ID:
        raise SystemExit("TG_BOT_OWNER_USER_ID must be set (DM @userinfobot to get yours).")
    if not config.TG_BOT_TOKEN:
        raise SystemExit("TG_BOT_TOKEN must be set (get one from @BotFather).")
    conn = connect(config.DB_PATH)
    init_schema(conn)
    app = Application.builder().token(config.TG_BOT_TOKEN).post_init(post_init).build()
    app.bot_data["conn"] = conn
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("halt", cmd_halt))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("execute", cmd_execute))
    app.add_handler(CommandHandler("positions", cmd_positions))
    app.add_handler(CommandHandler("closeall", cmd_closeall))
    app.add_handler(CallbackQueryHandler(on_button))
    # bootstrap_retries=-1: retry the initial get_me() / getUpdates handshake
    # forever. Default is 0, so a single transient network blip at startup
    # (ISP flap, DNS hiccup, laptop just woke up) crashes the bot with
    # telegram.error.TimedOut before it ever reaches the polling loop.
    app.run_polling(bootstrap_retries=-1)


if __name__ == "__main__":
    main()
