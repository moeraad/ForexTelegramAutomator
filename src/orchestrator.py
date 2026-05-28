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
from src.profile_context import ProfileContext
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


def _tag_inserted_actions(
    conn: sqlite3.Connection,
    action_ids: list[int],
    *,
    source_channel_id: str = "",
    route_id: str = "",
) -> None:
    """Stamp source_channel_id / route_id on a batch of just-inserted actions.

    Wrapper that lets process_message thread the routing context onto
    every action it creates without touching the seven-plus INSERT sites
    inside _persist_actions and the ALERT/CANCEL inline paths. Uses
    COALESCE so a previously-set value isn't overwritten when only one
    of the two ids is provided.

    No-op when no ids are supplied OR when both ids are blank.
    """
    if not action_ids:
        return
    if not source_channel_id and not route_id:
        return
    placeholders = ",".join("?" * len(action_ids))
    conn.execute(
        f"UPDATE actions SET "
        f"  source_channel_id = COALESCE(?, source_channel_id), "
        f"  route_id = COALESCE(?, route_id) "
        f"WHERE id IN ({placeholders})",
        (source_channel_id or None, route_id or None, *action_ids),
    )


def _insert_message(conn: sqlite3.Connection, tg_message_id: int, chat_id: int,
                    sender: str, text: str, *, is_backfill: bool = False,
                    source_channel_id: str = "",
                    reply_to_tg_message_id: int | None = None) -> int | None:
    """Returns row id, or None if duplicate."""
    cur = conn.execute(
        "INSERT OR IGNORE INTO messages"
        "(tg_message_id, chat_id, sender, text, is_backfill, "
        " source_channel_id, reply_to_tg_message_id) "
        "VALUES(?,?,?,?,?,?,?)",
        (tg_message_id, chat_id, sender, text, 1 if is_backfill else 0,
         source_channel_id or None, reply_to_tg_message_id),
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
    profile: ProfileContext | None = None,
    source_channel_id: str = "",
    route_id: str = "",
    halted: bool = False,
    failover_from_destination_id: str = "",
    reply_to_tg_message_id: int | None = None,
) -> list[int]:
    """Insert message, call AI, validate + persist actions. Returns inserted action IDs.

    ``profile`` (Step 3 of multi-channel plan): when provided, the AI/triage
    system prompts, target symbol, and prefilter rules all come from this
    ProfileContext. When omitted, falls back to the module-level globals
    that read config.CHANNEL_PROFILE — the v1 compatibility path used by
    the current single-stack listener until Step 5 replaces it.

    ``halted`` (Step 15 of multi-channel plan): when True, the message is
    still inserted into ``messages`` for audit, but the AI pipeline and
    action insertion are skipped. The caller resolves halt from the v2
    config (Channel.halted or Route.halted) so orchestrator stays
    decoupled from config_v2.
    """
    msg_id = _insert_message(conn, tg_message_id, chat_id, sender, text,
                             is_backfill=is_backfill,
                             source_channel_id=source_channel_id,
                             reply_to_tg_message_id=reply_to_tg_message_id)
    if msg_id is None:
        return []  # duplicate

    # Step 18: tag every AI call log with source_channel_id + route_id so
    # cost-per-channel / cost-per-route analytics work. The closure here
    # avoids touching every existing log_call site — they just inherit
    # the tags via the merge in _log_with_tags.
    def _log_with_tags(record: dict) -> None:
        record = dict(record)
        if source_channel_id:
            record.setdefault("source_channel_id", source_channel_id)
        if route_id:
            record.setdefault("route_id", route_id)
        # Step 21: tag every log row from a failover request so the
        # operator can grep the audit trail for failover events.
        if failover_from_destination_id:
            record.setdefault("failover_from", failover_from_destination_id)
        log_call(ai_log_path, record)

    def _record_pipeline_decision(
        stage: str,
        outcome: str,
        meta: dict | None = None,
    ) -> None:
        """Persist the terminal stage decision onto the messages row so
        the Pipeline view can render it without scanning ai_calls.jsonl.

        `stage` is one of the 7 buckets the radial chart counts:
        prefilter_drop, trigger_text, trigger_embedding, trigger_ignore,
        triage_ignored, interpreted_signal, interpreted_ignore. `outcome`
        is the short
        badge label (e.g. symbol-mismatch, ad-shape, keep, ignore, or
        comma-joined action types). `meta` is rendered in the detail
        panel — kept as a JSON blob so adding new fields doesn't require
        schema changes.

        Errors are swallowed: the audit row is nice-to-have, never worth
        breaking the live trade path. The JSONL log is still the source
        of truth for forensic deep-dives.
        """
        try:
            now_iso = datetime.now(timezone.utc).isoformat()
            conn.execute(
                "UPDATE messages SET decided_stage=?, decided_outcome=?, "
                "decided_at=?, pipeline_meta_json=? WHERE id=?",
                (
                    stage,
                    outcome,
                    now_iso,
                    json.dumps(meta) if meta else None,
                    msg_id,
                ),
            )
        except Exception:
            log.exception(
                "pipeline decision write failed msg_id=%s stage=%s",
                msg_id, stage,
            )

    # Step 20: per-route rule filter. Returns a (possibly shorter) actions
    # list with the disallowed ones dropped; each drop is logged so the
    # operator can grep them. Returns the input unchanged when route_id
    # is blank (legacy / single-stack) or rules can't be resolved.
    def _apply_route_rules(actions: "list[Action]") -> "list[Action]":
        if not route_id or not actions:
            return actions
        try:
            from src import config_v2
            from src.route_rules import evaluate_pre_persistence_rules
            cfg_path = config_v2.config_path()
            if not config_v2.is_v2(cfg_path):
                return actions
            cfg = config_v2.load_v2(cfg_path)
            if cfg is None:
                return actions
            route = cfg.route(route_id)
            if route is None:
                return actions
            kept: list[Action] = []
            for a in actions:
                at = _action_type(a)
                allowed, reason = evaluate_pre_persistence_rules(
                    route, action_type=at,
                )
                if allowed:
                    kept.append(a)
                else:
                    _log_with_tags({
                        "msg_id": msg_id, "stage": "route_rule",
                        "decision": "drop", "reason": reason,
                        "action_type": at,
                    })
                    log.info(
                        "route_rule drop msg_id=%s route=%s action=%s reason=%s",
                        msg_id, route_id, at, reason,
                    )
            return kept
        except Exception:  # noqa: BLE001 — never break live path on rule eval
            log.exception(
                "route rule filter failed for msg_id=%s route=%s; "
                "passing actions through", msg_id, route_id,
            )
            return actions

    def _route_payload_extras() -> dict:
        """Build the payload_extras dict for OPEN actions on this route.

        Step 20: surfaces EA-honored per-route caps (``route_max_lots``,
        ``route_min_account_balance``, ``route_skip_if_drawdown_pct``)
        to the execution layer via the OPEN payload. Empty dict on any
        config-resolution failure or when no rules are set — preserves
        single-stack behavior.
        """
        if not route_id:
            return {}
        try:
            from src import config_v2
            cfg_path = config_v2.config_path()
            if not config_v2.is_v2(cfg_path):
                return {}
            cfg = config_v2.load_v2(cfg_path)
            if cfg is None:
                return {}
            route = cfg.route(route_id)
            if route is None:
                return {}
            extras: dict = {}
            if route.max_lots > 0:
                extras["route_max_lots"] = float(route.max_lots)
            if route.min_account_balance > 0:
                extras["route_min_account_balance"] = float(route.min_account_balance)
            if route.skip_if_drawdown_pct > 0:
                extras["route_skip_if_drawdown_pct"] = float(route.skip_if_drawdown_pct)
            # Step 21: stamp the OPEN payload when this action came in
            # via the failover path so the operator can see "this trade
            # landed on the fallback destination" in DMs / journal.
            if failover_from_destination_id:
                extras["failover_from"] = failover_from_destination_id
            return extras
        except Exception:  # noqa: BLE001
            log.exception(
                "route payload extras lookup failed for route=%s", route_id,
            )
            return {}

    if halted:
        # Step 15: per-channel / per-route halt — message recorded for
        # audit, no actions emitted. Visible in ai_calls.jsonl so the
        # operator can confirm halt is doing what they expect.
        _log_with_tags({
            "msg_id": msg_id, "stage": "halt",
            "decision": "drop", "reason": "channel-or-route-halted",
        })
        log.info(
            "halt drop msg_id=%s channel=%s route=%s",
            msg_id, source_channel_id, route_id,
        )
        return []

    # Stage 0: universal pre-filter. Cheap, deterministic, no LLM call.
    # Driven entirely by profile.symbol_aliases / profile.other_instruments
    # — no hardcoded language tokens. With empty profile config this is a
    # no-op (safe default for first-run channels). Mirrors the same gate
    # the wizard runs on history, so live and offline behave consistently.
    # When ProfileContext is supplied, use its already-loaded data and skip
    # the per-message disk read (the v1 fallback path).
    _profile = profile.data if profile is not None else _load_channel_profile_for_prefilter()
    drop_sym, _sym_reason = prefilter.should_drop_by_symbol(text, _profile)
    if drop_sym:
        _log_with_tags({
            "msg_id": msg_id, "stage": "prefilter",
            "decision": "drop", "reason": "symbol-mismatch",
        })
        _record_pipeline_decision("prefilter_drop", "symbol-mismatch")
        _cleanup_message_if_orphan(conn, msg_id)
        return []
    if prefilter.looks_like_ad(text):
        _log_with_tags({
            "msg_id": msg_id, "stage": "prefilter",
            "decision": "drop", "reason": "ad-shape",
        })
        _record_pipeline_decision("prefilter_drop", "ad-shape")
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
    # Reply-context: when this message is a Telegram reply, look up the
    # parent's text from the messages archive so the matcher can apply
    # operator-defined "activate / ignore" reply-intent rules. None when
    # the parent isn't in the archive (older than backfill).
    parent_text: str | None = None
    if reply_to_tg_message_id:
        try:
            parent_row = conn.execute(
                "SELECT text FROM messages "
                "WHERE chat_id=? AND tg_message_id=? LIMIT 1",
                (chat_id, reply_to_tg_message_id),
            ).fetchone()
            if parent_row is not None:
                parent_text = parent_row["text"]
        except Exception:
            log.exception(
                "reply-parent lookup failed for msg_id=%s reply_to=%s",
                msg_id, reply_to_tg_message_id,
            )

    matched_actions: list[Action] | None = None
    matcher_meta: dict = {}
    try:
        matched_actions = trigger_matcher.match(
            text, conn, parent_text=parent_text, meta_out=matcher_meta,
        )
    except Exception as e:  # noqa: BLE001 — must not break live path
        log.warning("trigger_matcher raised for msg_id=%s: %s", msg_id, e)
        _log_with_tags({
            "msg_id": msg_id, "stage": "trigger_matcher", "error": str(e),
        })
    # An empty list (matched but explicitly suppressed via reply-intent
    # 'ignore') is distinct from None ('no match, try AI'). Suppressed
    # messages skip the AI silently — operator's call.
    if matched_actions == []:
        # Two distinct suppressions both surface as an empty list; the
        # matcher disambiguates via meta_out["layer"].
        if matcher_meta.get("layer") == "deterministic_ignore":
            # Whole-message equality with an operator-curated IGNORE
            # sample — drop before triage + interpreter. Its own bucket so
            # the operator can see (and un-curate) what was suppressed.
            _log_with_tags({
                "msg_id": msg_id,
                "stage": "trigger_matcher",
                "layer": "deterministic_ignore",
                "result": "suppressed_noise",
            })
            _record_pipeline_decision(
                "trigger_ignore", "curated-noise",
                meta={
                    "layer": "deterministic_ignore",
                    "rule_phrase": matcher_meta.get("rule_phrase"),
                },
            )
            trades.info(
                "trigger_match msg_id=%s suppressed as curated noise",
                msg_id,
            )
            _cleanup_message_if_orphan(conn, msg_id)
            return []
        _log_with_tags({
            "msg_id": msg_id,
            "stage": "trigger_matcher",
            "reply_intent": "ignore",
            "result": "suppressed",
        })
        # Reply-intent ignore is a deterministic-text match too — bucket
        # it under trigger_text so the operator sees these in the same
        # slice as the curated patterns they came from.
        _record_pipeline_decision(
            "trigger_text", "reply-intent-ignore",
            meta={"layer": matcher_meta.get("layer", "reply_intent_ignore")},
        )
        trades.info(
            "trigger_match msg_id=%s suppressed by reply_intent=ignore",
            msg_id,
        )
        return []
    if matched_actions:
        _log_with_tags({
            "msg_id": msg_id,
            "stage": "trigger_matcher",
            "matched_actions": [a.type for a in matched_actions],
        })
        action_types_csv = ",".join(a.type for a in matched_actions)
        layer = matcher_meta.get("layer", "deterministic")
        bucket = "trigger_embedding" if layer == "embedding" else "trigger_text"
        _record_pipeline_decision(
            bucket, action_types_csv,
            meta={k: v for k, v in matcher_meta.items() if v is not None},
        )
        trades.info(
            "trigger_match msg_id=%s types=[%s]",
            msg_id,
            action_types_csv,
        )
        # Step 20: per-route rule filter (action-type / time-of-day).
        # Drops are logged via _apply_route_rules → ai_calls.jsonl.
        filtered = _apply_route_rules(matched_actions)
        ids = _persist_actions(
            conn, msg_id, filtered, ai_log_path,
            auto_execute_delay_sec, is_backfill=is_backfill,
            payload_extras=_route_payload_extras(),
        )
        _tag_inserted_actions(conn, ids, source_channel_id=source_channel_id,
                              route_id=route_id)
        return ids

    # Triage runs only on messages the matcher didn't catch — i.e.
    # genuinely novel content the operator hasn't curated a trigger
    # for. Its job is to skip the expensive Sonnet call on clear
    # noise; it never sees matcher-bound messages, so it can't
    # false-negative a confirmed pattern. Built-in "WHEN IN DOUBT →
    # keep" bias keeps Sonnet as the safety net for anything triage
    # is unsure about.
    if triage is not None:
        try:
            tri = triage.classify(
                text,
                _open_positions_count(conn),
                system_prompt=(profile.triage_prompt if profile is not None else None),
            )
            _log_with_tags({
                "msg_id": msg_id,
                "stage": "triage",
                "decision": tri.decision,
                "raw_response": tri.raw_text,
                "latency_ms": tri.latency_ms,
                **tri.usage,
            })
            if tri.decision == "ignore":
                _record_pipeline_decision(
                    "triage_ignored", "ignore",
                    meta={
                        "raw_response": tri.raw_text,
                        "latency_ms": tri.latency_ms,
                        **tri.usage,
                    },
                )
                _cleanup_message_if_orphan(conn, msg_id)
                return []
        except Exception as e:
            # Never drop a message on triage failure — fall through to Sonnet.
            log.warning("triage failed for msg_id=%s: %s", msg_id, e)
            _log_with_tags({
                "msg_id": msg_id, "stage": "triage", "error": str(e),
            })

    open_positions_block = render_open_positions(
        conn,
        symbol=(profile.symbol if profile is not None else "XAUUSD"),
        current_tg_message_id=tg_message_id,
        current_chat_id=chat_id,
    )
    if SIGNAL_MEMORY_ENABLED:
        entries = signal_memory.load_active(
            conn, chat_id, SIGNAL_MEMORY_MAX_ENTRIES, SIGNAL_MEMORY_MAX_AGE_HOURS
        )
        context_block = signal_memory.render(entries)
    else:
        context_block = _recent_chat_text(conn, chat_id, RECENT_CHAT_WINDOW)

    try:
        result = ai.call(
            context_block,
            open_positions_block,
            f"{sender}: {text}",
            system_prompt=(profile.system_prompt if profile is not None else None),
        )
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
        _log_with_tags({"error": str(e), "msg_id": msg_id})
        ids = [cur.lastrowid]
        _tag_inserted_actions(conn, ids, source_channel_id=source_channel_id,
                              route_id=route_id)
        return ids

    _log_with_tags({
        "msg_id": msg_id,
        "stage": "interpret",
        "raw_response": result.raw_text,
        "latency_ms": result.latency_ms,
        **result.usage,
    })

    types_summary = ",".join(_action_type(a) for a in result.response.actions) or "-"
    interp_meta = {
        "category": result.response.category or "",
        "latency_ms": result.latency_ms,
        "raw_response": result.raw_text,
        **result.usage,
    }
    if result.response.actions:
        _record_pipeline_decision(
            "interpreted_signal", types_summary, meta=interp_meta,
        )
    else:
        _record_pipeline_decision(
            "interpreted_ignore",
            result.response.category or "ignore",
            meta=interp_meta,
        )
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

    # Step 20: per-route rule filter (action-type / time-of-day).
    # Applied AFTER the AI call so the operator can still see what the
    # AI proposed (via raw_response logging) even when the rule rejects.
    ai_actions = _apply_route_rules(list(result.response.actions))
    inserted = _persist_actions(
        conn, msg_id, ai_actions, ai_log_path,
        auto_execute_delay_sec, is_backfill=is_backfill,
        payload_extras=_route_payload_extras(),
    )
    _tag_inserted_actions(conn, inserted, source_channel_id=source_channel_id,
                          route_id=route_id)
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
    payload_extras: dict | None = None,
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

    Step 20: ``payload_extras`` (when given) is merged into the OPEN-action
    payload only. Used to propagate per-route caps (``route_max_lots``)
    from the route config to the EA at execution time without changing
    the AI's action shape.
    """
    inserted: list[int] = []
    for i, action in enumerate(actions):
        if payload_extras and isinstance(action, OpenAction):
            base = _payload_for(action)
            base.update(payload_extras)
            payload = json.dumps(base)
        else:
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
    # Settings-driven on/off. Default "1" — keep the evaluator running
    # unless the operator explicitly disables it via the Settings UI.
    try:
        from src import db_settings
        if db_settings.get_str(Path(db_path), "ai_evaluator_enabled", "1") != "1":
            return
    except Exception:
        log.exception("evaluator: failed to read ai_evaluator_enabled; defaulting to ON")
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
