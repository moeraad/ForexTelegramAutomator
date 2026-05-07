import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src import signal_memory
from src.ai import AIClient
from src.ai_logger import log_call
from src.ai_triage import TriageClient
from src.config import (
    DB_PATH,
    FINGERPRINT_BAND_PRICE,
    FINGERPRINT_WINDOW_HOURS,
    SIGNAL_MEMORY_ENABLED,
    SIGNAL_MEMORY_MAX_AGE_HOURS,
    SIGNAL_MEMORY_MAX_ENTRIES,
)
from src.fingerprint import signal_fingerprint
from src.logging_setup import trades_log
from src.state_summary import render_open_positions
from src.validators import Action, AlertAction, OpenAction, validate_action

log = logging.getLogger(__name__)
trades = trades_log()


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


def _open_positions_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) FROM positions WHERE status='open'"
    ).fetchone()
    return int(row[0]) if row else 0


def _cleanup_message_if_orphan(conn: sqlite3.Connection, msg_id: int) -> None:
    """Delete the messages row when no action ended up referencing it.

    Per project policy: the messages table only retains rows that produced
    at least one action. Triage-ignored, pure-context, or otherwise no-op
    messages are dropped so the table stays lean.

    signal_memory entries that pointed at the deleted message have their
    message_id NULLed so the distilled summary survives the cleanup
    (the summary is what the prompt cares about, not the raw text).
    """
    row = conn.execute(
        "SELECT 1 FROM actions WHERE source_msg_id = ? LIMIT 1",
        (msg_id,),
    ).fetchone()
    if row is not None:
        return  # at least one action references the message → keep it
    conn.execute(
        "UPDATE signal_memory SET message_id = NULL WHERE message_id = ?",
        (msg_id,),
    )
    conn.execute("DELETE FROM messages WHERE id = ?", (msg_id,))


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
    triage: TriageClient | None = None,
) -> list[int]:
    """Insert message, call AI, validate + persist actions. Returns inserted action IDs."""
    msg_id = _insert_message(conn, tg_message_id, chat_id, sender, text,
                             is_backfill=is_backfill)
    if msg_id is None:
        return []  # duplicate

    if triage is not None:
        try:
            tri = triage.classify(text, _open_positions_count(conn))
            log_call(ai_log_path, {
                "msg_id": msg_id,
                "stage": "triage",
                "decision": tri.decision,
                "raw_response": tri.raw_text,
                "latency_ms": tri.latency_ms,
                **tri.usage,
            })
            if tri.decision == "ignore":
                _cleanup_message_if_orphan(conn, msg_id)
                return []
        except Exception as e:
            # Never drop a message on triage failure — fall through to Sonnet.
            log.warning("triage failed for msg_id=%s: %s", msg_id, e)
            log_call(ai_log_path, {
                "msg_id": msg_id, "stage": "triage", "error": str(e),
            })

    open_positions_block = render_open_positions(conn)
    if SIGNAL_MEMORY_ENABLED:
        entries = signal_memory.load_active(
            conn, chat_id, SIGNAL_MEMORY_MAX_ENTRIES, SIGNAL_MEMORY_MAX_AGE_HOURS
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

    types_summary = ",".join(_action_type(a) for a in result.response.actions) or "-"
    trades.info(
        "ai_decision msg_id=%s category=%s latency_ms=%s types=[%s]",
        msg_id, result.response.category or "-", result.latency_ms, types_summary,
    )

    if SIGNAL_MEMORY_ENABLED and result.response.category and result.response.category != "ignore":
        signal_memory.record(
            conn, msg_id, chat_id, result.response.category,
            signal_memory.summarize(result.response),
        )

    inserted: list[int] = []
    open_persisted = False
    for i, action in enumerate(result.response.actions):
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
                trades.info(
                    "action_inserted msg_id=%s action_id=%s type=%s status=rejected reason=duplicate_signal",
                    msg_id, cur.lastrowid, _action_type(action),
                )
                continue

        # Pass preceding actions so the OPEN-with-position-open guard in
        # validate_action can recognise compound emits like
        # [CLOSE_FULL, OPEN] (close+reopen rule for new structured signals
        # when the existing position should be flushed).
        v = validate_action(
            action, conn, preceding_actions=result.response.actions[:i]
        )
        if not v.ok:
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                "status, ea_response, fingerprint) VALUES(?, ?, ?, 'rejected', ?, ?)",
                (msg_id, _action_type(action), payload, v.error, fp),
            )
            inserted.append(cur.lastrowid)
            trades.info(
                "action_inserted msg_id=%s action_id=%s type=%s status=rejected reason=%s",
                msg_id, cur.lastrowid, _action_type(action), v.error,
            )
            continue

        # ALERTs do not get auto-executed
        if isinstance(action, AlertAction):
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
                "VALUES(?, 'ALERT', ?, 'pending')",
                (msg_id, payload),
            )
            inserted.append(cur.lastrowid)
            trades.info(
                "action_inserted msg_id=%s action_id=%s type=ALERT status=pending",
                msg_id, cur.lastrowid,
            )
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
        trades.info(
            "action_inserted msg_id=%s action_id=%s type=%s status=pending",
            msg_id, cur.lastrowid, _action_type(action),
        )
        if isinstance(action, OpenAction):
            open_persisted = True
            # Async signal-quality evaluation. Doesn't block this function;
            # the action is already 'pending' and will be promoted on the
            # bot's next sweep regardless of the evaluator outcome. The
            # worker writes its result back into actions.payload_json under
            # the 'evaluation' key, which the EA dashboard reads via
            # GET /actions/latest_open_evaluation. See src/ai_evaluator.py.
            _kick_evaluator_for_open(cur.lastrowid, _payload_for(action))

    if SIGNAL_MEMORY_ENABLED and open_persisted:
        signal_memory.clear_on_open(conn, chat_id)
    if not inserted:
        # No action ended up referencing this message (pure context, all
        # actions filtered, etc.) — drop the raw row per project policy.
        _cleanup_message_if_orphan(conn, msg_id)
    return inserted


# ---- Async signal-quality evaluator hook --------------------------------
#
# Fire-and-forget thread that runs after every OPEN action insert. The
# evaluator (src/ai_evaluator.py) calls the LLM with the signal payload
# plus current market context (price, OHLC snapshot, channel history) and
# writes a 0-100 conviction score into actions.payload_json under the
# 'evaluation' key. The EA dashboard reads the latest one via
# GET /actions/latest_open_evaluation.
#
# Tests (and any caller passing an explicit `auto_execute_delay_sec`)
# don't kick the evaluator — the existing in-memory test DB is fine but
# spawning a thread that opens a separate connection to a tmp_path
# ephemeral DB during pytest creates flakiness. The
# COPYTRADES_DISABLE_EVALUATOR env var (or the absence of a real
# DB_PATH file) disables the kick.

def _kick_evaluator_for_open(action_id: int, signal_dict: dict) -> None:
    """Spawn a daemon thread that runs evaluate_signal and writes the
    result back into actions.payload_json. Returns immediately. Failures
    in the worker are caught and logged; they never bubble up to the
    orchestrator's caller (the listener) and never affect the trade flow.
    """
    import os
    if os.environ.get("COPYTRADES_DISABLE_EVALUATOR") == "1":
        return
    db_path = DB_PATH
    if not db_path or not Path(db_path).exists():
        # Fresh-install path or test path with no DB — skip silently.
        return
    t = threading.Thread(
        target=_evaluator_worker,
        args=(action_id, signal_dict, db_path),
        name=f"evaluator-action-{action_id}",
        daemon=True,
    )
    t.start()


def _evaluator_worker(action_id: int, signal_dict: dict, db_path: str) -> None:
    """Body of the evaluator thread. Opens its own SQLite connection,
    runs the LLM evaluation, and merges the result into the action's
    payload_json. All exceptions are caught and logged; this thread MUST
    NOT raise into the daemon-thread default handler.
    """
    try:
        from src.ai_evaluator import evaluate_signal
        from src.db import connect
    except Exception:
        log.exception("evaluator worker: import failed; skipping action_id=%s", action_id)
        return
    conn = None
    try:
        conn = connect(db_path)
        ai_client = _build_evaluator_ai_client()
        if ai_client is None:
            log.warning("evaluator worker: no AI client available; skipping action_id=%s", action_id)
            return
        evaluation = evaluate_signal(signal_dict, conn, ai_client)
        # Merge evaluation into the action's existing payload_json. Re-read
        # in case another writer touched it (rare but defensive).
        row = conn.execute(
            "SELECT payload_json FROM actions WHERE id=?", (action_id,)
        ).fetchone()
        if row is None:
            return
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        payload["evaluation"] = evaluation
        conn.execute(
            "UPDATE actions SET payload_json=? WHERE id=?",
            (json.dumps(payload), action_id),
        )
        log.info(
            "evaluator: action_id=%s score=%s verdict=%s data_quality=%s",
            action_id, evaluation.get("score"), evaluation.get("verdict"),
            evaluation.get("data_quality"),
        )
    except Exception:
        log.exception("evaluator worker: failed for action_id=%s", action_id)
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _build_evaluator_ai_client():
    """Construct a minimal `chat(system, user) -> str` adapter on top of
    the project's existing LLMProvider abstraction. Lives here (not in
    ai_evaluator.py) so the evaluator module stays independent of the
    interpreter's provider-construction machinery and can be tested with
    a plain MagicMock.
    """
    try:
        from src.llm_provider import build_interpreter_provider, reasoning_level
        from src import config as _cfg
    except Exception:
        log.exception("evaluator: provider import failed")
        return None

    class _ChatAdapter:
        def __init__(self) -> None:
            self._provider = build_interpreter_provider(model="")
            self._level = reasoning_level(
                _cfg.AI_THINKING_ENABLED, _cfg.AI_THINKING_BUDGET_TOKENS
            )

        def chat(self, system_prompt: str, user_text: str) -> str:
            result = self._provider.interpret(
                system_prompt=system_prompt,
                cached_prefix="",
                volatile_suffix=user_text,
                max_output_tokens=2048,
                reasoning_level=self._level,
            )
            return result.raw_text

    try:
        return _ChatAdapter()
    except Exception:
        log.exception("evaluator: ChatAdapter init failed")
        return None
