import asyncio
import json
import logging
from telethon import TelegramClient, events
from src import config
from src.db import connect, init_schema, get_setting, set_setting
from src.ai import AIClient
from src.orchestrator import process_message
from src.config import LOGS_DIR, DEFAULT_AUTO_EXECUTE_DELAY_SEC

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [listener] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


async def _resolve_sender(source) -> str:
    try:
        sender = await source.get_sender()
        return getattr(sender, "username", None) or getattr(sender, "first_name", "unknown")
    except Exception:
        return "unknown"


async def main() -> None:
    conn = connect(config.DB_PATH)
    init_schema(conn)
    ai = AIClient(model=config.ANTHROPIC_MODEL)
    ai_log_path = LOGS_DIR / "ai_calls.jsonl"

    client = TelegramClient(config.TG_SESSION_NAME, config.TG_API_ID, config.TG_API_HASH)
    await client.start(phone=config.TG_PHONE)

    last_seen = int(get_setting(conn, "last_seen_tg_msg_id") or "0")
    log.info("listener started; last_seen_tg_msg_id=%s", last_seen)

    ready = asyncio.Event()

    @client.on(events.NewMessage(chats=config.TG_WATCHED_CHAT_ID))
    async def handler(event):
        # Block live processing until backfill finishes to keep chat history ordered.
        await ready.wait()
        try:
            msg = event.message
            delay = int(get_setting(conn, "auto_execute_delay_sec")
                        or str(DEFAULT_AUTO_EXECUTE_DELAY_SEC))
            sender_name = await _resolve_sender(event)
            text = msg.message or ""
            log.info("received tg_msg_id=%s text=%r", msg.id, text[:80])
            ids = await asyncio.to_thread(
                process_message, conn, ai, msg.id, config.TG_WATCHED_CHAT_ID,
                sender_name, text, ai_log_path, delay,
            )
            if ids:
                log.info("inserted action ids=%s", ids)
            set_setting(conn, "last_seen_tg_msg_id", str(msg.id))
        except Exception:
            log.exception("handler failed for tg_msg_id=%s", getattr(event.message, "id", "?"))

    # Backfill any missed messages while offline (do NOT process via AI)
    backfilled = 0
    async for old_msg in client.iter_messages(config.TG_WATCHED_CHAT_ID, min_id=last_seen):
        sender_name = await _resolve_sender(old_msg)
        text = old_msg.message or ""
        conn.execute(
            "INSERT OR IGNORE INTO messages(tg_message_id, chat_id, sender, text, is_backfill) "
            "VALUES(?,?,?,?,1)",
            (old_msg.id, config.TG_WATCHED_CHAT_ID, sender_name, text),
        )
        backfilled += 1

    if backfilled > 0:
        conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(NULL, 'ALERT', ?, 'pending')",
            (json.dumps({
                "level": "warning",
                "text": f"You missed {backfilled} messages while listener was offline. "
                        "These were NOT processed by AI to avoid stale signals."
            }),),
        )
        log.info("backfilled %s messages (no AI processing)", backfilled)

    ready.set()
    log.info("listener live; awaiting new messages")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
