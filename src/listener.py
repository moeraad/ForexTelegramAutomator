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
from src.ai_triage import TriageClient
from src.config import (
    AI_TRIAGE_ENABLED,
    BACKFILL_MAX_AGE_MIN,
    DEFAULT_AUTO_EXECUTE_DELAY_SEC,
    LOGS_DIR,
)
from src.db import connect, init_schema, get_setting, set_setting
from src.llm_provider import default_interpreter_model, default_triage_model
from src.logging_setup import configure_logging
from src.orchestrator import process_message

log = configure_logging("listener")


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
    triage: TriageClient | None = None,
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
                    triage=triage,
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
    # Provider guard: fail fast on missing API key before Telethon auth
    # so the user doesn't burn session credentials on a broken config.
    if config.AI_PROVIDER == "openai":
        if not config.OPENAI_API_KEY:
            raise SystemExit(
                "OPENAI_API_KEY must be set when AI_PROVIDER=openai."
            )
    elif config.AI_PROVIDER == "anthropic":
        if not config.ANTHROPIC_API_KEY:
            raise SystemExit(
                "ANTHROPIC_API_KEY must be set when AI_PROVIDER=anthropic."
            )
    else:
        raise SystemExit(
            f"Unknown AI_PROVIDER={config.AI_PROVIDER!r}; "
            "expected 'anthropic' or 'openai'."
        )

    conn = connect(config.DB_PATH)
    init_schema(conn)
    interp_model = default_interpreter_model()
    triage_model = default_triage_model()
    ai = AIClient(model=interp_model)
    triage = TriageClient(model=triage_model) if AI_TRIAGE_ENABLED else None
    log.info("ai provider=%s interpreter=%s", config.AI_PROVIDER, interp_model)
    if triage is not None:
        log.info("ai triage enabled: model=%s", triage_model)
    ai_log_path = LOGS_DIR / "ai_calls.jsonl"

    # connection_retries=-1 tells Telethon to retry forever instead of bailing
    # after the default 5 attempts (which surfaced as ConnectionError and crashed
    # the listener). auto_reconnect handles transient drops without re-auth.
    client = TelegramClient(
        config.TG_SESSION_NAME,
        config.TG_API_ID,
        config.TG_API_HASH,
        connection_retries=-1,
        retry_delay=5,
        auto_reconnect=True,
        request_retries=5,
    )
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
                lambda: process_message(
                    conn, ai, msg.id, config.TG_WATCHED_CHAT_ID,
                    sender_name, text, ai_log_path, delay,
                    triage=triage,
                )
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
            lambda: replay_missed_messages(
                conn, ai, missed,
                chat_id=config.TG_WATCHED_CHAT_ID,
                ai_log_path=ai_log_path,
                auto_execute_delay_sec=delay,
                cap_minutes=BACKFILL_MAX_AGE_MIN,
                now=now,
                triage=triage,
            )
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

    # Supervisor: Telethon's connection_retries=-1 should prevent ConnectionError
    # from ever escaping, but wrap run_until_disconnected anyway so a stray
    # network fault (DNS flap, laptop sleep, ISP hiccup) doesn't kill the
    # listener. On reconnect we re-run the missed-message backfill so anything
    # that arrived during the outage is replayed, capped by BACKFILL_MAX_AGE_MIN.
    backoff = 5
    while True:
        try:
            await client.run_until_disconnected()
            log.info("telegram client disconnected cleanly; exiting")
            return
        except (ConnectionError, OSError) as e:
            log.warning("telegram disconnected: %s — reconnecting in %ss", e, backoff)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            try:
                if not client.is_connected():
                    await client.connect()
            except Exception:
                log.exception("reconnect attempt failed; will retry")
                continue
            backoff = 5  # reset after a successful reconnect

            # Replay anything that arrived during the outage.
            try:
                last_seen = int(get_setting(conn, "last_seen_tg_msg_id") or "0")
                missed = await _collect_missed(
                    client, config.TG_WATCHED_CHAT_ID, last_seen
                )
                if missed:
                    delay = int(get_setting(conn, "auto_execute_delay_sec")
                                or str(DEFAULT_AUTO_EXECUTE_DELAY_SEC))
                    now = datetime.now(timezone.utc)
                    processed, skipped = await asyncio.to_thread(
                        lambda: replay_missed_messages(
                            conn, ai, missed,
                            chat_id=config.TG_WATCHED_CHAT_ID,
                            ai_log_path=ai_log_path,
                            auto_execute_delay_sec=delay,
                            cap_minutes=BACKFILL_MAX_AGE_MIN,
                            now=now,
                            triage=triage,
                        )
                    )
                    set_setting(conn, "last_seen_tg_msg_id",
                                str(max(m.tg_message_id for m in missed)))
                    log.info("post-reconnect replay: processed=%s skipped=%s",
                             processed, skipped)
            except Exception:
                log.exception("post-reconnect replay failed; continuing live")


if __name__ == "__main__":
    asyncio.run(main())
