import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src import signal_memory
from src.ai import AIClient
from src.ai_logger import log_call
from src.config import (
    FINGERPRINT_BAND_PRICE,
    FINGERPRINT_WINDOW_HOURS,
    SIGNAL_MEMORY_ENABLED,
    SIGNAL_MEMORY_MAX_AGE_HOURS,
    SIGNAL_MEMORY_MAX_ENTRIES,
)
from src.fingerprint import signal_fingerprint
from src.state_summary import render_open_positions
from src.validators import Action, AlertAction, OpenAction, validate_action


RECENT_CHAT_WINDOW = 20


def _insert_message(conn: sqlite3.Connection, tg_message_id: int, chat_id: int,
                    sender: str, text: str, *, is_backfill: bool = False) -> int | None:
    """Returns row id, or None if duplicate."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(tg_message_id, chat_id, sender, text, is_backfill) "
        "VALUES(?,?,?,?,?)",
        (tg_message_id, chat_id, sender, text, 1 if is_backfill else 0),
    )
    if cur.rowcount == 0:
        return None
    return cur.lastrowid


def _recent_chat_text(conn: sqlite3.Connection, chat_id: int, limit: int) -> str:
    rows = conn.execute(
        "SELECT sender, text, received_at FROM messages "
        "WHERE chat_id=? ORDER BY id DESC LIMIT ?",
        (chat_id, limit),
    ).fetchall()
    rows = list(reversed(rows))
    return "\n".join(f"[{r['received_at']}] {r['sender']}: {r['text']}" for r in rows)


def _payload_for(action: Action) -> dict:
    return action.model_dump(exclude={"type"})


def _action_type(action: Action) -> str:
    return action.type


def _has_recent_duplicate_open(
    conn: sqlite3.Connection, fingerprint: str, window_hours: int
) -> bool:
    """True if an OPEN action with the same fingerprint is still "live" —
    either sitting in the pipeline (pending/sent/claimed) or already executed
    with the linked position still open — and was created within the window.

    Why: channels often re-post or quote earlier signals. The AI can't always
    tell these from genuinely new entries, so we gate at the orchestrator.
    """
    row = conn.execute(
        "SELECT 1 FROM actions a "
        "WHERE a.fingerprint = ? "
        "  AND a.created_at > datetime('now', ?) "
        "  AND ("
        "    a.status IN ('pending','sent','claimed') "
        "    OR (a.status = 'executed' AND EXISTS ("
        "      SELECT 1 FROM positions p "
        "      WHERE p.action_id = a.id AND p.status = 'open'"
        "    ))"
        "  ) "
        "LIMIT 1",
        (fingerprint, f"-{window_hours} hours"),
    ).fetchone()
    return row is not None


def process_message(
    conn: sqlite3.Connection,
    ai: AIClient,
    tg_message_id: int,
    chat_id: int,
    sender: str,
    text: str,
    ai_log_path: Path | str,
    auto_execute_delay_sec: int,
    *,
    is_backfill: bool = False,
) -> list[int]:
    """Insert message, call AI, validate + persist actions. Returns inserted action IDs."""
    msg_id = _insert_message(conn, tg_message_id, chat_id, sender, text,
                             is_backfill=is_backfill)
    if msg_id is None:
        return []  # duplicate

    open_positions_block = render_open_positions(conn)
    if SIGNAL_MEMORY_ENABLED:
        entries = signal_memory.load_active(
            conn, SIGNAL_MEMORY_MAX_ENTRIES, SIGNAL_MEMORY_MAX_AGE_HOURS
        )
        context_block = signal_memory.render(entries)
    else:
        context_block = _recent_chat_text(conn, chat_id, RECENT_CHAT_WINDOW)

    try:
        result = ai.call(context_block, open_positions_block, f"{sender}: {text}")
    except Exception as e:
        # Persist as ALERT so user is informed
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(?, 'ALERT', ?, 'pending')",
            (msg_id, json.dumps({"level": "warning", "text": f"AI error: {e}"})),
        )
        log_call(ai_log_path, {"error": str(e), "msg_id": msg_id})
        return [cur.lastrowid]

    log_call(ai_log_path, {
        "msg_id": msg_id,
        "raw_response": result.raw_text,
        "latency_ms": result.latency_ms,
        **result.usage,
    })

    if SIGNAL_MEMORY_ENABLED and result.response.category and result.response.category != "ignore":
        signal_memory.record(
            conn, msg_id, result.response.category,
            signal_memory.summarize(result.response),
        )

    inserted: list[int] = []
    open_persisted = False
    for action in result.response.actions:
        payload = json.dumps(_payload_for(action))
        fp: str | None = None
        if isinstance(action, OpenAction):
            fp = signal_fingerprint(action, band=FINGERPRINT_BAND_PRICE)
            if _has_recent_duplicate_open(conn, fp, FINGERPRINT_WINDOW_HOURS):
                cur = conn.execute(
                    "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                    "status, ea_response, fingerprint) "
                    "VALUES(?, ?, ?, 'rejected', 'duplicate_signal', ?)",
                    (msg_id, _action_type(action), payload, fp),
                )
                inserted.append(cur.lastrowid)
                continue

        v = validate_action(action, conn)
        if not v.ok:
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                "status, ea_response, fingerprint) VALUES(?, ?, ?, 'rejected', ?, ?)",
                (msg_id, _action_type(action), payload, v.error, fp),
            )
            inserted.append(cur.lastrowid)
            continue

        # ALERTs do not get auto-executed
        if isinstance(action, AlertAction):
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
                "VALUES(?, 'ALERT', ?, 'pending')",
                (msg_id, payload),
            )
            inserted.append(cur.lastrowid)
            continue

        execute_after = (
            datetime.now(timezone.utc) + timedelta(seconds=auto_execute_delay_sec)
        ).isoformat()
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, execute_after, fingerprint) VALUES(?, ?, ?, 'pending', ?, ?)",
            (msg_id, _action_type(action), payload, execute_after, fp),
        )
        inserted.append(cur.lastrowid)
        if isinstance(action, OpenAction):
            open_persisted = True

    if SIGNAL_MEMORY_ENABLED and open_persisted:
        signal_memory.clear_on_open(conn)
    return inserted
