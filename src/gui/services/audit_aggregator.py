"""Cross-destination audit aggregator (Step 19 of multi-channel plan).

Searches every Destination's DB for a Telegram message (by tg_message_id
or text substring) and returns the full trace per destination:
  message → its actions → each action's bot_outbox rows (DMs).

This is the first feature that intentionally breaks the
"each Stack's DB is fully isolated" principle. It's acceptable here
because audit is operator-driven (not hot-path), runs read-only against
each DB, and is the natural way to answer "did channel A's mirror
actually fire on dest B?" once Step 11 lets one channel hit N destinations.

Design notes:
  - Connection-per-query, not a pool. Audit queries are operator-rare
    (probably <100/day across all operators). A persistent pool would
    add lifecycle bugs (DB rotated under us, file lock races) without
    saving meaningful latency.
  - Read-only SQLite open via the URI form (``file:...?mode=ro``) so an
    audit pass from the GUI can't accidentally corrupt a destination DB.
  - All return types are plain dataclasses — easy to feed straight into
    a Qt tree widget without dragging Qt into the service layer.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class DmTrace:
    """One bot_outbox row tied to an action."""
    id: int
    bot_id: str
    event_type: str
    created_at: str
    delivered_at: str | None
    source_channel_id: str | None
    route_id: str | None


@dataclass(frozen=True)
class ActionTrace:
    """One actions row + its DMs."""
    id: int
    action_type: str
    status: str
    payload: dict
    created_at: str
    executed_at: str | None
    ea_response: str | None
    source_channel_id: str | None
    route_id: str | None
    dms: tuple[DmTrace, ...] = ()


@dataclass(frozen=True)
class MessageTrace:
    """One messages row + every action it triggered."""
    id: int
    tg_message_id: int
    chat_id: int
    sender: str | None
    text: str
    received_at: str
    is_backfill: bool
    source_channel_id: str | None
    actions: tuple[ActionTrace, ...] = ()


@dataclass(frozen=True)
class DestinationTrace:
    """All messages matching the audit query inside ONE destination DB."""
    destination_id: str
    destination_name: str
    db_path: str
    messages: tuple[MessageTrace, ...] = field(default_factory=tuple)
    error: str | None = None  # set when the DB couldn't be opened


def _open_readonly(db_path: Path) -> sqlite3.Connection:
    """Open a SQLite DB in read-only mode via the URI form.

    Refusing to mutate is the audit invariant: a GUI operator-search
    must not be able to flip a row in a production destination DB.
    """
    uri = f"file:{db_path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _matches_clause(
    *, tg_message_id: int | None, text_query: str,
) -> tuple[str, list]:
    """Build the WHERE clause + params for the message search.

    Empty/None for both → return the most recent N (let caller LIMIT).
    Both → AND the two conditions.
    """
    clauses: list[str] = []
    params: list = []
    if tg_message_id is not None:
        clauses.append("tg_message_id = ?")
        params.append(tg_message_id)
    if text_query.strip():
        clauses.append("text LIKE ?")
        params.append(f"%{text_query.strip()}%")
    if not clauses:
        return "1=1", []
    return " AND ".join(clauses), params


def _fetch_dms_for_action(
    conn: sqlite3.Connection, action_id: int,
) -> tuple[DmTrace, ...]:
    """Read bot_outbox rows for one action_id."""
    try:
        rows = conn.execute(
            "SELECT id, bot_id, event_type, created_at, delivered_at, "
            "       source_channel_id, route_id "
            "FROM bot_outbox WHERE action_id = ? ORDER BY id",
            (action_id,),
        ).fetchall()
    except sqlite3.OperationalError:
        # Pre-v2 DB without bot_outbox — that's fine, just no DMs to show.
        return ()
    return tuple(
        DmTrace(
            id=r["id"], bot_id=r["bot_id"], event_type=r["event_type"],
            created_at=r["created_at"], delivered_at=r["delivered_at"],
            source_channel_id=r["source_channel_id"], route_id=r["route_id"],
        )
        for r in rows
    )


def _fetch_actions_for_message(
    conn: sqlite3.Connection, message_id: int,
) -> tuple[ActionTrace, ...]:
    """Read actions rows for one message + their DMs."""
    rows = conn.execute(
        "SELECT id, action_type, payload_json, status, created_at, "
        "       executed_at, ea_response, source_channel_id, route_id "
        "FROM actions WHERE source_msg_id = ? ORDER BY id",
        (message_id,),
    ).fetchall()
    out: list[ActionTrace] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {"_raw": r["payload_json"]}
        dms = _fetch_dms_for_action(conn, r["id"])
        out.append(ActionTrace(
            id=r["id"], action_type=r["action_type"], status=r["status"],
            payload=payload, created_at=r["created_at"],
            executed_at=r["executed_at"], ea_response=r["ea_response"],
            source_channel_id=r["source_channel_id"],
            route_id=r["route_id"],
            dms=dms,
        ))
    return tuple(out)


def _fetch_messages_for_destination(
    conn: sqlite3.Connection,
    *,
    tg_message_id: int | None,
    text_query: str,
    limit: int,
) -> tuple[MessageTrace, ...]:
    """Read matching messages + cascading actions/DMs."""
    where, params = _matches_clause(
        tg_message_id=tg_message_id, text_query=text_query,
    )
    sql = (
        "SELECT id, tg_message_id, chat_id, sender, text, received_at, "
        "       is_backfill, source_channel_id "
        f"FROM messages WHERE {where} "
        "ORDER BY id DESC LIMIT ?"
    )
    rows = conn.execute(sql, [*params, limit]).fetchall()
    out: list[MessageTrace] = []
    for r in rows:
        actions = _fetch_actions_for_message(conn, r["id"])
        out.append(MessageTrace(
            id=r["id"], tg_message_id=r["tg_message_id"],
            chat_id=r["chat_id"], sender=r["sender"],
            text=r["text"], received_at=r["received_at"],
            is_backfill=bool(r["is_backfill"]),
            source_channel_id=r["source_channel_id"],
            actions=actions,
        ))
    return tuple(out)


def search_trace(
    *,
    tg_message_id: int | None = None,
    text_query: str = "",
    destinations: Iterable[tuple[str, str, Path]],
    limit_per_destination: int = 25,
) -> tuple[DestinationTrace, ...]:
    """Search every destination's DB and return per-destination traces.

    ``destinations`` is an iterable of ``(destination_id, name, db_path)``
    tuples. The caller (typically the audit view) derives this from the
    v2 config. Decoupling from ``config_v2`` here keeps the aggregator
    testable without writing a stacks_config.json.

    Returns a tuple aligned with the ``destinations`` order. Each
    ``DestinationTrace`` either has ``messages`` populated OR ``error``
    set (mutually exclusive). A blank query returns the most recent
    ``limit_per_destination`` messages per destination — useful as a
    landing-page "what just happened?" view.
    """
    results: list[DestinationTrace] = []
    for dest_id, dest_name, db_path in destinations:
        if not db_path.exists():
            results.append(DestinationTrace(
                destination_id=dest_id, destination_name=dest_name,
                db_path=str(db_path),
                error=f"DB file not found: {db_path}",
            ))
            continue
        try:
            conn = _open_readonly(db_path)
        except sqlite3.Error as e:
            results.append(DestinationTrace(
                destination_id=dest_id, destination_name=dest_name,
                db_path=str(db_path),
                error=f"sqlite open failed: {e}",
            ))
            continue
        try:
            msgs = _fetch_messages_for_destination(
                conn,
                tg_message_id=tg_message_id,
                text_query=text_query,
                limit=limit_per_destination,
            )
            results.append(DestinationTrace(
                destination_id=dest_id, destination_name=dest_name,
                db_path=str(db_path), messages=msgs,
            ))
        except sqlite3.Error as e:
            results.append(DestinationTrace(
                destination_id=dest_id, destination_name=dest_name,
                db_path=str(db_path),
                error=f"query failed: {e}",
            ))
        finally:
            conn.close()
    return tuple(results)


def destinations_from_v2_config() -> tuple[tuple[str, str, Path], ...]:
    """Helper: derive the (id, name, db_path) tuples from the v2 config.

    Returns an empty tuple when the v2 config is absent (fresh install
    pre-migration). The audit view treats empty as "nothing to search."
    """
    try:
        from src import config_v2
        cfg_path = config_v2.config_path()
        if not config_v2.is_v2(cfg_path):
            return ()
        cfg = config_v2.load_v2(cfg_path)
        if cfg is None:
            return ()
        return tuple(
            (d.id, d.name, Path(d.db_path))
            for d in cfg.destinations
            if d.db_path
        )
    except Exception:
        return ()
