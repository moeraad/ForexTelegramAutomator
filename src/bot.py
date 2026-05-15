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
from src.promoter import promote_due_actions, release_stale_claims

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
    # Defensive parse: maxsplit=1 handles future op:arg payloads without
    # crashing, and the `op` whitelist makes a malformed callback fail
    # cleanly instead of leaving the button stuck in a spinner.
    parts = q.data.split(":", 1)
    if len(parts) != 2 or parts[0] not in ("cancel", "execute"):
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
                text = render_action_terminal(
                    r["id"], r["action_type"], r["status"],
                    payload, r["ea_response"] or "",
                    actual_entry=actual_entry,
                    actual_volume=actual_volume,
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
            n = release_stale_claims(conn)  # default 300s
            if n:
                log.warning("released %s stale claim(s)", n)
        except Exception as e:
            log.exception("claim_sweeper_loop error: %s", e)
        await asyncio.sleep(15.0)


async def telegram_heartbeat_loop(app: Application):
    """Probes Telegram every 30s via bot.get_me() and writes
    settings.bot_telegram_ok_at on success. The GUI's service-bar reads
    this timestamp to colour the Bot pill — green = recent success,
    amber = stale, red = failing or missing.

    Failure modes (all surface to the pill, none crash the bot):
      - DNS broken -> NetworkError -> heartbeat not updated -> pill goes amber/red
      - Telegram backend slow -> same
      - Bot token revoked -> Unauthorized -> heartbeat not updated -> red
    """
    from datetime import datetime, timezone
    from src.db import set_setting
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            await app.bot.get_me()
            set_setting(
                conn, "bot_telegram_ok_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:  # noqa: BLE001
            # Don't update the timestamp — staleness IS the signal.
            log.debug("telegram_heartbeat: %s: %s", type(e).__name__, e)
        await asyncio.sleep(30.0)


async def macro_feed_loop(app: Application):
    """Periodically fetch the macro snapshot (DXY/10Y/VIX/JPY/oil) and
    persist it under settings.macro_snapshot for the directional-bias
    evaluator (src/ai_evaluator.py::_get_macro_snapshot).

    Cadence: 60s. yfinance is rate-tolerant at this rate (5 small
    daily-bar downloads / minute), and the evaluator's stale threshold
    is 300s — so a single failed cycle is invisible to consumers.

    Failure modes (all silent at this layer; logged at WARNING):
      - yfinance not installed -> fetch_macro_snapshot returns None
      - Yahoo Finance down -> fetch_macro_snapshot returns None
      - Partial outage (some tickers fail) -> partial dict is still
        persisted; evaluator renders the present fields.
    """
    from src.macro import fetch_macro_snapshot
    from src.db import set_setting
    conn: sqlite3.Connection = app.bot_data["conn"]
    while True:
        try:
            snap = await fetch_macro_snapshot()
            if snap is not None:
                set_setting(conn, "macro_snapshot", json.dumps(snap))
                set_setting(conn, "macro_snapshot_at", snap["fetched_at"])
                log.info(
                    "macro_feed: dxy=%s vix=%s tnx=%s (%d/5 tickers)",
                    snap.get("dxy"), snap.get("vix"), snap.get("tnx"),
                    sum(1 for k in ("dxy", "tnx", "vix", "jpy", "oil") if k in snap),
                )
            else:
                log.warning("macro_feed: no tickers fetched this cycle")
        except Exception as e:
            log.exception("macro_feed_loop error: %s", e)
        await asyncio.sleep(60.0)


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
    _supervise(asyncio.create_task(notification_dispatcher(app)), "notification_dispatcher")
    _supervise(asyncio.create_task(position_close_notifier(app)), "position_close_notifier")
    _supervise(asyncio.create_task(promotion_loop(app)), "promotion_loop")
    _supervise(asyncio.create_task(claim_sweeper_loop(app)), "claim_sweeper_loop")
    _supervise(asyncio.create_task(macro_feed_loop(app)), "macro_feed_loop")
    _supervise(asyncio.create_task(telegram_heartbeat_loop(app)), "telegram_heartbeat_loop")


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
