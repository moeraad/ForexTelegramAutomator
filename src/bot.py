import asyncio
import json
import logging
import sqlite3
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
)
from src import config
from src.db import connect, init_schema, get_setting, set_setting
from src.telegram_format import render_action_notification
from src.promoter import promote_due_actions

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [bot] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


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
        "VALUES('CLOSE_ALL', ?, 'pending', datetime('now'))",
        (payload,),
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
    """Polls for unnotified actions and DMs the owner."""
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            rows = conn.execute(
                "SELECT id, action_type, payload_json, source_msg_id "
                "FROM actions WHERE notified_at IS NULL "
                "AND status IN ('pending','rejected','cancelled') "
                "ORDER BY id ASC LIMIT 20"
            ).fetchall()
            for r in rows:
                payload = json.loads(r["payload_json"])
                src = ""
                if r["source_msg_id"]:
                    m = conn.execute(
                        "SELECT text FROM messages WHERE id=?", (r["source_msg_id"],)
                    ).fetchone()
                    if m:
                        src = m["text"]
                delay = int(get_setting(conn, "auto_execute_delay_sec") or "30")
                text = render_action_notification(
                    r["id"], r["action_type"], payload, src, delay
                )
                kb = _kb_for_action(r["id"], r["action_type"])
                await app.bot.send_message(
                    chat_id=config.TG_BOT_OWNER_USER_ID,
                    text=text,
                    reply_markup=kb,
                )
                conn.execute(
                    "UPDATE actions SET notified_at=CURRENT_TIMESTAMP WHERE id=?",
                    (r["id"],),
                )
        except Exception as e:
            log.exception("notification_dispatcher error: %s", e)
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


async def post_init(app: Application):
    asyncio.create_task(notification_dispatcher(app))
    asyncio.create_task(promotion_loop(app))


def main() -> None:
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
    app.run_polling()


if __name__ == "__main__":
    main()
