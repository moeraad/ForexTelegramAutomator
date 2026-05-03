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
from src.db import connect, init_schema, get_setting, set_setting
from src.logging_setup import configure_logging
from src.telegram_format import render_action_notification
from src.promoter import promote_due_actions, release_stale_claims, expire_stale_watches

log = configure_logging("bot")


def _owner_only(user_id: int) -> bool:
    return user_id == config.TG_BOT_OWNER_USER_ID


def _kb_for_action(action_id: int, action_type: str) -> InlineKeyboardMarkup | None:
    if action_type == "ALERT":
        return None
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Cancel", callback_data=f"cancel:{action_id}"),
        InlineKeyboardButton("Execute now", callback_data=f"execute:{action_id}"),
    ]])


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
    await update.message.reply_text("🛑 KILL SWITCH ON. Already-sent actions will still run.")


async def cmd_resume(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not _owner_only(update.effective_user.id):
        return
    conn = ctx.application.bot_data["conn"]
    set_setting(conn, "kill_switch", "off")
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
    op, aid = q.data.split(":")
    aid = int(aid)
    conn = ctx.application.bot_data["conn"]
    if op == "cancel":
        await _do_cancel(conn, aid, lambda t: q.edit_message_text(t))
    elif op == "execute":
        await _do_execute(conn, aid, lambda t: q.edit_message_text(t))


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


async def notification_dispatcher(app: Application):
    """Polls for actions that just reached a terminal state and DMs the owner.

    Per project policy (fully automated, no human approval gate): the bot
    only DMs about actions that have ACTUALLY HAPPENED — executed, failed,
    rejected, or watching. The pre-execution approval prompt is gone; the
    promoter auto-promotes pending → sent without operator interaction.
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
        "  AND status IN ('executed','failed','rejected','watching')"
    )

    while True:
        try:
            rows = conn.execute(
                "SELECT id, action_type, payload_json, status, ea_response, source_msg_id "
                "FROM actions WHERE notified_at IS NULL "
                "AND status IN ('executed','failed','rejected','watching') "
                "ORDER BY id ASC LIMIT 20"
            ).fetchall()
            for r in rows:
                try:
                    payload = json.loads(r["payload_json"])
                except (TypeError, ValueError):
                    payload = {}
                text = render_action_terminal(
                    r["id"], r["action_type"], r["status"],
                    payload, r["ea_response"] or "",
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

    # First-run guard: if the watermark setting is missing, advance it
    # past everything currently in the DB so we don't flood the operator
    # with the historical backlog. Future closes (status flips to
    # 'closed' AFTER startup) will have closed_at > this watermark and
    # flow through normally.
    if get_setting(conn, "position_close_last_notified_at") is None:
        row = conn.execute(
            "SELECT MAX(closed_at) AS m FROM positions "
            "WHERE status='closed' AND closed_at IS NOT NULL"
        ).fetchone()
        seed = (row["m"] if row and row["m"]
                else datetime.now(timezone.utc).isoformat())
        set_setting(conn, "position_close_last_notified_at", seed)

    while True:
        try:
            cursor = (
                get_setting(conn, "position_close_last_notified_at")
                or "1970-01-01T00:00:00+00:00"
            )
            rows = conn.execute(
                "SELECT mt5_ticket, side, symbol, volume, original_volume, "
                "       entry_price, sl, tp, closed_at, close_reason "
                "FROM positions WHERE status='closed' "
                "  AND closed_at IS NOT NULL AND closed_at > ? "
                "ORDER BY closed_at ASC LIMIT 20",
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
                set_setting(conn, "position_close_last_notified_at", r["closed_at"])
        except Exception as e:
            log.exception("position_close_notifier error: %s", e)
        await asyncio.sleep(1.0)


async def promotion_loop(app: Application):
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            n = promote_due_actions(conn)
            if n:
                log.info("promoted %s actions", n)
        except Exception as e:
            log.exception("promotion_loop error: %s", e)
        await asyncio.sleep(1.0)


async def claim_sweeper_loop(app: Application):
    """Periodically release claimed actions a crashed EA never confirmed."""
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            n = release_stale_claims(conn, max_age_sec=120)
            if n:
                log.warning("released %s stale claim(s)", n)
        except Exception as e:
            log.exception("claim_sweeper_loop error: %s", e)
        await asyncio.sleep(15.0)


async def watch_sweeper_loop(app: Application):
    """Reject synthetic-pending watches whose zone expiry has passed.

    Authoritative even when the EA is offline: once expires_at is in the past,
    the signal is stale regardless of EA state.
    """
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            n = expire_stale_watches(conn)
            if n:
                log.info("expired %s stale watch(es)", n)
        except Exception as e:
            log.exception("watch_sweeper_loop error: %s", e)
        await asyncio.sleep(15.0)


async def post_init(app: Application):
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
    asyncio.create_task(notification_dispatcher(app))
    asyncio.create_task(position_close_notifier(app))
    asyncio.create_task(promotion_loop(app))
    asyncio.create_task(claim_sweeper_loop(app))
    asyncio.create_task(watch_sweeper_loop(app))


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
