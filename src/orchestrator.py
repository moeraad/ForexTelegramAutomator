import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src.ai import AIClient
from src.ai_logger import log_call
from src.state_summary import render_open_positions
from src.validators import Action, AlertAction, validate_action


RECENT_CHAT_WINDOW = 20


def _insert_message(conn: sqlite3.Connection, tg_message_id: int, chat_id: int,
                    sender: str, text: str) -> int | None:
    """Returns row id, or None if duplicate."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages(tg_message_id, chat_id, sender, text) "
        "VALUES(?,?,?,?)",
        (tg_message_id, chat_id, sender, text),
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


def process_message(
    conn: sqlite3.Connection,
    ai: AIClient,
    tg_message_id: int,
    chat_id: int,
    sender: str,
    text: str,
    ai_log_path: Path | str,
    auto_execute_delay_sec: int,
) -> list[int]:
    """Insert message, call AI, validate + persist actions. Returns inserted action IDs."""
    msg_id = _insert_message(conn, tg_message_id, chat_id, sender, text)
    if msg_id is None:
        return []  # duplicate

    open_positions_block = render_open_positions(conn)
    recent_chat = _recent_chat_text(conn, chat_id, RECENT_CHAT_WINDOW)

    try:
        result = ai.call(recent_chat, open_positions_block, f"{sender}: {text}")
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

    inserted: list[int] = []
    for action in result.response.actions:
        v = validate_action(action, conn)
        payload = json.dumps(_payload_for(action))
        if not v.ok:
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                "status, ea_response) VALUES(?, ?, ?, 'rejected', ?)",
                (msg_id, _action_type(action), payload, v.error),
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
            "status, execute_after) VALUES(?, ?, ?, 'pending', ?)",
            (msg_id, _action_type(action), payload, execute_after),
        )
        inserted.append(cur.lastrowid)
    return inserted
