"""v2-aware listener entry point.

Reads the v2 ``stacks_config.json``, validates routing constraints, and
spawns one Telethon session per Account. Each session subscribes to all
enabled Channels owned by that account; each message is dispatched to
the API of each Destination that the matching Route points at, via
``POST /incoming_message`` (Step 4 wire contract).

Architectural scope by step:

| Step | Scope this module supports                                              |
|------|-------------------------------------------------------------------------|
| 5    | 1 account, N channels — Telethon work delegated to legacy listener.main |
| 6    | 1 account, N channels — multi-channel-per-session handlers registered   |
|      |   here (replaces the legacy delegation)                                 |
| 11+  | N→M routes — listener's POST loop fans out to all enabled destinations  |
| 13   | M accounts — top-level loop spawns one Telethon session per account     |

Step 5 (current): we delegate to ``listener.main()`` for the actual
Telethon work, because that code is already the working single-account/
single-channel implementation. The v1→v2 migration guarantees that
``config.TG_WATCHED_CHAT_ID`` matches the (single) ``Channel.chat_id``
for migrated single-stack setups, so delegation is correctness-preserving.

Why this module exists today (before its full machinery is built): it is
the new public entry point. Step 8 will install the NSSM service to run
``python -m src.shared_listener`` instead of ``python -m src.listener``;
having the module ready now lets Step 8 land without a coordinated change.
The existing ``listener.py`` entry stays callable for backward compat
(legacy NSSM services, ``launch.bat``, ``gui_launcher.py``).
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from pathlib import Path

from src import config, config_v2
from src.config_v2 import Account, Channel, ConfigV2
from src.logging_setup import configure_logging

log = configure_logging("shared_listener")


def _resolve_v2_or_none() -> ConfigV2 | None:
    """Load the v2 config if present; return None if absent / unreadable.

    A None return is a signal to delegate to the legacy single-stack
    listener — the right behavior on fresh installs that haven't been
    through the GUI's auto-migration yet.
    """
    cfg_path = config_v2.config_path()
    if not config_v2.is_v2(cfg_path):
        return None
    try:
        return config_v2.load_v2(cfg_path)
    except Exception:  # noqa: BLE001 — fall back to legacy on any read failure
        log.exception("v2 config read failed; delegating to legacy listener")
        return None


def _validate_current_scope(
    cfg: ConfigV2, account_id: str | None = None,
) -> tuple[Account, tuple[Channel, ...]]:
    """Pick THE account this listener process binds to + its enabled channels.

    Step 13: this process serves ONE Telegram user account. When the
    v2 config has multiple accounts, ``account_id`` selects which one
    (passed via ``--account-id`` on the command line). When ``account_id``
    is None AND the config has exactly one account, that's the one
    (single-account back-compat — preserves the Step 5 behavior so
    operators who never set --account-id still work).

    Raises SystemExit with a clear pointer when:
      - the config has no accounts at all
      - account_id is required (N>1 accounts) but missing
      - account_id is specified but doesn't match any configured account
      - the chosen account has no enabled channels
    """
    if not cfg.accounts:
        raise SystemExit(
            "shared_listener: v2 config has no accounts. "
            "Run the GUI setup wizard or run the v1→v2 migration "
            "(triggered by opening the GUI on an existing stack)."
        )

    if account_id:
        account = cfg.account(account_id)
        if account is None:
            known = ", ".join(a.id for a in cfg.accounts)
            raise SystemExit(
                f"shared_listener: --account-id {account_id!r} not found "
                f"in v2 config. Known account ids: {known}"
            )
    elif len(cfg.accounts) == 1:
        # Single-account back-compat (Step 5 behavior): no --account-id
        # needed when there's only one account in the config.
        account = cfg.accounts[0]
    else:
        names = ", ".join(a.id for a in cfg.accounts)
        raise SystemExit(
            f"shared_listener: v2 config has {len(cfg.accounts)} accounts "
            f"({names}). With multiple accounts you MUST pass "
            "--account-id <id> on the command line (Step 13 of multi-channel "
            "plan: one listener service per Account). The bootstrap install "
            "helper does this automatically when registering NSSM services."
        )

    channels = tuple(c for c in cfg.channels_for_account(account.id) if c.enabled)
    if not channels:
        raise SystemExit(
            f"shared_listener: account {account.id!r} has no enabled channels."
        )
    return account, channels


def _resolve_dispatch_for_channel(
    channel: Channel, cfg: ConfigV2,
) -> "list[tuple[Path, object]]":
    """Resolve all (destination_db_path, _ApiDispatchTarget) tuples for a Channel.

    Step 11: returns a LIST — one entry per enabled Route — so the
    runner can fan out a single incoming Telegram message to N
    destinations (mirror routing).

    Returns an empty list when:
      - Channel has no Routes
      - All Routes are disabled
      - All Routes point at destinations with no db_path

    An empty list is non-fatal at the runner level; the runner logs and
    skips this channel rather than crashing.
    """
    from src.listener import _ApiDispatchTarget

    routes = [r for r in cfg.routes_for_channel(channel.id) if r.enabled]
    if not routes:
        return []

    out: list[tuple[Path, _ApiDispatchTarget]] = []
    for route in routes:
        primary = _build_target_for_destination(
            channel=channel, route=route,
            destination_id=route.destination_id, cfg=cfg,
        )
        if primary is None:
            continue
        primary_path, primary_target = primary
        # Step 21: attach the fallback target when configured. A missing
        # fallback (config typo, fallback dest deleted) doesn't prevent
        # primary scheduling — just disables failover for this leg.
        if route.fallback_destination_id:
            fb = _build_target_for_destination(
                channel=channel, route=route,
                destination_id=route.fallback_destination_id, cfg=cfg,
            )
            if fb is not None:
                from dataclasses import replace as _replace
                primary_target = _replace(primary_target, fallback=fb[1])
        out.append((primary_path, primary_target))
    return out


def _build_target_for_destination(
    *, channel: Channel, route, destination_id: str, cfg: ConfigV2,
) -> "tuple[Path, object] | None":
    """Construct one (dest_db_path, _ApiDispatchTarget) tuple.

    Used twice per route at most: once for the primary destination, once
    for the fallback (Step 21). Returns None if the destination is
    missing or has no db_path — caller decides whether to skip the whole
    leg (primary missing) or just drop the fallback (fallback missing).
    """
    from src.listener import _ApiDispatchTarget
    dest = cfg.destination(destination_id)
    if dest is None or not dest.db_path:
        log.warning(
            "channel %s route %s points at unknown/empty destination %s; "
            "skipping this leg",
            channel.id, route.id, destination_id,
        )
        return None
    host = (dest.api_host or "127.0.0.1").strip()
    port = int(dest.api_port) if dest.api_port else None
    if not port:
        port = _read_destination_api_port(Path(dest.db_path)) or 8765
    url = f"http://{host}:{port}/incoming_message"
    token = _read_destination_listener_token(Path(dest.db_path))
    target = _ApiDispatchTarget(
        url=url,
        channel_id=channel.id,
        token=token,
        route_id=route.id,
        sizing_multiplier=route.sizing_multiplier,
        destination_id=dest.id,
    )
    return (Path(dest.db_path), target)


def _resolve_one_dispatch_for_channel(
    channel: Channel, cfg: ConfigV2,
) -> "tuple[Path, object] | None":
    """Single-target convenience for back-compat callers (Step-6 tests).

    Returns the first resolved target or None. New code should use the
    multi-target form above.
    """
    targets = _resolve_dispatch_for_channel(channel, cfg)
    return targets[0] if targets else None


def _read_destination_api_port(db_path: Path) -> int | None:
    """Read api_port from a destination DB's settings table."""
    if not db_path.exists():
        return None
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            row = conn.execute(
                "SELECT value FROM settings WHERE key='api_port'"
            ).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    try:
        return int(row[0])
    except (TypeError, ValueError):
        return None


def _read_destination_listener_token(db_path: Path) -> str:
    """Read listener_shared_token (or fall back to ea_shared_token) from
    a destination's settings table. Returns "" when neither is set."""
    if not db_path.exists():
        return (config.LISTENER_SHARED_TOKEN or config.EA_SHARED_TOKEN or "").strip()
    import sqlite3
    try:
        conn = sqlite3.connect(str(db_path))
        try:
            rows = dict(conn.execute(
                "SELECT key, value FROM settings "
                "WHERE key IN ('listener_shared_token', 'ea_shared_token')"
            ).fetchall())
        finally:
            conn.close()
    except sqlite3.Error:
        return ""
    return (rows.get("listener_shared_token")
            or rows.get("ea_shared_token")
            or "").strip()


def _read_session_from_account_file(account: Account) -> str:
    """Read a per-account StringSession blob from ``account.session_path``.

    "Add Account" dialog writes the blob to a text file at
    ``%APPDATA%/CopyTrades/accounts/<account_id>.session.txt`` so the
    operator can fully auth an Account before wiring it to any
    destination. This helper reads that file as a fallback when the
    destination DB has no ``tg_session_blob`` yet.

    Returns "" on any failure (path empty, file missing, IO error) —
    the caller treats empty the same as "no session" and falls through
    to its polling loop, so a bad fallback never wedges startup.
    """
    if not account.session_path:
        return ""
    try:
        from pathlib import Path as _Path
        p = _Path(account.session_path)
        if not p.exists() or not p.is_file():
            return ""
        return p.read_text(encoding="utf-8").strip()
    except Exception:
        log.exception(
            "failed to read per-account session file %s; ignoring",
            account.session_path,
        )
        return ""


async def _telegram_heartbeat_loop_multi(client, dest_dbs: set[Path]) -> None:
    """Pings Telegram via Telethon's ``get_me()`` every 30s and writes
    ``listener_telegram_ok_at`` into every destination DB the shared
    listener serves.

    The GUI's services-bar reads this timestamp per-stack to colour the
    Listener pill (green when fresh, orange "starting…" when missing,
    red "no telegram" when stale). Mirrors ``_telegram_heartbeat_loop``
    in ``src/listener.py`` but fans out across N destinations because one
    shared listener can feed several stacks.

    Failures don't crash the listener — they just stop refreshing the
    heartbeat, which IS the signal.
    """
    from src import db_settings
    while True:
        try:
            await client.get_me()
            now_iso = datetime.now(timezone.utc).isoformat()
            for dest_db in dest_dbs:
                try:
                    db_settings.set_str(
                        dest_db, "listener_telegram_ok_at", now_iso,
                    )
                except Exception:  # noqa: BLE001
                    log.debug(
                        "listener_heartbeat: failed to write into %s",
                        dest_db, exc_info=True,
                    )
        except Exception as e:  # noqa: BLE001
            log.debug("listener_heartbeat: %s: %s", type(e).__name__, e)
        await asyncio.sleep(30.0)


async def _run_multi_channel(
    account: Account,
    channels: "tuple[Channel, ...]",
    cfg: ConfigV2,
) -> None:
    """Real multi-channel Telethon runner (Step 6).

    One Telethon session, N event handlers. Each handler POSTs to the
    matching destination's API via ``_post_incoming_message``.

    Lifecycle today:
      1. Resolve per-channel dispatch targets (skip channels with no Route)
      2. Load session_blob from the FIRST channel's destination DB
      3. Connect Telethon
      4. Register one event handler per channel
      5. Per-channel backfill on startup
      6. ``run_until_disconnected`` with a basic reconnect loop

    What's NOT in Step 6 (deferred to later):
      - Second-pass-catchup (the legacy listener has it; multi-channel
        version will be added when an operator hits a dropped-envelope
        issue across multiple channels in practice)
      - Cross-channel halt coordination (Step 15)
    """
    # Late imports: keep this module importable without Telethon for tests.
    import sqlite3
    from telethon import TelegramClient, events
    from telethon.sessions import StringSession
    from src import db_settings
    from src.listener import (
        _ApiDispatchTarget,
        _collect_missed,
        _post_incoming_message,
        _resolve_sender,
    )

    # ---- 1. Resolve dispatch targets per channel ------------------------
    # Step 11 (mirror routing): each channel can have N enabled routes,
    # so the value is a LIST of (dest_db, target) tuples. The handler
    # fans out to all of them.
    targets: dict[int, tuple[Channel, list[tuple[Path, _ApiDispatchTarget]]]] = {}
    for ch in channels:
        resolved = _resolve_dispatch_for_channel(ch, cfg)
        if not resolved:
            log.warning(
                "channel %s has no enabled route; skipping (operator must "
                "add a Route in stacks_config.json)", ch.id,
            )
            continue
        targets[ch.chat_id] = (ch, resolved)
        if len(resolved) > 1:
            log.info(
                "channel %s: mirror routing to %d destination(s): %s",
                ch.id, len(resolved),
                ", ".join(t.url for _, t in resolved),
            )
    if not targets:
        raise SystemExit(
            "shared_listener: no channels have valid routes — "
            "every channel was skipped during target resolution."
        )

    # ---- 2. Load session_blob -------------------------------------------
    # Multi-channel under one account shares one Telethon session. The
    # session_blob lives in the destination DB (the v1→v2 migration left
    # it there). Convention: read from the FIRST channel's FIRST
    # destination DB. If the operator runs the wizard against a
    # different destination, it writes session_blob to THAT DB — they
    # need to keep them in sync OR always launch the wizard against
    # the destination the listener reads.
    first_chat_id = next(iter(targets))
    _, first_routes = targets[first_chat_id]
    first_dest_db = first_routes[0][0]
    session_blob = db_settings.get_str(first_dest_db, "tg_session_blob", "")
    if not session_blob:
        # "Add Account" dialog flow: when the operator authed Telethon via
        # the GUI's standalone Add-Account dialog (before wiring a channel
        # to any destination), the session blob was written to a per-account
        # file at account.session_path. Read that as a fallback; mirror it
        # into the destination DB so subsequent starts use the fast path.
        session_blob = _read_session_from_account_file(account)
        if session_blob:
            log.info(
                "tg_session_blob empty in %s — falling back to per-account "
                "file %s; mirroring into the destination DB",
                first_dest_db, account.session_path,
            )
            try:
                db_settings.set_str(
                    first_dest_db, "tg_session_blob", session_blob,
                )
            except Exception:
                log.exception(
                    "failed to mirror per-account session into %s; "
                    "next start will read the file again",
                    first_dest_db,
                )
    if not session_blob:
        log.warning(
            "tg_session_blob empty in %s and no per-account session "
            "fallback found — waiting for the setup wizard or Add-Account "
            "completion. Polling every 10s.", first_dest_db,
        )
        while not session_blob:
            await asyncio.sleep(10)
            session_blob = db_settings.get_str(first_dest_db, "tg_session_blob", "")
            if not session_blob:
                session_blob = _read_session_from_account_file(account)
        log.info("tg_session_blob detected — continuing startup")

    # ---- 3. Connect Telethon --------------------------------------------
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
    log.info("Telethon connected; subscribing to %d channel(s)", len(targets))

    # ---- 4. Register one handler per channel ----------------------------
    ready = asyncio.Event()

    def _make_handler(
        chat_id: int, ch: Channel,
        routes: list[tuple[Path, _ApiDispatchTarget]],
    ):
        @client.on(events.NewMessage(chats=chat_id))
        async def _handler(event):
            await ready.wait()
            try:
                msg = event.message
                text = msg.message or ""
                sender_name = await _resolve_sender(event)
                received_at = datetime.now(timezone.utc).isoformat()
                # Telethon: reply_to_msg_id is set when this message is
                # a reply to a prior one in the same chat. Capture it so
                # state_summary can prepend the parent text — lets the
                # AI resolve pronouns like "cancel that order" against
                # the message they reference.
                reply_to_id = getattr(msg, "reply_to_msg_id", None)
                if reply_to_id is None:
                    reply_to_obj = getattr(msg, "reply_to", None)
                    if reply_to_obj is not None:
                        reply_to_id = getattr(
                            reply_to_obj, "reply_to_msg_id", None,
                        )
                log.info(
                    "recv ch=%s (chat_id=%s) tg_msg_id=%s reply_to=%s "
                    "routes=%d text=%r",
                    ch.id, chat_id, msg.id, reply_to_id, len(routes),
                    text[:80],
                )

                # Step 11: fan out to ALL enabled routes in parallel.
                # asyncio.gather with return_exceptions ensures one
                # slow/failing destination doesn't block the rest.
                # Each leg is its own _post_incoming_message call with
                # its own retry/backoff (route_id distinguishes legs).
                # Step 21: when the primary fails AND a fallback is
                # configured on the route, retry against the fallback
                # destination. Returns (route_id, ok, used_fallback).
                async def _post_one(
                    target: _ApiDispatchTarget,
                ) -> tuple[str, bool, bool]:
                    ok = await asyncio.to_thread(
                        lambda: _post_incoming_message(
                            target,
                            tg_chat_id=chat_id,
                            tg_message_id=msg.id,
                            text=text,
                            sender=sender_name,
                            received_at=received_at,
                            is_backfill=False,
                            reply_to_tg_message_id=reply_to_id,
                        )
                    )
                    if ok or target.fallback is None:
                        return (
                            target.route_id or "(no route_id)",
                            ok, False,
                        )
                    # Primary failed AND fallback is configured: retry.
                    # The fallback POST carries failover_from_destination_id
                    # so the receiving API can tag the action.
                    log.warning(
                        "primary POST failed ch=%s route=%s; trying "
                        "fallback %s",
                        ch.id, target.route_id,
                        target.fallback.destination_id,
                    )
                    fb_ok = await asyncio.to_thread(
                        lambda: _post_incoming_message(
                            target.fallback,
                            tg_chat_id=chat_id,
                            tg_message_id=msg.id,
                            text=text,
                            sender=sender_name,
                            received_at=received_at,
                            is_backfill=False,
                            failover_from_destination_id=target.destination_id,
                            reply_to_tg_message_id=reply_to_id,
                        )
                    )
                    return (
                        target.route_id or "(no route_id)",
                        fb_ok, fb_ok,  # used_fallback iff the retry succeeded
                    )

                results = await asyncio.gather(
                    *[_post_one(target) for _, target in routes],
                    return_exceptions=True,
                )
                for r in results:
                    if isinstance(r, BaseException):
                        log.exception(
                            "fan-out leg crashed for ch=%s tg_msg_id=%s: %r",
                            ch.id, msg.id, r,
                        )
                        continue
                    route_id, ok, used_fb = r
                    if ok and used_fb:
                        log.info(
                            "dispatched ch=%s tg_msg_id=%s route=%s "
                            "via FAILOVER",
                            ch.id, msg.id, route_id,
                        )
                    elif ok:
                        log.info(
                            "dispatched ch=%s tg_msg_id=%s route=%s",
                            ch.id, msg.id, route_id,
                        )
                    else:
                        log.warning(
                            "DROPPED ch=%s tg_msg_id=%s route=%s "
                            "(API unreachable, fallback %s)",
                            ch.id, msg.id, route_id,
                            "also failed" if used_fb else "absent",
                        )
            except Exception:
                log.exception("handler crashed for ch=%s", ch.id)
        return _handler

    for chat_id, (ch, routes) in targets.items():
        _make_handler(chat_id, ch, routes)

    # ---- 5. Per-channel backfill on startup -----------------------------
    # last_seen lives per-destination-DB; with single-channel-per-destination
    # (current scope) the key 'last_seen_tg_msg_id' resolves to the right
    # channel implicitly. For Step 12 (multi-channel-per-destination,
    # deferred) the key will need a channel suffix.
    #
    # Step 11: backfill also fans out. We run one backfill per (channel,
    # destination) leg so each destination's local last_seen advances
    # independently — if dest A is up to date but dest B is behind
    # (was offline), the channel's backfill into B replays via POST.
    for chat_id, (ch, routes) in targets.items():
        for dest_db, target in routes:
            await _backfill_channel_on_startup(
                client, ch, dest_db, target,
                _collect_missed, _post_incoming_message,
            )

    ready.set()
    log.info("backfill complete; handlers live")

    # Heartbeat: write listener_telegram_ok_at to every unique destination
    # DB so each stack's GUI services-bar can colour the Listener pill
    # green. Without this the pill stays orange ("starting…") forever for
    # shared-listener-backed stacks.
    unique_dest_dbs: set[Path] = {
        dest_db for _, routes in targets.values() for dest_db, _ in routes
    }
    asyncio.create_task(
        _telegram_heartbeat_loop_multi(client, unique_dest_dbs)
    )

    # ---- 6. Run forever with basic reconnect ----------------------------
    while True:
        try:
            await client.run_until_disconnected()
            log.warning("Telethon disconnected without exception; reconnecting in 10s")
        except Exception:
            log.exception("Telethon loop crashed; reconnecting in 10s")
        await asyncio.sleep(10)
        try:
            if not client.is_connected():
                await client.connect()
        except Exception:
            log.exception("reconnect failed; will retry on next loop")


async def _backfill_channel_on_startup(
    client, channel: Channel, dest_db: Path, target,
    collect_missed_fn, post_message_fn,
) -> None:
    """First-pass backfill for one channel: collect missed messages and
    POST each to its destination, then advance last_seen.

    Mirrors the legacy listener's first-launch handling: when last_seen=0
    (never run before) we ARCHIVE without re-running the AI to avoid
    stale-history replay. Otherwise we replay through the API.
    """
    import sqlite3
    from src import db_settings
    from src.config import BACKFILL_MAX_AGE_MIN

    try:
        last_seen_str = db_settings.get_str(dest_db, "last_seen_tg_msg_id", "0")
        last_seen = int(last_seen_str) if last_seen_str else 0
    except (ValueError, TypeError):
        last_seen = 0

    try:
        missed = await collect_missed_fn(client, channel.chat_id, last_seen)
    except Exception:
        log.exception("collect_missed failed for ch=%s; skipping backfill", channel.id)
        return

    if not missed:
        log.info("no missed messages for ch=%s (last_seen=%s)", channel.id, last_seen)
        return

    if last_seen == 0:
        # First launch on this channel: archive without AI.
        log.info(
            "ch=%s first launch; archiving %d historical messages without AI",
            channel.id, len(missed),
        )
        # Write straight into destination DB's messages table. Idempotent
        # via UNIQUE(chat_id, tg_message_id).
        try:
            conn = sqlite3.connect(str(dest_db))
            try:
                for m in missed:
                    conn.execute(
                        "INSERT OR IGNORE INTO messages"
                        "(tg_message_id, chat_id, sender, text, is_backfill, "
                        " source_channel_id) "
                        "VALUES(?,?,?,?,1,?)",
                        (m.tg_message_id, channel.chat_id, m.sender, m.text,
                         channel.id),
                    )
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error:
            log.exception("archive failed for ch=%s", channel.id)
        db_settings.set_str(
            dest_db, "last_seen_tg_msg_id",
            str(max(m.tg_message_id for m in missed)),
        )
        return

    # Subsequent launch: replay missed through API, age-capped.
    from datetime import timedelta
    cap = timedelta(minutes=BACKFILL_MAX_AGE_MIN)
    now = datetime.now(timezone.utc)
    processed = 0
    skipped = 0
    for m in sorted(missed, key=lambda x: x.tg_message_id):
        age = now - m.date
        if age > cap:
            skipped += 1
            continue
        ok = await asyncio.to_thread(
            lambda mm=m: post_message_fn(
                target,
                tg_chat_id=channel.chat_id,
                tg_message_id=mm.tg_message_id,
                text=mm.text,
                sender=mm.sender,
                received_at=mm.date.isoformat(),
                is_backfill=True,
            )
        )
        if ok:
            processed += 1
        else:
            log.warning(
                "backfill DROPPED ch=%s tg_msg_id=%s",
                channel.id, m.tg_message_id,
            )
    db_settings.set_str(
        dest_db, "last_seen_tg_msg_id",
        str(max(m.tg_message_id for m in missed)),
    )
    log.info(
        "ch=%s backfill: processed=%d skipped=%d (age>%dm)",
        channel.id, processed, skipped, BACKFILL_MAX_AGE_MIN,
    )


async def _run_legacy_single_account() -> None:
    """Delegate to ``listener.main()`` for the actual Telethon work.

    Step 6 will replace this with a real multi-channel-per-session
    implementation that registers one event handler per Channel on the
    shared Telethon client. Until then, the v1 listener does the work —
    its single-channel subscription matches the current-scope constraint.
    """
    # Lazy import so this module imports cheaply (tests that just check
    # the v2 resolution logic don't need Telethon).
    from src.listener import main as legacy_main
    await legacy_main()


def _parse_account_id_arg(argv: list[str]) -> str | None:
    """Read ``--account-id <id>`` from argv. Returns None when absent.

    Step 13: each NSSM listener service is registered with a specific
    --account-id so its process binds to one Telegram user account.
    Single-account back-compat: when argv has no --account-id and the
    v2 config has exactly one account, that one is picked automatically
    (Step 5 behavior).
    """
    try:
        idx = argv.index("--account-id")
    except ValueError:
        return None
    if idx + 1 >= len(argv):
        return None
    return argv[idx + 1].strip() or None


async def main() -> None:
    """v2-aware listener entry point.

    Behavior:
      - v2 config absent / empty → run the legacy single-stack listener
        (preserves fresh-install / pre-migration UX).
      - v2 config present and ``--account-id`` matches an account →
        run that account's channels (Step 13).
      - v2 config present with exactly 1 account and no --account-id →
        run that account (Step 5 single-account back-compat).
      - v2 config present with N>1 accounts and no --account-id →
        SystemExit with a clear pointer to Step 13.
    """
    import sys
    cfg = _resolve_v2_or_none()
    if cfg is None:
        log.info("v2 config absent — running legacy single-stack listener")
        await _run_legacy_single_account()
        return

    account_id = _parse_account_id_arg(sys.argv)
    account, channels = _validate_current_scope(cfg, account_id=account_id)
    log.info(
        "shared_listener starting: account=%s phone=%s channels=%d",
        account.id, account.phone_display(), len(channels),
    )
    for ch in channels:
        log.info(
            "  channel: id=%s chat_id=%s profile=%s enabled=%s",
            ch.id, ch.chat_id, ch.profile_id, ch.enabled,
        )

    if len(channels) > 1:
        # Step 6: real multi-channel runner. One Telethon session, N
        # event handlers, each dispatching to the matching destination's
        # API via POST /incoming_message.
        log.info(
            "multi-channel runner: %d channels under account %s",
            len(channels), account.id,
        )
        await _run_multi_channel(account, channels, cfg)
        return

    # Step 5 path: single channel delegates to legacy listener.main()
    # for the hot-path lifecycle (heartbeat, second-pass catchup,
    # supervisor) that the multi-channel runner doesn't replicate yet.
    # When the multi-channel runner reaches feature parity, this branch
    # collapses into _run_multi_channel(account, channels, cfg).
    # Audit-fix: mirror sidecar creds → dest DB BEFORE delegating, so the
    # legacy listener.main() finds tg_session_blob/api_id/hash on its
    # first read. Without this, accounts created via the v2 Add Account
    # dialog would have the legacy listener poll forever (it only reads
    # from config.DB_PATH, not the sidecar files).
    _mirror_sidecar_to_dest_db_if_needed(account, channels, cfg)
    await _run_legacy_single_account()


def _mirror_sidecar_to_dest_db_if_needed(
    account: Account, channels: "tuple[Channel, ...]", cfg: ConfigV2,
) -> None:
    """One-shot mirror of sidecar Telethon creds into the dest DB.

    The legacy ``listener.main()`` reads ``tg_session_blob`` /
    ``tg_api_id`` / ``tg_api_hash`` from ``config.DB_PATH`` (the
    destination's DB). For accounts authed via the v2 Add Account
    dialog the dest DB is empty — creds live in
    ``%APPDATA%/CopyTrades/accounts/<acc>.creds.json`` +
    ``.session.txt``. Mirror them here so delegation works.

    No-op when the dest DB already has the keys (the fast path).
    """
    try:
        from src import db_settings
        # The legacy listener uses config.DB_PATH. Resolve the same way
        # _resolve_dispatch_for_channel would for this channel — first
        # destination it routes to.
        first_route = next(
            (r for r in cfg.routes_for_channel(channels[0].id) if r.enabled),
            None,
        )
        if first_route is None:
            return
        dest = cfg.destination(first_route.destination_id)
        if dest is None or not dest.db_path:
            return
        db_path = Path(dest.db_path)
        if not db_path.exists():
            return
        # Already populated? Skip.
        if db_settings.get_str(db_path, "tg_session_blob", ""):
            return

        from src.gui.services.account_credentials import (
            load_account_credentials,
        )
        creds = load_account_credentials(cfg, account)
        if creds is None:
            return
        db_settings.set_str(db_path, "tg_api_id", str(creds.api_id))
        db_settings.set_str(db_path, "tg_api_hash", creds.api_hash)
        db_settings.set_str(db_path, "tg_session_blob", creds.session_blob)
        log.info(
            "mirrored sidecar Telethon creds for account %s into %s",
            account.id, db_path,
        )
    except Exception:
        # Never block the delegate path on mirror failure — the listener
        # will fall back to its own polling loop and surface the issue
        # via its log.
        log.exception(
            "sidecar→DB mirror failed for account %s; legacy listener "
            "will poll for tg_session_blob",
            account.id,
        )


if __name__ == "__main__":
    asyncio.run(main())
