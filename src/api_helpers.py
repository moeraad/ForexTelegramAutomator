"""Module-level helpers for ``src/api.py``.

Pulled out during the Day-1 cleanup pass so api.py keeps the endpoint
definitions + middleware in one place, and ancillary functions live
with the rest of their reasoning.

Three concerns here:

  - ``_backfill_destination_channel_on_startup`` — Step-4 one-shot
    that tags pre-v2 NULL ``source_channel_id`` rows after the API
    process establishes which Destination/Channel it serves.
  - ``_run_orchestrator_for_incoming`` — BackgroundTask body for
    ``POST /incoming_message`` (Step 4 wire chain).
  - ``_enforce_auth_bind_policy`` — security gate that refuses to start
    when the API would be unauthenticated on a non-loopback interface
    (REVIEW.md P2).
"""
from __future__ import annotations

import logging
import sqlite3

from src.api_models import IncomingMessageBody
from src.profile_context import ProfileContext


log = logging.getLogger("api")


# Hosts that count as loopback for the EA_SHARED_TOKEN bind policy.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"})

DEFAULT_API_HOST = "127.0.0.1"
DEFAULT_API_PORT = 8765


def _backfill_destination_channel_on_startup(
    conn: sqlite3.Connection, db_path: str,
) -> None:
    """One-shot backfill of NULL source_channel_id rows on API startup.

    Looks up the v2 config; finds the Destination whose db_path matches
    this process's DB; finds the (single) Route pointing at it; calls
    ``backfill_source_channel_id`` and ``backfill_route_id`` with that
    Route's channel + route ids.

    Skips silently when:
      - v2 config doesn't exist (fresh install pre-migration)
      - No Destination matches this db_path (orphaned DB)
      - The Destination has 0 or >1 enabled Routes (ambiguous; deferred
        Step 12 / Step 11 cases — operator handles)
    """
    from pathlib import Path as _Path
    from src import config_v2
    from src.db import backfill_route_id, backfill_source_channel_id

    cfg_path = config_v2.config_path()
    if not config_v2.is_v2(cfg_path):
        return
    cfg = config_v2.load_v2(cfg_path)
    if cfg is None:
        return
    # Normalize comparison: paths may be relative or have varying separators.
    target = _Path(db_path).resolve()
    matching = [d for d in cfg.destinations
                if d.db_path and _Path(d.db_path).resolve() == target]
    if len(matching) != 1:
        return
    dest = matching[0]
    routes = [r for r in cfg.routes_for_destination(dest.id) if r.enabled]
    if len(routes) != 1:
        return
    route = routes[0]
    msgs, acts = backfill_source_channel_id(conn, route.channel_id)
    routes_updated = backfill_route_id(conn, route.id)
    if msgs or acts or routes_updated:
        log.info(
            "backfill on startup: destination=%s channel=%s route=%s "
            "messages+=%s actions+source_channel+=%s actions+route+=%s",
            dest.id, route.channel_id, route.id, msgs, acts, routes_updated,
        )


def _resolve_profile_for_channel(
    channel_id: str, fallback: ProfileContext | None,
) -> ProfileContext | None:
    """Look up the ProfileContext for a specific v2 Channel.

    Step 12 (aggregate routing): one destination's API can receive
    messages from N different channels, each with its OWN profile. The
    startup-loaded ``app.state.profile_ctx`` (which is the destination's
    "default" profile) is the wrong choice when channel B has a
    different profile than the destination was configured with.

    Resolution flow:
      1. Read v2 config from ``stacks_config.json`` (cached cheaply via
         is_v2 + load_v2; the v2 reader does the heavy lifting once).
      2. Find Channel by ``channel_id``.
      3. Look up its Profile by ``channel.profile_id``.
      4. Call ``get_profile_context(profile.path)`` — leverages the
         Day-2 mtime cache so live profile edits propagate without
         restart.

    Returns ``fallback`` when:
      - v2 config absent (fresh install pre-migration)
      - channel_id is blank (pre-Step-12 listener compat)
      - Channel / Profile not found in v2 config
      - get_profile_context fails (missing file, corrupt JSON)

    The fallback chain matches the Step-4 contract: when this lookup
    can't resolve, use the destination's startup-loaded profile.
    """
    if not channel_id:
        return fallback
    try:
        from src import config_v2
        from src.profile_context import get_profile_context
        cfg_path = config_v2.config_path()
        if not config_v2.is_v2(cfg_path):
            return fallback
        cfg = config_v2.load_v2(cfg_path)
        if cfg is None:
            return fallback
        channel = cfg.channel(channel_id)
        if channel is None:
            return fallback
        profile = cfg.profile(channel.profile_id)
        if profile is None or not profile.path:
            return fallback
        from pathlib import Path as _Path
        return get_profile_context(_Path(profile.path), name=profile.id)
    except Exception:
        log.exception(
            "per-channel profile lookup failed for channel_id=%s; using fallback",
            channel_id,
        )
        return fallback


def _resolve_halt_for_message(channel_id: str, route_id: str) -> bool:
    """Return True iff the v2 config marks this channel OR route as halted.

    Step 15: per-channel / per-route halt. The store is the v2 config file
    itself (``Channel.halted`` and ``Route.halted`` were carved out as
    forward-compat fields by earlier steps). mtime-cached reads via
    ``is_v2`` / ``load_v2`` keep the hot-path cost negligible: a `stat`
    call and (on change) one JSON parse — both bounded.

    Resolution is fail-open: any lookup failure (v2 config absent,
    blank ids, channel/route not found) returns False so a config glitch
    can't silently halt the entire pipeline. Halt is operator-driven and
    visible in the GUI; silent halt would be far worse than a missed
    halt that the operator can re-trigger.
    """
    if not channel_id and not route_id:
        return False
    try:
        from src import config_v2
        cfg_path = config_v2.config_path()
        if not config_v2.is_v2(cfg_path):
            return False
        cfg = config_v2.load_v2(cfg_path)
        if cfg is None:
            return False
        if channel_id:
            ch = cfg.channel(channel_id)
            if ch is not None and ch.halted:
                return True
        if route_id:
            rt = cfg.route(route_id)
            if rt is not None and rt.halted:
                return True
        return False
    except Exception:
        log.exception(
            "halt resolution failed for channel=%s route=%s; assuming NOT halted",
            channel_id, route_id,
        )
        return False


def _run_orchestrator_for_incoming(
    *,
    conn: sqlite3.Connection,
    ai,
    triage,
    profile: ProfileContext | None,
    body: IncomingMessageBody,
    ai_log_path,
    auto_execute_delay_sec: int,
) -> None:
    """BackgroundTask body for POST /incoming_message.

    Runs in the API process's background-task pool AFTER the 202 response
    has been sent. Exceptions are logged and swallowed (the HTTP client
    has already seen success — there's no one to report a failure to here).

    Step 12 (aggregate routing): resolve the profile PER CHANNEL via
    v2 config lookup, not the destination's startup-loaded default. The
    fallback to ``profile`` (startup-loaded) preserves the legacy
    single-stack path when v2 config can't resolve the lookup.

    Day-2 cleanup: refresh the resolved profile from disk via mtime-cache
    so the operator's GUI Profile Editor save takes effect on the very
    next message without a service restart. ``get_profile_context``
    already handles this internally — no-op (returns the same object)
    when the file is unchanged.
    """
    from src.orchestrator import process_message
    # Step 15: per-channel / per-route halt. Resolved here (not inside
    # process_message) so the halt source-of-truth (v2 config) is read at
    # the API boundary — orchestrator stays decoupled from config_v2.
    halted = _resolve_halt_for_message(body.channel_id, body.route_id)
    # Step 12: per-channel profile resolution.
    profile = _resolve_profile_for_channel(body.channel_id, profile)
    # Day-2: refresh in case the file changed since the resolver's cache hit.
    if profile is not None:
        from src.profile_context import refresh_if_stale
        try:
            profile = refresh_if_stale(profile)
        except Exception:
            # File missing / unreadable / corrupt JSON — keep the
            # in-memory copy and log so the operator can debug.
            log.exception(
                "profile refresh failed for %s; using cached version",
                profile.path,
            )
    try:
        process_message(
            conn,
            ai,
            body.tg_message_id,
            body.tg_chat_id,
            body.sender,
            body.text,
            ai_log_path,
            auto_execute_delay_sec,
            is_backfill=body.is_backfill,
            triage=triage,
            profile=profile,
            # Step 11: tag the inserted message + every action with the
            # originating channel + route so the audit trail (Step 19)
            # can reconstruct which leg of a mirror produced which trade.
            source_channel_id=body.channel_id,
            route_id=body.route_id,
            halted=halted,
            failover_from_destination_id=body.failover_from_destination_id,
            reply_to_tg_message_id=body.reply_to_tg_message_id,
        )
    except Exception:
        log.exception(
            "incoming_message orchestrator failed: tg_msg_id=%s channel=%s",
            body.tg_message_id, body.channel_id,
        )


def _enforce_auth_bind_policy(host: str, token: str) -> None:
    """Refuse to start when the API would be unauthenticated AND bound to
    a non-loopback interface. Any process on the network could otherwise
    POST /actions/{id}/result and forge broker fills (REVIEW.md P2).

    An empty/unset host is treated as the safe loopback default (see
    DEFAULT_API_HOST). Anything explicitly non-loopback requires a
    non-empty EA_SHARED_TOKEN.
    """
    if (token or "").strip():
        return
    h = (host or "").strip().lower()
    if not h or h in _LOOPBACK_HOSTS:
        return
    raise SystemExit(
        f"API refuses to start: EA_SHARED_TOKEN is empty and API_HOST="
        f"{host!r} is not loopback. Set EA_SHARED_TOKEN in .env, or bind "
        f"to 127.0.0.1 if this is a single-host install."
    )
