import json
import logging
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from src import prefilter, signal_memory, trigger_matcher, unmatched_store
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
from src.validators import (
    Action,
    AlertAction,
    AttachSignalAction,
    CancelPendingAction,
    OpenAction,
    OpenInstantAction,
    validate_action,
)

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
    """Mark a message as processed even when no action was produced.

    Originally this DELETEd the row "to keep the table lean", but that
    removed the only dedup tombstone we had — the UNIQUE(chat_id,
    tg_message_id) index. After Telethon reconnected and called
    `get_difference`, the same Telegram message would be redelivered
    and `_insert_message` would happily reinsert it (the unique row
    was gone). The orchestrator then re-ran the full pipeline:
    triage + Sonnet + persistence, burning $0.01–0.05 per re-process.
    Forensic logs showed msg_id 2303 re-processed 22 times overnight
    on a single channel.

    The fix is to KEEP the row so the UNIQUE index keeps rejecting
    redelivery. Lean-table is a non-goal compared to dedup correctness;
    a row per ignored message is < 1KB and a chatty channel produces
    ~200/day, so a year of orphans is ~70k rows — fine for SQLite.
    Operators who care can prune `messages WHERE id NOT IN
    (SELECT source_msg_id FROM actions WHERE source_msg_id IS NOT NULL)
    AND received_at < datetime('now', '-30 days')` on a schedule.

    The function name + signature are kept for back-compat; it's now a
    no-op when there are no actions referencing the message (the only
    case the old code wanted to act on).
    """
    return


def _load_channel_profile_for_prefilter() -> dict:
    """Load the active channel profile from disk for the Stage 0 pre-filter.

    Returns empty dict if no profile is configured / found, which makes the
    pre-filter a no-op (safe default). Cached lookup not needed — this is
    called once per incoming message and the file is small.
    """
    try:
        from src.ai import _resolve_profile_path
        path = _resolve_profile_path()
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, RuntimeError):
        return {}


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

    # Stage 0: universal pre-filter. Cheap, deterministic, no LLM call.
    # Driven entirely by profile.symbol_aliases / profile.other_instruments
    # — no hardcoded language tokens. With empty profile config this is a
    # no-op (safe default for first-run channels). Mirrors the same gate
    # the wizard runs on history, so live and offline behave consistently.
    _profile = _load_channel_profile_for_prefilter()
    drop_sym, _sym_reason = prefilter.should_drop_by_symbol(text, _profile)
    if drop_sym:
        log_call(ai_log_path, {
            "msg_id": msg_id, "stage": "prefilter",
            "decision": "drop", "reason": "symbol-mismatch",
        })
        _cleanup_message_if_orphan(conn, msg_id)
        return []
    if prefilter.looks_like_ad(text):
        log_call(ai_log_path, {
            "msg_id": msg_id, "stage": "prefilter",
            "decision": "drop", "reason": "ad-shape",
        })
        _cleanup_message_if_orphan(conn, msg_id)
        return []

    # Trigger matcher (Layers 1 + 2) runs BEFORE triage. Operator-
    # curated triggers are the highest-confidence signal in the
    # pipeline — they're literally the operator declaring "this exact
    # pattern means this action." Gating them behind triage's LLM
    # keep/ignore guess was dropping legitimate matches when the
    # message happened to look like one of triage's 8 universal
    # IGNORE categories (TP-hit announcements being the classic
    # offender: triage drops them as category 5 noise, but operator
    # may have a CLOSE_FULL trigger for exactly that pattern).
    #
    # The cost rationale for triage running first no longer applies:
    # triage's job is to skip Sonnet, but the matcher path doesn't
    # call Sonnet either, so triage couldn't save anything for
    # matcher-bound messages. Triage now runs only on the residual
    # set the matcher didn't catch — same Sonnet-cost-savings, no
    # false-negative drops on operator-confirmed patterns.
    #
    # Errors inside the matcher must never break the live path —
    # wrapped + logged + falls through.
    matched_actions: list[Action] | None = None
    try:
        matched_actions = trigger_matcher.match(text, conn)
    except Exception as e:  # noqa: BLE001 — must not break live path
        log.warning("trigger_matcher raised for msg_id=%s: %s", msg_id, e)
        log_call(ai_log_path, {
            "msg_id": msg_id, "stage": "trigger_matcher", "error": str(e),
        })
    if matched_actions:
        log_call(ai_log_path, {
            "msg_id": msg_id,
            "stage": "trigger_matcher",
            "matched_actions": [a.type for a in matched_actions],
        })
        trades.info(
            "trigger_match msg_id=%s types=[%s]",
            msg_id,
            ",".join(a.type for a in matched_actions),
        )
        return _persist_actions(
            conn, msg_id, matched_actions, ai_log_path,
            auto_execute_delay_sec, is_backfill=is_backfill,
        )

    # Triage runs only on messages the matcher didn't catch — i.e.
    # genuinely novel content the operator hasn't curated a trigger
    # for. Its job is to skip the expensive Sonnet call on clear
    # noise; it never sees matcher-bound messages, so it can't
    # false-negative a confirmed pattern. Built-in "WHEN IN DOUBT →
    # keep" bias keeps Sonnet as the safety net for anything triage
    # is unsure about.
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
        # Persist as ALERT so user is informed. Terminal 'executed' so
        # notification_dispatcher DMs it (REVIEW.md P1).
        ai_err_now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, executed_at) "
            "VALUES(?, 'ALERT', ?, 'executed', ?)",
            (msg_id, json.dumps({"level": "warning", "text": f"AI error: {e}"}),
             ai_err_now),
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

    # Reaching this branch means the trigger matcher returned no hits
    # for this message. If Sonnet emitted a deterministic-emittable
    # action type, the message is a trigger candidate — queue it for
    # the operator to review in the Triggers tab. Backfill replays
    # are excluded: they're historical and the operator already had a
    # chance to curate triggers for those.
    if not is_backfill:
        try:
            unmatched_store.record(
                conn,
                text=text,
                source_msg_id=msg_id,
                actions=list(result.response.actions),
            )
        except Exception as e:  # noqa: BLE001 — must not break live path
            log.warning("unmatched_store.record raised for msg_id=%s: %s", msg_id, e)

    inserted = _persist_actions(
        conn, msg_id, list(result.response.actions), ai_log_path,
        auto_execute_delay_sec, is_backfill=is_backfill,
    )
    # signal_memory clear runs only when an OPEN-shape action persisted.
    # The matcher path doesn't emit OPEN/ATTACH_SIGNAL so this stays
    # AI-path-specific. Re-derive open_persisted from the actions list
    # rather than threading another return value through _persist_actions.
    if SIGNAL_MEMORY_ENABLED:
        if any(isinstance(a, (OpenAction, OpenInstantAction, AttachSignalAction))
               for a in result.response.actions):
            signal_memory.clear_on_open(conn, chat_id)
    return inserted


def _persist_actions(
    conn: sqlite3.Connection,
    msg_id: int,
    actions: list[Action],
    ai_log_path: Path | str,
    auto_execute_delay_sec: int,
    *,
    is_backfill: bool,
) -> list[int]:
    """Shared persistence loop used by both the AI path and the trigger
    matcher path. Iterates `actions`, validates each, inserts the
    appropriate row (rejected / executed / sent / pending), and runs
    the evaluator kick for OPEN-shape actions.

    Splitting this out lets the trigger matcher short-circuit the LLM
    call without duplicating ~150 lines of insert + validation +
    branch logic — and guarantees both paths obey the same fingerprint
    dedup, validator gates, CANCEL_PENDING server-side handling, ALERT
    terminal-row policy, and backfill-management guard.
    """
    inserted: list[int] = []
    for i, action in enumerate(actions):
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
            action, conn, preceding_actions=actions[:i]
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

        # CANCEL_PENDING is server-side only. Flip matching watching OPENs
        # to 'rejected' immediately and mark the CANCEL action itself
        # 'executed' — the EA's ManagePendingOrders() picks up the
        # rejected state on its next OnTimer sweep and OrderDelete's the
        # broker pending. No EA dispatcher involvement needed.
        if isinstance(action, CancelPendingAction):
            now_iso = datetime.now(timezone.utc).isoformat()
            res = conn.execute(
                "UPDATE actions SET status='rejected', ea_response=?, executed_at=? "
                "WHERE action_type='OPEN' AND status='watching' "
                "  AND json_extract(payload_json,'$.symbol')=?",
                ("cancelled_by_channel", now_iso, action.symbol),
            )
            cancelled_n = res.rowcount if res.rowcount is not None else 0
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, status, "
                "ea_response, executed_at) "
                "VALUES(?, 'CANCEL_PENDING', ?, 'executed', ?, ?)",
                (msg_id, payload,
                 f"cancelled {cancelled_n} watching OPEN(s)", now_iso),
            )
            inserted.append(cur.lastrowid)
            trades.info(
                "action_inserted msg_id=%s action_id=%s type=CANCEL_PENDING "
                "status=executed cancelled=%s",
                msg_id, cur.lastrowid, cancelled_n,
            )
            continue

        # ALERTs are notification-only — they have nothing to execute
        # against MT5 (no broker call, no position mutation). Insert as
        # terminal 'executed' with executed_at set so the bot's
        # notification_dispatcher (which only DMs terminal rows) picks
        # them up immediately. Previously inserted as 'pending' with no
        # execute_after, which left them invisible forever — neither the
        # promoter nor the dispatcher would advance them (REVIEW.md P1).
        if isinstance(action, AlertAction):
            alert_now = datetime.now(timezone.utc).isoformat()
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                "status, executed_at) "
                "VALUES(?, 'ALERT', ?, 'executed', ?)",
                (msg_id, payload, alert_now),
            )
            inserted.append(cur.lastrowid)
            trades.info(
                "action_inserted msg_id=%s action_id=%s type=ALERT status=executed",
                msg_id, cur.lastrowid,
            )
            continue

        execute_after = (
            datetime.now(timezone.utc) + timedelta(seconds=auto_execute_delay_sec)
        ).isoformat()
        # Short-circuit straight to 'sent' when the operator has set the
        # grace delay to 0. Skips the 1s promoter sweep + lets the EA
        # pick the action up on its very next /actions?status=sent poll.
        # When a delay > 0 is set we still insert as 'pending' so the
        # /cancel-via-DM window works as designed.
        initial_status = "sent" if auto_execute_delay_sec <= 0 else "pending"
        # Backfill-management guard (REVIEW.md P1 / Q5): a non-OPEN
        # action arriving from backfill replay is almost always a stale
        # reminder of an instruction that has already been handled. An
        # old REINFORCE in particular would close+reopen the current
        # position without operator review. Park these rows
        # (status=pending, execute_after=NULL) so the promoter cannot
        # auto-fire them; the bot's notification dispatcher then DMs the
        # operator with an Execute/Ignore keyboard.
        ea_response: str | None = None
        if is_backfill and not isinstance(action, (OpenAction, OpenInstantAction)):
            initial_status = "pending"
            execute_after = None
            ea_response = "backfill_management_review_required"
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, execute_after, fingerprint, ea_response) "
            "VALUES(?, ?, ?, ?, ?, ?, ?)",
            (msg_id, _action_type(action), payload, initial_status,
             execute_after, fp, ea_response),
        )
        inserted.append(cur.lastrowid)
        trades.info(
            "action_inserted msg_id=%s action_id=%s type=%s status=pending",
            msg_id, cur.lastrowid, _action_type(action),
        )
        if isinstance(action, (OpenAction, OpenInstantAction)):
            # Async signal-quality evaluation. Doesn't block this function;
            # the action is already 'pending' and will be promoted on the
            # bot's next sweep regardless of the evaluator outcome. The
            # worker writes its result back into actions.payload_json under
            # the 'evaluation' key, which the EA dashboard reads via
            # GET /actions/latest_open_evaluation. See src/ai_evaluator.py.
            #
            # OPEN_INSTANT is included because the evaluator only reads
            # `signal["side"]` (it deliberately ignores entry/SL/TPs — see
            # ai_evaluator.py module docstring). Direction is the only
            # input it needs, and OPEN_INSTANT carries that. Firing here
            # gives the dashboard a score within seconds of the naked
            # open, instead of waiting for ATTACH_SIGNAL.
            kick_evaluator_for_open(cur.lastrowid, _payload_for(action))

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

def kick_evaluator_for_open(action_id: int, signal_dict: dict) -> None:
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
        from src import db_settings
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
        # Version switch: 'v1' = legacy 15-axis LLM-as-judge,
        # 'v2' = new layered (deterministic + LLM synthesizer). Default
        # v2; flip back via settings during rollout if needed. The v2
        # path tolerates a None ai_client and still publishes a
        # deterministic verdict, but the worker only spawns when the
        # client is available — so this branch always has one.
        from pathlib import Path
        evaluator_version = db_settings.get_str(
            Path(db_path), "evaluator_version", "v2",
        )
        if evaluator_version == "v2":
            from src.evaluator.evaluator import evaluate_signal_v2
            evaluation = evaluate_signal_v2(
                signal_dict, conn, ai_client, db_path=Path(db_path),
            )
        else:
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
    except Exception as e:
        log.exception("evaluator worker: failed for action_id=%s", action_id)
        # Write an ALERT so the operator sees the failure via the bot's
        # notification dispatcher. Without this the AI call was billed
        # (it shows up in ai_calls.jsonl) but no breadcrumb tied it to a
        # specific action (REVIEW.md P2).
        if conn is not None:
            try:
                conn.execute(
                    "INSERT INTO actions(source_msg_id, action_type, "
                    "payload_json, status, executed_at) "
                    "VALUES(NULL, 'ALERT', ?, 'executed', ?)",
                    (
                        json.dumps({
                            "level": "warning",
                            "text": (
                                f"Evaluator crashed for action #{action_id}: "
                                f"{type(e).__name__}: {e}"
                            ),
                        }),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            except Exception:
                log.exception(
                    "evaluator worker: failed to write ALERT row for action_id=%s",
                    action_id,
                )
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
