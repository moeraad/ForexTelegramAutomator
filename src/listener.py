import asyncio
import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, events

from src import config
from src.ai import AIClient
from src.config import (
    BACKFILL_MAX_AGE_MIN,
    DEFAULT_AUTO_EXECUTE_DELAY_SEC,
    LOGS_DIR,
)
from src.db import connect, init_schema, get_setting, set_setting
from src.orchestrator import process_message

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [listener] %(levelname)s %(message)s")
log = logging.getLogger(__name__)


@dataclass(frozen=True)
class MissedMessage:
    tg_message_id: int
    sender: str
    text: str
    date: datetime


def replay_missed_messages(
    conn: sqlite3.Connection,
    ai: AIClient,
    messages: Iterable[MissedMessage],
    *,
    chat_id: int,
    ai_log_path: Path,
    auto_execute_delay_sec: int,
    cap_minutes: int,
    now: datetime,
) -> tuple[int, int]:
    """Replay missed messages through the AI pipeline with an age cap.

    Messages fresher than cap_minutes are fed to process_message (flagged
    is_backfill=True). Stale messages are archived with is_backfill=1 but
    not processed — price has moved too far for the signal to be safe.

    Returns (processed_count, skipped_count).
    """
    ordered = sorted(messages, key=lambda m: m.tg_message_id)
    cap = timedelta(minutes=cap_minutes)
    processed = 0
    skipped = 0
    for msg in ordered:
        age = now - msg.date
        if age <= cap:
            try:
                process_message(
                    conn, ai, msg.tg_message_id, chat_id,
                    msg.sender, msg.text, ai_log_path,
                    auto_execute_delay_sec,
                    is_backfill=True,
                )
                processed += 1
            except Exception:
                log.exception(
                    "backfill replay failed for tg_msg_id=%s", msg.tg_message_id
                )
        else:
            conn.execute(
                "INSERT OR IGNORE INTO messages"
                "(tg_message_id, chat_id, sender, text, is_backfill) "
                "VALUES(?,?,?,?,1)",
                (msg.tg_message_id, chat_id, msg.sender, msg.text),
            )
            skipped += 1
    return processed, skipped


async def _resolve_sender(source) -> str:
    try:
        sender = await source.get_sender()
        return getattr(sender, "username", None) or getattr(sender, "first_name", "unknown")
    except Exception:
        return "unknown"


async def _collect_missed(client, chat_id: int, min_id: int) -> list[MissedMessage]:
    missed: list[MissedMessage] = []
    async for old_msg in client.iter_messages(chat_id, min_id=min_id):
        sender = await _resolve_sender(old_msg)
        missed.append(MissedMessage(
            tg_message_id=old_msg.id,
            sender=sender,
            text=old_msg.message or "",
            date=old_msg.date,
        ))
    return missed


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

    missed = await _collect_missed(client, config.TG_WATCHED_CHAT_ID, last_seen)

    if not missed:
        log.info("no missed messages to replay")
    elif last_seen == 0:
        # First-ever launch: do NOT replay history through the AI. Just archive
        # and warn once so the user is aware.
        for m in missed:
            conn.execute(
                "INSERT OR IGNORE INTO messages"
                "(tg_message_id, chat_id, sender, text, is_backfill) "
                "VALUES(?,?,?,?,1)",
                (m.tg_message_id, config.TG_WATCHED_CHAT_ID, m.sender, m.text),
            )
        conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(NULL, 'ALERT', ?, 'pending')",
            (json.dumps({
                "level": "warning",
                "text": f"First launch: archived {len(missed)} historical messages "
                        "without AI processing to avoid stale-history replay."
            }),),
        )
        log.info("first launch; archived %s historical messages (no AI)", len(missed))
    else:
        delay = int(get_setting(conn, "auto_execute_delay_sec")
                    or str(DEFAULT_AUTO_EXECUTE_DELAY_SEC))
        now = datetime.now(timezone.utc)
        processed, skipped = await asyncio.to_thread(
            replay_missed_messages,
            conn, ai, missed,
            chat_id=config.TG_WATCHED_CHAT_ID,
            ai_log_path=ai_log_path,
            auto_execute_delay_sec=delay,
            cap_minutes=BACKFILL_MAX_AGE_MIN,
            now=now,
        )
        conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(NULL, 'ALERT', ?, 'pending')",
            (json.dumps({
                "level": "info" if skipped == 0 else "warning",
                "text": f"Reconnect replay: processed {processed} missed messages "
                        f"within {BACKFILL_MAX_AGE_MIN}min cap; skipped {skipped} "
                        "older than the cap."
            }),),
        )
        log.info("backfill replay: processed=%s skipped=%s cap_min=%s",
                 processed, skipped, BACKFILL_MAX_AGE_MIN)

    if missed:
        set_setting(conn, "last_seen_tg_msg_id",
                    str(max(m.tg_message_id for m in missed)))

    ready.set()
    log.info("listener live; awaiting new messages")
    await client.run_until_disconnected()


if __name__ == "__main__":
    asyncio.run(main())
