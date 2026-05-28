"""Per-bot notification dispatcher (Step 7 of multi-channel plan).

Replaces the legacy ``bot.py::notification_dispatcher`` polling-loop's
"who gets DMed" decision with an explicit fan-out at the moment an event
happens:

  1. Some API endpoint (e.g. ``post_result``) calls ``dispatch_notification``
  2. The dispatcher queries v2 bindings matching the event's scope
  3. One ``bot_outbox`` row is written per matching bot
  4. Each ``Bot`` process tails its own ``bot_outbox`` rows via
     ``bot_outbox_tailer.OutboxTailer`` and DMs the operator

All four scopes are wired here:

  - ``scope=global``: bot DMs about every event on every destination
  - ``scope=destination``: bot DMs about events on one destination
  - ``scope=channel``: bot DMs about events tagged with a specific
    ``source_channel_id`` (works across destinations the channel mirrors to)
  - ``scope=route``: bot DMs about events tagged with a specific
    ``route_id`` (one channel-to-destination edge)

Step 7 wired only ``scope=destination``; Step 14 extended this to the
full set. Per-event filtering on channel/route happens INSIDE the
dispatcher (see the scope switch below); the candidate-set widening
for which bindings COULD match this destination lives in
``ConfigV2.bindings_for_destination``.

Design notes:
  - The outbox row stores ``action_id`` + ``event_type`` + small payload.
    The tailer re-fetches the action at render time so it always reflects
    the latest state (a partial close after the row was queued would still
    show the freshest data).
  - The dispatcher is a NO-OP when v2 config is absent / has no bindings
    for this destination — single-stack pre-migration setups keep using
    the legacy ``bot.py::notification_dispatcher`` polling path.
  - The dispatcher returns the number of outbox rows written; callers
    use this to decide whether to also kick the legacy path (mutex
    enforced at ``bot.py`` startup, not per-event).
"""
from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from src import config_v2
from src.config_v2 import ConfigV2, Destination


log = logging.getLogger("notification_dispatcher")


EventType = str  # "action_terminal" | "action_parked" | "alert" | future


def dispatch_notification(
    conn: sqlite3.Connection,
    *,
    event_type: EventType,
    action_id: int | None = None,
    db_path: str | Path | None = None,
    source_channel_id: str | None = None,
    route_id: str | None = None,
    extra_payload: dict | None = None,
) -> int:
    """Write outbox rows for every bot whose binding matches the event.

    Returns the number of rows inserted. ``0`` means either v2 isn't
    configured yet OR no bindings cover this destination — caller can
    fall back to the legacy notification path.

    Resolution flow:
      1. Read v2 config from ``stacks_config.json``
      2. Find the Destination whose ``db_path`` matches ``db_path`` arg
      3. Get ``bindings_for_destination(dest.id)`` — returns the union
         of global / destination / channel-that-routes-here /
         route-that-targets-here bindings (Step 14)
      4. For each binding, apply per-event filter:
         - channel scope: skip if event's ``source_channel_id`` doesn't match
         - route scope: skip if event's ``route_id`` doesn't match
         - global / destination: always proceed
      5. Insert one ``bot_outbox`` row per surviving binding

    ``db_path`` defaults to the conn's database path; pass explicitly
    when the conn is in-memory (tests).
    """
    if db_path is None:
        try:
            db_path = _conn_db_path(conn)
        except Exception:  # noqa: BLE001
            log.debug("dispatch_notification: cannot resolve db_path from conn")
            return 0

    cfg = _resolve_v2_or_none()
    if cfg is None:
        return 0

    dest = _find_destination(cfg, Path(db_path))
    if dest is None:
        return 0

    bindings = cfg.bindings_for_destination(dest.id)
    if not bindings:
        return 0

    payload = dict(extra_payload or {})
    payload_json = json.dumps(payload, ensure_ascii=False)
    now = datetime.now(timezone.utc).isoformat()

    inserted = 0
    for binding in bindings:
        # Step 14: scope-specific event filtering. bindings_for_destination
        # already widened the candidate set to every binding that COULD
        # match events on this destination; here we narrow to bindings
        # that match THIS event's source_channel_id / route_id.
        # Empty channel/route tags on the event are treated as "no
        # specific channel/route" — channel/route-scoped bindings can't
        # match (they require a positive match), but global+destination
        # still do.
        if binding.scope == "channel":
            if not source_channel_id or source_channel_id != binding.channel_id:
                continue
        elif binding.scope == "route":
            if not route_id or route_id != binding.route_id:
                continue
        try:
            conn.execute(
                "INSERT INTO bot_outbox("
                "  bot_id, event_type, event_payload, "
                "  source_channel_id, route_id, action_id, created_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    binding.bot_id,
                    event_type,
                    payload_json,
                    source_channel_id,
                    route_id,
                    action_id,
                    now,
                ),
            )
            inserted += 1
        except sqlite3.Error:
            log.exception(
                "dispatch_notification: failed to insert outbox row for "
                "bot=%s event=%s action=%s",
                binding.bot_id, event_type, action_id,
            )
    return inserted


def has_v2_binding_for_destination(db_path: str | Path) -> bool:
    """True iff v2 config has at least one binding covering this destination.

    Used at ``bot.py`` startup to pick between the new tailer path and
    the legacy ``notification_dispatcher`` polling loop. Never raises —
    returns False on any read error so the legacy path remains the safe
    default.
    """
    try:
        cfg = _resolve_v2_or_none()
        if cfg is None:
            return False
        dest = _find_destination(cfg, Path(db_path))
        if dest is None:
            return False
        return bool(cfg.bindings_for_destination(dest.id))
    except Exception:  # noqa: BLE001
        return False


def resolve_bot_id_for_destination(db_path: str | Path) -> str | None:
    """Return the bot_id whose ``scope=destination`` binding matches this DB.

    For Step 7's tailer setup: bot.py needs to know which bot_id to poll
    outbox for. Single-stack post-migration has exactly one binding per
    destination, so this is unambiguous. With multi-bot bindings (Step 14)
    one bot.py process serves one bot_id — Step 8/14 will spawn N bot
    processes if the operator wants per-channel/per-route bots.
    """
    try:
        cfg = _resolve_v2_or_none()
        if cfg is None:
            return None
        dest = _find_destination(cfg, Path(db_path))
        if dest is None:
            return None
        # Prefer destination-scoped binding (the most specific match for
        # this DB); fall back to global-scoped binding.
        for b in cfg.bindings_for_destination(dest.id):
            if b.scope == "destination":
                return b.bot_id
        for b in cfg.bindings_for_destination(dest.id):
            if b.scope == "global":
                return b.bot_id
    except Exception:  # noqa: BLE001
        log.exception("resolve_bot_id_for_destination failed")
    return None


# ---- internals -------------------------------------------------------------


def _resolve_v2_or_none() -> ConfigV2 | None:
    cfg_path = config_v2.config_path()
    if not config_v2.is_v2(cfg_path):
        return None
    try:
        return config_v2.load_v2(cfg_path)
    except Exception:  # noqa: BLE001
        return None


def _find_destination(cfg: ConfigV2, db_path: Path) -> Destination | None:
    target = db_path.resolve()
    for d in cfg.destinations:
        if d.db_path and Path(d.db_path).resolve() == target:
            return d
    return None


def _conn_db_path(conn: sqlite3.Connection) -> str:
    """Read the file path backing a sqlite3.Connection via PRAGMA."""
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row:
        raise RuntimeError("PRAGMA database_list returned nothing")
    # Row shape: (seq, name, file). Handle both Row and tuple.
    try:
        return row["file"]
    except (KeyError, IndexError, TypeError):
        return row[2]
