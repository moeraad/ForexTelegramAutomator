import asyncio
import base64
import json
import logging
import sqlite3
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from telethon import TelegramClient, events
from telethon.sessions import StringSession

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
from src.profile_context import ProfileContext, get_profile_context

log = configure_logging("listener")

# Telethon emits a flurry of WARNING-level "Server sent a very old message"
# lines on first connect after a gap (the MTProto envelope check fires on
# getDifference catch-up packets while the time_offset auto-calibration
# converges). The warnings are benign once the connection settles — see
# the post-init second-pass catchup below for the actual safety net that
# prevents lost messages. Demote those two specific loggers to ERROR so
# the listener log isn't drowned in catch-up chatter.
logging.getLogger("telethon.network.mtprotostate").setLevel(logging.ERROR)
logging.getLogger("telethon.network.mtprotosender").setLevel(logging.ERROR)


@dataclass(frozen=True)
class MissedMessage:
    tg_message_id: int
    sender: str
    text: str
    date: datetime


# Step 4 of multi-channel plan: dispatch via POST /incoming_message instead
# of calling process_message directly. The listener-side function below is
# transitional — Step 5 replaces this whole module with shared_listener.py
# and uses the same wire contract from a top-level shared process.


@dataclass(frozen=True)
class _ApiDispatchTarget:
    """Resolved API endpoint + channel/route ids the listener POSTs each message to.

    Step 11 adds ``route_id`` + ``sizing_multiplier`` to support mirror
    routing (1 channel → N destinations). Each destination's target
    carries its own ``route_id`` so the receiving API can tag actions
    accordingly; ``sizing_multiplier`` rides along for the EA execution
    path (Step 20 deferred — value plumbed today, applied later).

    Step 21: ``fallback`` (when present) is the secondary
    ``_ApiDispatchTarget`` the listener tries if the primary POST fails.
    ``destination_id`` is the resolved primary destination id (needed
    by the failover wire so the fallback API can tag actions with
    ``failover_from``). ``fallback`` is itself an ``_ApiDispatchTarget``
    so the failover path reuses the same POST helper without special
    casing.
    """
    url: str
    channel_id: str
    token: str  # may be empty string when no auth configured
    route_id: str = ""
    sizing_multiplier: float = 1.0
    destination_id: str = ""
    fallback: "_ApiDispatchTarget | None" = None


def _resolve_api_dispatch_target() -> _ApiDispatchTarget | None:
    """Look up the API URL, channel_id, and auth token from v2 config + settings.

    Returns None when:
      - v2 config absent (fresh install pre-migration) — listener should
        fall back to calling process_message directly (legacy path)
      - No Destination matches this listener's DB_PATH
      - Destination has 0 or >1 enabled Routes (ambiguous in current scope)

    A return of None is non-fatal; the caller falls back to the in-process
    orchestrator call.
    """
    from pathlib import Path as _Path
    from src import config_v2

    cfg_path = config_v2.config_path()
    if not config_v2.is_v2(cfg_path):
        return None
    cfg = config_v2.load_v2(cfg_path)
    if cfg is None:
        return None
    target_db = _Path(config.DB_PATH).resolve()
    matching = [d for d in cfg.destinations
                if d.db_path and _Path(d.db_path).resolve() == target_db]
    if len(matching) != 1:
        return None
    dest = matching[0]
    routes = [r for r in cfg.routes_for_destination(dest.id) if r.enabled]
    if len(routes) != 1:
        return None
    route = routes[0]

    # API URL: prefer the destination's stored host/port (the v1 compat
    # shim reads from the same settings table we'd read here anyway).
    # Fall back to module-level config values populated from DB settings.
    host = (config.API_HOST or "").strip() or "127.0.0.1"
    port = int(config.API_PORT) if config.API_PORT else 8765
    url = f"http://{host}:{port}/incoming_message"

    # Auth token: listener_shared_token wins, falls back to ea_shared_token
    # so single-stack operators don't need to configure two secrets.
    token = (config.LISTENER_SHARED_TOKEN or config.EA_SHARED_TOKEN or "").strip()

    return _ApiDispatchTarget(url=url, channel_id=route.channel_id, token=token)


def _post_incoming_message(
    target: _ApiDispatchTarget,
    *,
    tg_chat_id: int,
    tg_message_id: int,
    text: str,
    sender: str,
    received_at: str,
    is_backfill: bool,
    max_retries: int = 3,
    base_backoff: float = 0.5,
    failover_from_destination_id: str = "",
    reply_to_tg_message_id: int | None = None,
    image_b64: str | None = None,
) -> bool:
    """POST a message to the API's /incoming_message endpoint.

    Returns True on 2xx response; False on permanent failure. Retries
    transient errors (connection refused, 5xx) with exponential backoff.
    4xx responses do NOT retry — they indicate a wire-contract bug, not
    a transient outage; the message is dropped with an error log.

    Step 21: ``failover_from_destination_id`` is set when this POST is
    the fallback retry after the primary destination's POST failed.
    The receiving API stamps every resulting action's payload with
    ``failover_from`` so the operator can spot failover events in
    DMs / journal.
    """
    body = {
        "channel_id": target.channel_id,
        "tg_chat_id": tg_chat_id,
        "tg_message_id": tg_message_id,
        "text": text,
        "sender": sender,
        "received_at": received_at,
        "is_backfill": is_backfill,
        # Step 11: include route_id + sizing_multiplier so the receiving
        # API can tag actions with the originating route (matters for
        # mirror routing where one channel fans out to N destinations).
        "route_id": target.route_id,
        "sizing_multiplier": target.sizing_multiplier,
        # Step 21: empty string when this is a primary POST.
        "failover_from_destination_id": failover_from_destination_id,
        # Telegram reply_to_msg_id (if the message is a reply to a prior
        # one in the same chat). The API persists it so state_summary
        # can prepend the parent's text to the SYSTEM STATE block.
        "reply_to_tg_message_id": reply_to_tg_message_id,
        # Chart image for image-fallback AI path (base64-encoded JPEG/PNG).
        # None when no photo was attached or download failed.
        "image_b64": image_b64,
    }
    data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if target.token:
        headers["X-Listener-Token"] = target.token

    last_err: str | None = None
    for attempt in range(max_retries):
        req = urllib.request.Request(target.url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                if 200 <= resp.status < 300:
                    return True
                last_err = f"HTTP {resp.status}"
        except urllib.error.HTTPError as e:
            # Client errors (4xx other than 408/429) won't succeed on retry.
            if 400 <= e.code < 500 and e.code not in (408, 429):
                log.error(
                    "incoming_message POST permanent failure: HTTP %s tg_msg_id=%s",
                    e.code, tg_message_id,
                )
                return False
            last_err = f"HTTP {e.code}"
        except urllib.error.URLError as e:
            last_err = str(e.reason)
        except (OSError, TimeoutError) as e:
            last_err = str(e)

        if attempt < max_retries - 1:
            time.sleep(base_backoff * (2 ** attempt))

    log.error(
        "incoming_message POST exhausted retries: tg_msg_id=%s last_err=%s",
        tg_message_id, last_err,
    )
    return False


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
    profile: "ProfileContext | None" = None,
    dispatch_target: "_ApiDispatchTarget | None" = None,
) -> tuple[int, int]:
    """Replay missed messages through the AI pipeline with an age cap.

    Messages fresher than cap_minutes are fed to process_message (flagged
    is_backfill=True). Stale messages are archived with is_backfill=1 but
    not processed — price has moved too far for the signal to be safe.

    When ``dispatch_target`` is set the listener routes via API POST
    instead of calling process_message directly (Step 4 of multi-channel
    plan); identical end-to-end behavior, the orchestrator just runs in
    the API process.

    Returns (processed_count, skipped_count).
    """
    # Day-2 cleanup: refresh ProfileContext once at the top of replay so a
    # mid-session profile edit takes effect on the next backfill batch.
    # No-op (same object) when the file hasn't changed.
    if profile is not None:
        try:
            from src.profile_context import refresh_if_stale
            profile = refresh_if_stale(profile)
        except Exception:
            log.exception(
                "replay_missed_messages: profile refresh failed; using cached",
            )
    ordered = sorted(messages, key=lambda m: m.tg_message_id)
    cap = timedelta(minutes=cap_minutes)
    processed = 0
    skipped = 0
    for msg in ordered:
        age = now - msg.date
        if age <= cap:
            try:
                if dispatch_target is not None:
                    posted = _post_incoming_message(
                        dispatch_target,
                        tg_chat_id=chat_id,
                        tg_message_id=msg.tg_message_id,
                        text=msg.text,
                        sender=msg.sender,
                        received_at=msg.date.isoformat(),
                        is_backfill=True,
                    )
                    if not posted:
                        log.warning(
                            "backfill replay DROPPED tg_msg_id=%s (API unreachable)",
                            msg.tg_message_id,
                        )
                        continue
                else:
                    process_message(
                        conn, ai, msg.tg_message_id, chat_id,
                        msg.sender, msg.text, ai_log_path,
                        auto_execute_delay_sec,
                        is_backfill=True,
                        triage=triage,
                        profile=profile,
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


async def _telegram_heartbeat_loop(client, conn) -> None:
    """Pings Telegram via Telethon's get_me() every 30s and writes
    settings.listener_telegram_ok_at on success. The GUI's service-bar
    reads this timestamp to colour the Listener pill — green if the
    timestamp is fresh, amber/red if it goes stale.

    Failure modes don't crash the listener; they just stop refreshing
    the heartbeat, which IS the signal.
    """
    from datetime import datetime, timezone
    from src.db import set_setting
    while True:
        try:
            await client.get_me()
            set_setting(
                conn, "listener_telegram_ok_at",
                datetime.now(timezone.utc).isoformat(),
            )
        except Exception as e:  # noqa: BLE001
            log.debug("listener_heartbeat: %s: %s", type(e).__name__, e)
        await asyncio.sleep(30.0)


async def _second_pass_catchup(
    client,
    conn: sqlite3.Connection,
    ai: AIClient,
    ai_log_path: Path,
    triage: TriageClient | None,
    *,
    delay_sec: float = 6.0,
    profile: ProfileContext | None = None,
    dispatch_target: "_ApiDispatchTarget | None" = None,
) -> None:
    """Fire-and-forget second backfill pass to catch messages dropped by
    Telethon's MTProto envelope checks during the first-connect burst.

    Runs `delay_sec` after listener goes live (default 6s — long enough
    for Telethon's time_offset to calibrate and getDifference cycles to
    settle, short enough that a recovered message still falls inside
    BACKFILL_MAX_AGE_MIN). Reuses the same _collect_missed +
    replay_missed_messages path the supervisor uses on reconnect, so
    behavior is identical and dedup is automatic.
    """
    try:
        await asyncio.sleep(delay_sec)
        last_seen = int(get_setting(conn, "last_seen_tg_msg_id") or "0")
        missed = await _collect_missed(
            client, config.TG_WATCHED_CHAT_ID, last_seen
        )
        if not missed:
            log.info("second-pass catchup: nothing new beyond last_seen=%s", last_seen)
            return
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
                profile=profile,
                dispatch_target=dispatch_target,
            )
        )
        set_setting(conn, "last_seen_tg_msg_id",
                    str(max(m.tg_message_id for m in missed)))
        log.info("second-pass catchup: processed=%s skipped=%s "
                 "(safety net for dropped MTProto envelopes)",
                 processed, skipped)
    except Exception:
        log.exception("second-pass catchup failed; live handler unaffected")


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
    # ProfileContext (Step 3 of multi-channel plan): load once at startup,
    # thread to every process_message call. Eliminates the runtime
    # config.CHANNEL_PROFILE reads that used to happen inside the
    # orchestrator's prefilter step + ai.SYSTEM_PROMPT module global.
    # `get_profile_context` is cached on path, so the listener restarting
    # for any reason re-uses the parsed dict instantly.
    profile_ctx: ProfileContext | None = None
    try:
        from src.ai import _resolve_profile_path
        profile_path = _resolve_profile_path()
        if profile_path.exists():
            profile_ctx = get_profile_context(
                profile_path,
                name=config.CHANNEL_PROFILE or profile_path.stem,
            )
            log.info(
                "profile context loaded: name=%s symbol=%s path=%s",
                profile_ctx.name, profile_ctx.symbol, profile_ctx.path,
            )
    except (RuntimeError, FileNotFoundError, OSError) as e:
        # No profile configured yet (fresh install) — orchestrator falls
        # back to module-level globals (which will also fail clearly when
        # an AI call is attempted). Don't crash startup.
        log.warning("profile context unavailable, using legacy globals: %s", e)
    ai = AIClient(
        model=interp_model,
        system_prompt=(profile_ctx.system_prompt if profile_ctx else None),
    )
    triage = (
        TriageClient(
            model=triage_model,
            system_prompt=(profile_ctx.triage_prompt if profile_ctx else None),
        ) if AI_TRIAGE_ENABLED else None
    )
    log.info("ai provider=%s interpreter=%s", config.AI_PROVIDER, interp_model)
    if triage is not None:
        log.info("ai triage enabled: model=%s", triage_model)
    ai_log_path = LOGS_DIR / "ai_calls.jsonl"

    # Step 4 of multi-channel plan: when the v2 config tells us where to
    # POST, route messages through the API instead of running the
    # orchestrator in-process. Falls back to the legacy in-process path
    # when v2 isn't configured (fresh install pre-migration).
    dispatch_target = _resolve_api_dispatch_target()
    if dispatch_target is not None:
        log.info(
            "listener dispatch via API: url=%s channel=%s auth=%s",
            dispatch_target.url, dispatch_target.channel_id,
            "on" if dispatch_target.token else "off",
        )
    else:
        log.info("listener dispatch in-process (legacy: no v2 routing config)")

    # Session is persisted in the DB as the DPAPI-encrypted tg_session_blob
    # written by the setup wizard. Loading it as a StringSession lets the
    # listener restart without re-prompting for the Telegram login code.
    #
    # Wait-instead-of-crash: on a fresh stack install the operator launches
    # the services BEFORE running the Telegram wizard. Raising here would
    # exit the process inside NSSM's AppThrottle window, NSSM would mark the
    # service SERVICE_PAUSED, and the next `nssm start` would surface
    # "Unexpected status SERVICE_PAUSED in response to START control."
    # Polling keeps the process alive and stable; it picks up the session
    # the moment the wizard writes it.
    from src import db_settings
    session_blob = db_settings.get_str(Path(config.DB_PATH), "tg_session_blob", "")
    if not session_blob:
        log.warning(
            "tg_session_blob is empty — waiting for the setup wizard to "
            "persist a Telegram session (polling every 10s). Open the GUI "
            "and run the Telegram wizard to proceed."
        )
        while not session_blob:
            await asyncio.sleep(10)
            session_blob = db_settings.get_str(Path(config.DB_PATH), "tg_session_blob", "")
        log.info("tg_session_blob detected — continuing listener startup")

    # connection_retries=-1 tells Telethon to retry forever instead of bailing
    # after the default 5 attempts (which surfaced as ConnectionError and crashed
    # the listener). auto_reconnect handles transient drops without re-auth.
    client = TelegramClient(
        StringSession(session_blob),
        config.TG_API_ID,
        config.TG_API_HASH,
        connection_retries=-1,
        retry_delay=5,
        auto_reconnect=True,
        request_retries=5,
    )
    await client.connect()
    if not await client.is_user_authorized():
        raise RuntimeError(
            "Telegram session in tg_session_blob is no longer authorized "
            "(probably revoked from another device or expired). Re-run the "
            "setup wizard to log in again."
        )

    last_seen = int(get_setting(conn, "last_seen_tg_msg_id") or "0")
    log.info("listener started; last_seen_tg_msg_id=%s", last_seen)
    from src.notify import notify_owner  # local import: avoids circulars on test imports
    notify_owner(
        f"📡 Listener started — chat={config.TG_WATCHED_CHAT_ID} "
        f"provider={config.AI_PROVIDER} model={interp_model} "
        f"last_seen_msg_id={last_seen}"
    )

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
            # Photo download for image fallback (geometric-inconsistency correction).
            # Download is best-effort: failures are silently swallowed so a photo
            # download error never blocks signal processing.
            _MAX_IMAGE_BYTES = 10 * 1024 * 1024  # 10 MB guard
            image_bytes_raw: bytes | None = None
            if msg.photo or (msg.media and hasattr(msg.media, "photo")):
                try:
                    raw_img = await msg.download_media(bytes)
                    if raw_img and len(raw_img) <= _MAX_IMAGE_BYTES:
                        image_bytes_raw = raw_img
                    elif raw_img:
                        log.warning(
                            "tg_msg_id=%s photo too large (%d bytes) — skipping image fallback",
                            msg.id, len(raw_img),
                        )
                except Exception:
                    log.warning(
                        "tg_msg_id=%s photo download failed — continuing without image",
                        msg.id, exc_info=True,
                    )
            # Telethon exposes the parent message id on replies as
            # event.message.reply_to_msg_id (Telethon 1.x) or via
            # event.message.reply_to.reply_to_msg_id (newer schema).
            # Either path; fallback to None on non-reply messages.
            reply_to_id = getattr(msg, "reply_to_msg_id", None)
            if reply_to_id is None:
                reply_to_obj = getattr(msg, "reply_to", None)
                if reply_to_obj is not None:
                    reply_to_id = getattr(reply_to_obj, "reply_to_msg_id", None)
            log.info("received tg_msg_id=%s reply_to=%s text=%r",
                     msg.id, reply_to_id, text[:80])
            if dispatch_target is not None:
                received_at = datetime.now(timezone.utc).isoformat()
                _image_b64 = base64.b64encode(image_bytes_raw).decode() if image_bytes_raw else None
                posted = await asyncio.to_thread(
                    lambda: _post_incoming_message(
                        dispatch_target,
                        tg_chat_id=config.TG_WATCHED_CHAT_ID,
                        tg_message_id=msg.id,
                        text=text,
                        sender=sender_name,
                        received_at=received_at,
                        is_backfill=False,
                        reply_to_tg_message_id=reply_to_id,
                        image_b64=_image_b64,
                    )
                )
                if posted:
                    log.info("dispatched tg_msg_id=%s via API", msg.id)
                else:
                    log.warning("dispatched tg_msg_id=%s DROPPED (API unreachable)", msg.id)
            else:
                # Day-2 cleanup: refresh profile before per-message
                # dispatch so the GUI Profile Editor's save propagates
                # without a service restart. No-op when the file hasn't
                # changed.
                live_profile = profile_ctx
                if live_profile is not None:
                    try:
                        from src.profile_context import refresh_if_stale
                        live_profile = refresh_if_stale(live_profile)
                    except Exception:
                        log.exception(
                            "profile refresh failed for tg_msg_id=%s; "
                            "using cached", msg.id,
                        )
                ids = await asyncio.to_thread(
                    lambda: process_message(
                        conn, ai, msg.id, config.TG_WATCHED_CHAT_ID,
                        sender_name, text, ai_log_path, delay,
                        triage=triage,
                        profile=live_profile,
                        image_bytes=image_bytes_raw,
                        has_image=(image_bytes_raw is not None),
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
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, executed_at) "
            "VALUES(NULL, 'ALERT', ?, 'executed', "
            "  strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))",
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
                profile=profile_ctx,
                dispatch_target=dispatch_target,
            )
        )
        conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, executed_at) "
            "VALUES(NULL, 'ALERT', ?, 'executed', "
            "  strftime('%Y-%m-%dT%H:%M:%S+00:00','now'))",
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

    # Second-pass catchup: Telethon's first connect after a gap can drop
    # MTProto envelopes that fail the "very old message" check, including
    # ones carrying real new updates. The first message after launch can
    # be silently lost as a result. Wait a few seconds for Telethon's
    # time_offset to calibrate and the connection to stabilize, then
    # re-run the missed-message backfill. Anything dropped during the
    # initial burst gets picked up here; anything already processed is
    # deduped by the messages.UNIQUE(chat_id, tg_message_id) constraint
    # and the orchestrator's fingerprint guard.
    asyncio.create_task(_second_pass_catchup(
        client, conn, ai, ai_log_path, triage,
        profile=profile_ctx, dispatch_target=dispatch_target,
    ))
    asyncio.create_task(_telegram_heartbeat_loop(client, conn))

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
                            profile=profile_ctx,
                            dispatch_target=dispatch_target,
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
