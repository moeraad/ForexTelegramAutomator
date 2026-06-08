import json
import logging
import sqlite3
import time
from datetime import datetime, timezone

from fastapi import BackgroundTasks, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from src import config
# Pydantic request/response models live in src/api_models.py (Day-1 split).
from src.api_models import (
    AlertBody,
    AttachSignalBody,
    CandleBar,
    CloseBody,
    IncomingMessageBody,
    LegResult,
    MarketCandlesBody,
    MarketPriceBody,
    MarketSnapshotBody,
    MarketSnapshotStateBody,
    PositionRecoverBody,
    PositionUpdateBody,
    ResizePendingBody,
    ResultBody,
    TimeframeOhlcBody,
)
from src.logging_setup import configure_logging, trades_log
from src.position_signal import (
    find_last_replayable_closed,
    parse_payload,
    payload_has_signal_fields,
    resolve_signal_payload,
)
from src.profile_context import ProfileContext

log = configure_logging("api")
trades = trades_log()


# Signal-resolution helpers live in `src.position_signal` so both this
# module and `src.state_summary` see the same view of which closed
# positions are actually replayable. Aliases kept for any in-tree
# callers that previously imported the private names.
_parse_payload = parse_payload
_payload_has_signal_fields = payload_has_signal_fields
_resolve_signal_payload = resolve_signal_payload


# XAUUSD contract size in ounces: a 1.00 price move on 1.0 lot = $100.
# Used only for the GUI resize warning — the EA does the broker-precise
# clamp/feasibility check at placement time.
_XAUUSD_CONTRACT_SIZE = 100.0


def _pending_risk(
    lots: float, entry: float, sl: float, balance: float | None
) -> tuple[float, float | None]:
    """Estimate the dollars-at-risk if the SL hits at `lots`, and that as a
    percent of `balance`. `pct` is None when balance is unavailable.
    """
    sl_distance = abs(entry - sl)
    dollars = sl_distance * _XAUUSD_CONTRACT_SIZE * lots
    pct = (dollars / balance * 100.0) if balance and balance > 0 else None
    return dollars, pct


# Action types whose execution opens a new position and therefore makes
# sense to score after the fact. Phase-2 management types (CLOSE_PARTIAL,
# MOVE_SL_BE, etc.) modify the existing position — there's nothing new
# to evaluate. ALERTs never reach the EA at all.
_EVALUATABLE_OPEN_TYPES: frozenset[str] = frozenset({
    "OPEN", "OPEN_INSTANT", "REOPEN_LAST", "REINFORCE",
})


def _maybe_kick_post_execution_evaluator(
    action_row: sqlite3.Row,
    body: "ResultBody",
    legs: "list[LegResult]",
) -> None:
    """Fire the evaluator after a successful executed-result POST when
    the originating action is an OPEN-shape type AND no prior evaluation
    exists in the payload (the orchestrator-side kick already ran).

    Reads side from the first leg's snapshot — the evaluator only needs
    direction (see ai_evaluator.py module docstring), so a single leg is
    enough even for compound multi-position fills. Any failure to parse
    the payload or load the kick function is swallowed; this is purely
    diagnostic and must never break a result POST.
    """
    if body.status != "executed" or not legs:
        return
    if action_row["action_type"] not in _EVALUATABLE_OPEN_TYPES:
        return
    payload = parse_payload(action_row["payload_json"]) or {}
    if "evaluation" in payload:
        # Orchestrator-side kick already scored this action. Keep its
        # result; the chased-fill score would otherwise overwrite the
        # signal-time score that the dashboard has been showing.
        return
    side = legs[0].snapshot.get("side")
    if side not in ("BUY", "SELL"):
        # Without a usable side the evaluator can't run; bail rather
        # than feed it junk and burn an LLM call on a known-bad input.
        return
    signal_dict = {
        "side": side,
        "symbol": legs[0].snapshot.get("symbol"),
        "entry_price": legs[0].snapshot.get("entry_price"),
        "sl": legs[0].snapshot.get("sl"),
        "tp": legs[0].snapshot.get("tp"),
    }
    try:
        from src.orchestrator import kick_evaluator_for_open
    except ImportError:
        log.warning("post_exec evaluator: orchestrator import failed")
        return
    kick_evaluator_for_open(action_row["id"], signal_dict)


# Module-level helpers live in src/api_helpers.py (Day-1 split).
from src.api_helpers import (
    DEFAULT_API_HOST,
    DEFAULT_API_PORT,
    _backfill_destination_channel_on_startup,
    _enforce_auth_bind_policy,
    _run_orchestrator_for_incoming,
)



def build_app(
    conn: sqlite3.Connection,
    *,
    ai_client: object | None = None,
    triage_client: object | None = None,
    profile: ProfileContext | None = None,
    ai_log_path: object | None = None,
) -> FastAPI:
    """Build the FastAPI app for this destination's API process.

    The optional runtime context (``ai_client``, ``triage_client``,
    ``profile``, ``ai_log_path``) is required only by ``POST /incoming_message``
    — the EA-facing endpoints work without them. Tests that exercise the
    EA contract can call ``build_app(conn)`` unchanged; the new endpoint
    will reject requests with a clear 503 if it wasn't wired.
    """
    app = FastAPI()
    # Stash the runtime context on the app so the endpoint closure can
    # read it without smuggling MagicMock-shaped state through globals.
    app.state.ai_client = ai_client
    app.state.triage_client = triage_client
    app.state.profile_ctx = profile
    app.state.ai_log_path = ai_log_path

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        # Shared-secret header. When EA_SHARED_TOKEN is blank we run
        # unauthenticated (dev mode + tests that don't set it). When set,
        # every request must carry a matching X-EA-Token header — without
        # it the EA can't reach any endpoint, which is the whole point.
        # Read config.EA_SHARED_TOKEN per-request rather than capturing at
        # middleware-build time so test fixtures can flip it via
        # monkeypatch without rebuilding the app.
        #
        # POST /incoming_message uses X-Listener-Token instead (separate
        # secret so the listener and EA can be rotated independently).
        # Falls back to EA_SHARED_TOKEN when listener_shared_token is unset
        # — most single-stack operators won't bother with two tokens.
        path = request.url.path
        if path == "/incoming_message":
            expected = config.LISTENER_SHARED_TOKEN or config.EA_SHARED_TOKEN
            if expected and request.headers.get("X-Listener-Token") != expected:
                client_host = request.client.host if request.client else "?"
                log.warning("listener auth rejected from %s on %s",
                            client_host, path)
                return JSONResponse(
                    status_code=401,
                    content={"error": "missing or invalid X-Listener-Token"})
        else:
            expected = config.EA_SHARED_TOKEN
            if expected:
                if request.headers.get("X-EA-Token") != expected:
                    client_host = request.client.host if request.client else "?"
                    log.warning("auth rejected from %s on %s %s",
                                client_host, request.method, path)
                    return JSONResponse(
                        status_code=401,
                        content={"error": "missing or invalid X-EA-Token"})
        return await call_next(request)

    @app.middleware("http")
    async def http_error_log(request: Request, call_next):
        # Log only non-2xx responses to system.log. The EA polls /actions and
        # POSTs /market/price every second — logging every 200 was 90% of the
        # log volume and zero of the signal. 4xx/5xx still get forensic
        # history (status, latency, path) so a 3 a.m. 422 is debuggable.
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            if status >= 400:
                ms = int((time.perf_counter() - start) * 1000)
                # Expected 404s on poll endpoints: log at DEBUG only.
                # The EA hits these every tick; they're "absent, not error".
                path = request.url.path
                expected_empty_404 = status == 404 and path in (
                    "/actions/latest_open_evaluation",
                )
                logger_fn = log.debug if expected_empty_404 else log.warning
                logger_fn(
                    "%s %s -> %s %dms",
                    request.method, path, status, ms,
                )

    @app.exception_handler(RequestValidationError)
    async def on_validation_error(request: Request, exc: RequestValidationError):
        try:
            raw = (await request.body()).decode("utf-8", errors="replace")
        except Exception as e:
            raw = f"<unreadable: {e}>"
        safe_errors = []
        for err in exc.errors():
            e = dict(err)
            if isinstance(e.get("input"), (bytes, bytearray)):
                e["input"] = bytes(e["input"]).decode("utf-8", errors="replace")
            safe_errors.append(e)
        log.error(
            "422 on %s %s | errors=%s | raw_body=%r",
            request.method, request.url.path, safe_errors, raw,
        )
        return JSONResponse(
            status_code=422,
            content={"detail": safe_errors, "raw_body": raw},
        )

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/incoming_message", status_code=202)
    def incoming_message(body: IncomingMessageBody, bg: BackgroundTasks):
        """Listener-to-API ingestion endpoint (Step 4 of multi-channel plan).

        The shared listener POSTs each Telegram message here instead of
        writing the DB directly. The API:

          1. Validates the token (handled by auth_gate middleware)
          2. Verifies the runtime context is wired (returns 503 otherwise)
          3. Queues orchestrator.process_message as a background task
          4. Returns 202 immediately so the listener isn't blocked on AI calls

        ``channel_id`` is recorded in the inserted message + downstream
        actions via ``source_channel_id``. In single-stack mode the API
        uses its process-level ProfileContext regardless of channel_id
        (every message routes to the same destination). Multi-channel
        destinations (Step 12 deferred) will look up the ProfileContext
        per channel_id.
        """
        ai = app.state.ai_client
        triage = app.state.triage_client
        profile = app.state.profile_ctx
        log_path = app.state.ai_log_path
        if ai is None or log_path is None:
            log.error("incoming_message rejected: API was built without ai_client/ai_log_path")
            raise HTTPException(
                status_code=503,
                detail="API runtime context not configured (ai_client/ai_log_path required)",
            )

        # Resolve the auto-execute delay from settings just like the
        # listener used to do inline. Per-request read so live changes via
        # /settings take effect immediately.
        from src.db import get_setting
        delay_str = get_setting(conn, "auto_execute_delay_sec")
        try:
            delay = int(delay_str) if delay_str else config.DEFAULT_AUTO_EXECUTE_DELAY_SEC
        except (TypeError, ValueError):
            delay = config.DEFAULT_AUTO_EXECUTE_DELAY_SEC

        bg.add_task(
            _run_orchestrator_for_incoming,
            conn=conn,
            ai=ai,
            triage=triage,
            profile=profile,
            body=body,
            ai_log_path=log_path,
            auto_execute_delay_sec=delay,
        )
        return {"queued": True, "tg_message_id": body.tg_message_id}

    @app.get("/actions")
    def get_actions(status: str = "sent", limit: int = 50):
        rows = conn.execute(
            "SELECT id, action_type, payload_json, status, created_at "
            "FROM actions WHERE status=? ORDER BY id ASC LIMIT ?",
            (status, limit),
        ).fetchall()
        return {
            "actions": [
                {
                    "id": r["id"],
                    "action_type": r["action_type"],
                    "payload": json.loads(r["payload_json"]),
                    "status": r["status"],
                    "created_at": r["created_at"],
                }
                for r in rows
            ]
        }

    @app.get("/settings/{key}")
    def get_setting(key: str):
        row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
        if row is None:
            raise HTTPException(404)
        return {"key": key, "value": row["value"]}

    # ---- Pipeline view -------------------------------------------------
    # Powers src/gui/views/pipeline_view.py: per-message stage decision
    # readout + a per-bucket histogram. Both queries are time-windowed by
    # received_at and indexed via idx_messages_decided_stage. NULL stages
    # are surfaced as the "unknown" bucket (pre-migration history + any
    # in-flight rows the orchestrator hasn't decided on yet).
    _PIPELINE_BUCKETS = (
        "prefilter_drop", "trigger_text", "trigger_embedding",
        "trigger_ignore", "triage_ignored", "interpreted_signal",
        "interpreted_ignore",
    )

    def _pipeline_since_clause(since_hours: int | None) -> tuple[str, tuple]:
        if since_hours is None or since_hours <= 0:
            return "", ()
        return (
            "WHERE received_at >= datetime('now', ?)",
            (f"-{int(since_hours)} hours",),
        )

    @app.get("/pipeline/stats")
    def pipeline_stats(since_hours: int | None = 24):
        where_sql, params = _pipeline_since_clause(since_hours)
        rows = conn.execute(
            f"SELECT COALESCE(decided_stage, 'unknown') AS bucket, "
            f"COUNT(*) AS n FROM messages {where_sql} "
            f"GROUP BY bucket",
            params,
        ).fetchall()
        counts = {b: 0 for b in _PIPELINE_BUCKETS}
        counts["unknown"] = 0
        for r in rows:
            counts[r["bucket"]] = r["n"]
        return {"since_hours": since_hours, "counts": counts}

    @app.get("/pipeline/messages")
    def pipeline_messages(
        since_hours: int | None = 24,
        stage: str | None = None,
        limit: int = 200,
        offset: int = 0,
    ):
        clauses: list[str] = []
        params: list = []
        if since_hours is not None and since_hours > 0:
            clauses.append("received_at >= datetime('now', ?)")
            params.append(f"-{int(since_hours)} hours")
        if stage:
            if stage == "unknown":
                clauses.append("decided_stage IS NULL")
            else:
                clauses.append("decided_stage = ?")
                params.append(stage)
        where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        # Cap limit defensively — the GUI paginates anyway.
        limit = max(1, min(int(limit), 1000))
        offset = max(0, int(offset))
        rows = conn.execute(
            f"SELECT m.id, m.received_at, m.sender, m.text, "
            f"m.source_channel_id, m.decided_stage, m.decided_outcome, "
            f"m.decided_at, m.pipeline_meta_json, "
            f"(SELECT GROUP_CONCAT(a.id) FROM actions a "
            f" WHERE a.source_msg_id = m.id) AS action_ids "
            f"FROM messages m {where_sql} "
            f"ORDER BY m.received_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        return {
            "since_hours": since_hours,
            "stage": stage,
            "limit": limit,
            "offset": offset,
            "messages": [
                {
                    "id": r["id"],
                    "received_at": r["received_at"],
                    "sender": r["sender"],
                    "text": r["text"],
                    "source_channel_id": r["source_channel_id"],
                    "decided_stage": r["decided_stage"],
                    "decided_outcome": r["decided_outcome"],
                    "decided_at": r["decided_at"],
                    "pipeline_meta_json": r["pipeline_meta_json"],
                    "action_ids": (
                        [int(x) for x in r["action_ids"].split(",")]
                        if r["action_ids"] else []
                    ),
                }
                for r in rows
            ],
        }

    @app.post("/actions/{action_id}/claim")
    def claim_action(action_id: int):
        now = datetime.now(timezone.utc).isoformat()
        cur = conn.execute(
            "UPDATE actions SET status='claimed', claimed_at=? "
            "WHERE id=? AND status='sent'",
            (now, action_id),
        )
        if cur.rowcount == 0:
            row = conn.execute("SELECT status FROM actions WHERE id=?", (action_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "action not found")
            raise HTTPException(409, f"action is {row['status']}, not sent")
        trades.info("action_claimed id=%s", action_id)
        return {"ok": True, "claimed_at": now}

    @app.post("/actions/{action_id}/result")
    def post_result(action_id: int, body: ResultBody):
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        now = datetime.now(timezone.utc).isoformat()

        # A watching->watching re-post happens when the EA re-places a pending
        # order after a resize: it updates ea_response with the new ticket but
        # is NOT a terminal transition, so don't re-stamp executed_at or fire
        # the terminal notification.
        is_rewatch = body.status == "watching" and row["status"] == "watching"
        result_executed_at = row["executed_at"] if is_rewatch else now

        # Resolve legs OUTSIDE the transaction so we don't hold a write lock
        # while doing trivial dict lookups. Legs are only relevant for the
        # executed branch; failed/rejected results have no position rows.
        legs: list[LegResult] = []
        if body.status == "executed":
            if body.legs:
                legs = list(body.legs)
            elif body.mt5_ticket and body.snapshot:
                legs = [LegResult(mt5_ticket=body.mt5_ticket, snapshot=body.snapshot)]

        # Atomic: the action's terminal-state UPDATE and the position
        # INSERT(s) must commit together. The connection runs in
        # isolation_level=None (autocommit) per src/db.py, so without
        # this explicit BEGIN/COMMIT each statement is its own
        # transaction. A crash or EA retry between them would leave the
        # action in `executed` with no position row — the AI's SYSTEM
        # STATE block would then show no open position and could emit
        # another OPEN.
        # OR IGNORE on positions (not OR REPLACE): a re-POST for an
        # already-inserted ticket must not resurrect a position that has
        # since been closed (manual MT5 close, reconcile, etc.). First
        # insert wins.
        conn.execute("BEGIN")
        try:
            # Only accept result POSTs from non-terminal owning states:
            #   - 'claimed' is the normal path (EA claimed then executed)
            #   - 'watching' is the limit-order path (EA placed BuyLimit
            #     earlier with status='watching', limit fires later and the
            #     EA POSTs the executed result)
            # If the row is in 'sent' (release_stale_claims recycled the
            # claim) or already terminal (executed/failed/rejected — a
            # duplicate retry), refuse the POST. This is the safety net
            # against double broker orders from one AI signal (REVIEW.md
            # P0).
            cur = conn.execute(
                "UPDATE actions SET status=?, executed_at=?, ea_response=? "
                "WHERE id=? AND status IN ('claimed','watching')",
                (body.status, result_executed_at, body.error, action_id),
            )
            claim_expired = cur.rowcount == 0
            if not claim_expired:
                for leg in legs:
                    s = leg.snapshot
                    is_naked = 1 if s.get("is_naked") else 0
                    naked_opened_at = now if is_naked else None
                    conn.execute(
                        "INSERT OR IGNORE INTO positions(action_id, mt5_ticket, symbol, side, "
                        "volume, original_volume, entry_price, sl, tp, status, opened_at, "
                        "is_naked, naked_opened_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?, 'open', ?, ?, ?)",
                        (action_id, leg.mt5_ticket, s.get("symbol"), s.get("side"),
                         s.get("volume"), s.get("volume"),
                         s.get("entry_price"), s.get("sl"), s.get("tp"), now,
                         is_naked, naked_opened_at),
                    )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        # Step 7: per-bot notification fan-out. Writes outbox rows for
        # every bot whose binding covers this destination. No-op when v2
        # bindings absent — legacy bot.py::notification_dispatcher
        # handles those via its existing actions.notified_at poll. Look
        # up the action's source_channel + route from the row so future
        # scope-aware bindings (Step 14) can match correctly.
        if not claim_expired and not is_rewatch:
            try:
                from src.notification_dispatcher import dispatch_notification
                meta = conn.execute(
                    "SELECT source_channel_id, route_id FROM actions WHERE id=?",
                    (action_id,),
                ).fetchone()
                dispatch_notification(
                    conn,
                    event_type="action_terminal",
                    action_id=action_id,
                    source_channel_id=meta["source_channel_id"] if meta else None,
                    route_id=meta["route_id"] if meta else None,
                )
            except Exception:
                log.exception("dispatch_notification(action_terminal) failed; "
                              "legacy notification_dispatcher will still cover")

        if claim_expired:
            # The claim was released by release_stale_claims (or the row was
            # never in `claimed` state). Refuse the late POST so a stale
            # executor cannot overwrite a fresher claim's status — the same
            # action may already have been re-claimed and is mid-flight on
            # the EA. Safety net against double broker orders from one AI
            # signal (REVIEW.md P0).
            current = conn.execute(
                "SELECT status FROM actions WHERE id=?", (action_id,)
            ).fetchone()
            trades.info(
                "action_result_rejected id=%s reason=claim_expired current=%s incoming=%s ticket=%s",
                action_id,
                current["status"] if current else "missing",
                body.status,
                body.mt5_ticket or "",
            )
            raise HTTPException(
                409,
                detail={
                    "error": "claim_expired",
                    "current_status": current["status"] if current else None,
                },
            )

        # Trades log AFTER commit so a rolled-back transaction doesn't
        # leave phantom lifecycle lines in trades.log.
        trades.info(
            "action_result id=%s status=%s ticket=%s error=%s",
            action_id, body.status, body.mt5_ticket or "", body.error or "",
        )
        for leg in legs:
            s = leg.snapshot
            trades.info(
                "position_opened ticket=%s side=%s vol=%s entry=%s sl=%s tp=%s",
                leg.mt5_ticket, s.get("side"), s.get("volume"),
                s.get("entry_price"), s.get("sl"), s.get("tp"),
            )

        # Post-execution evaluator kick.
        #
        # The orchestrator-side kick fires at insert time for OPEN /
        # OPEN_INSTANT, when the signal's side/sl/tps are already in the
        # payload. REOPEN_LAST and REINFORCE have no such payload at
        # insert (just `{within_hours}` / `{side}`) — the actual trade
        # params only materialize here, after the EA reports a filled
        # snapshot. Firing the same evaluator here closes that gap.
        #
        # Skip when payload already carries an `evaluation` key: the
        # orchestrator-side path already scored this action. This keeps
        # OPEN/OPEN_INSTANT behavior unchanged and only adds scoring to
        # the previously-uncovered REOPEN_LAST/REINFORCE paths.
        _maybe_kick_post_execution_evaluator(row, body, legs)

        return {"ok": True}

    @app.get("/positions")
    def list_positions(status: str | None = None, limit: int = 200):
        """EA uses this to reconcile: fetch every position the DB still
        thinks is open, then verify against MT5. Any ticket the DB has as
        open but MT5 doesn't know about gets closed via /positions/{t}/close."""
        if limit < 1 or limit > 1000:
            limit = 200
        if status is None:
            rows = conn.execute(
                "SELECT id, mt5_ticket, symbol, side, status, opened_at, closed_at "
                "FROM positions ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, mt5_ticket, symbol, side, status, opened_at, closed_at "
                "FROM positions WHERE status=? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        return {"positions": [dict(r) for r in rows]}

    @app.post("/positions/{ticket}/update")
    def update_position(ticket: int, body: PositionUpdateBody):
        """EA calls this after partial closes or SL moves so the DB reflects
        the live state. Any combination of volume/sl/tp may be provided.
        Only touches open positions; closed ones are immutable here.

        Side-effects (Phase-1 state plumbing):
          - When `volume` arrives strictly less than the current row's volume,
            partial_close_count is incremented. This is what the AI prompt
            checks to skip "متاح حجز الارباح لو ما لحقت" reminders that would
            otherwise re-fire CLOSE_PARTIAL on an already-closed position.
          - When `sl` arrives different from the current row's sl AND
            sl_moved_at is still NULL, sl_moved_at is set to now(). This
            lets the prompt distinguish "SL is still original" from "SL has
            been moved at least once" without trying to compare floats.
        """
        row = conn.execute(
            "SELECT status, volume, original_volume, sl, sl_moved_at "
            "FROM positions WHERE mt5_ticket=?",
            (ticket,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        if row["status"] != "open":
            raise HTTPException(409, f"position is {row['status']}")
        sets: list[str] = []
        args: list = []
        explicit_fields = 0  # what the caller asked us to update (volume/sl/tp)
        if body.volume is not None:
            sets.append("volume=?"); args.append(body.volume)
            explicit_fields += 1
            if row["volume"] is not None and body.volume < row["volume"]:
                sets.append("partial_close_count = partial_close_count + 1")
            # Heal original_volume when the snapshot lower-bound was stamped
            # before the broker filled the full requested lot (e.g. first
            # /post_result captured a partial fill, the broker then topped up
            # the remainder, and this /update is the first reflection of the
            # true full lot). Without healing, the AI prompt would render
            # "0.10 of 0.05 orig" — visibly wrong and breaks the
            # idempotency rules that key off original_volume (REVIEW.md P2).
            if (
                row["original_volume"] is not None
                and body.volume > row["original_volume"]
            ):
                sets.append("original_volume=?")
                args.append(body.volume)
        if body.sl is not None:
            sets.append("sl=?"); args.append(body.sl)
            explicit_fields += 1
            if row["sl"] != body.sl and row["sl_moved_at"] is None:
                sets.append("sl_moved_at=?")
                args.append(datetime.now(timezone.utc).isoformat())
        if body.tp is not None:
            sets.append("tp=?"); args.append(body.tp)
            explicit_fields += 1
        if body.realized_pnl_delta is not None:
            # Add the partial-close P&L to the running total. COALESCE handles
            # the first delta (NULL + x = x). Doesn't count toward
            # explicit_fields because the EA may bundle a delta with a
            # volume/sl/tp update; a "delta only" call is a legitimate
            # partial-deal-only POST so we let it through with no field flag.
            sets.append("realized_pnl = COALESCE(realized_pnl, 0) + ?")
            args.append(body.realized_pnl_delta)
        if not sets:
            return {"ok": True, "updated": 0}
        args.append(ticket)
        conn.execute(
            f"UPDATE positions SET {', '.join(sets)} WHERE mt5_ticket=?",
            args,
        )
        if body.volume is not None and row["volume"] is not None and body.volume < row["volume"]:
            trades.info(
                "position_partial ticket=%s vol_before=%s vol_after=%s",
                ticket, row["volume"], body.volume,
            )
        if body.sl is not None and row["sl"] != body.sl:
            trades.info(
                "position_sl_moved ticket=%s sl_before=%s sl_after=%s",
                ticket, row["sl"], body.sl,
            )
        return {"ok": True, "updated": explicit_fields}

    @app.post("/positions/{ticket}/attach_signal")
    def attach_signal(ticket: int, body: AttachSignalBody):
        """EA calls this after wiring a structured signal's SL/TP into a
        previously naked OPEN_INSTANT position. Clears the naked flag,
        records new SL/TP, and stamps sl_moved_at if this is the first SL
        move. Idempotent on a non-naked open row (just updates sl/tp).
        """
        row = conn.execute(
            "SELECT status, sl, sl_moved_at, is_naked FROM positions WHERE mt5_ticket=?",
            (ticket,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        if row["status"] != "open":
            raise HTTPException(409, f"position is {row['status']}")
        sets = ["is_naked=0", "naked_opened_at=NULL", "sl=?", "tp=?"]
        args: list = [body.sl, body.tp]
        if row["sl"] != body.sl and row["sl_moved_at"] is None:
            sets.append("sl_moved_at=?")
            args.append(datetime.now(timezone.utc).isoformat())
        args.append(ticket)
        conn.execute(
            f"UPDATE positions SET {', '.join(sets)} WHERE mt5_ticket=?",
            args,
        )
        trades.info(
            "position_attach_signal ticket=%s sl_before=%s sl_after=%s tp=%s",
            ticket, row["sl"], body.sl, body.tp,
        )
        return {"ok": True, "was_naked": bool(row["is_naked"])}

    @app.post("/positions/{ticket}/close")
    def close_position(ticket: int, body: CloseBody):
        """Idempotent: re-POSTing close for an already-closed row is a no-op.

        Historical context: the EA's ReconcileClosedPositions used to scan
        48h of MT5 history every OnTimer tick and POST close for every
        DEAL_ENTRY_OUT it found, including ones already in our DB as
        'closed'. Without the status='open' guard below, every tick
        re-stamped closed_at — which the position_close_notifier then
        read as a "new" close and DM'd indefinitely.

        Defense-in-depth (Phase 3): that same history-deal scan also
        mistakenly POSTed close for partial-close deals (in hedging mode
        every partial is also DEAL_ENTRY_OUT). The scan has been removed
        in the EA, but as a backstop we refuse any reason='mt5_close'
        that arrives while the position is in mid-partial state
        (partial_close_count > 0 AND volume < original_volume). The
        legitimate full-close path goes through the EA's authoritative
        pass with reason='mt5_not_found', which is unambiguous.
        AI-driven and operator-driven closes use their own explicit
        reasons (ai_close_full, etc.) and pass through.
        """
        row = conn.execute(
            "SELECT status, volume, original_volume, partial_close_count "
            "FROM positions WHERE mt5_ticket=?",
            (ticket,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)

        # Backstop against the false-close failure mode (see docstring).
        # Only fires for reason='mt5_close' on an open, mid-partial position.
        if (
            body.reason == "mt5_close"
            and row["status"] == "open"
            and (row["partial_close_count"] or 0) > 0
            and row["original_volume"] is not None
            and row["volume"] is not None
            and row["volume"] < row["original_volume"]
        ):
            log.warning(
                "/close skipped: ticket=%s reason=mt5_close in partial state "
                "(vol=%s of %s, partial_close_count=%s) — likely stale "
                "DEAL_ENTRY_OUT scan",
                ticket, row["volume"], row["original_volume"],
                row["partial_close_count"],
            )
            trades.info(
                "position_close_skipped ticket=%s reason=mt5_close_in_partial_state "
                "vol=%s orig=%s partials=%s",
                ticket, row["volume"], row["original_volume"],
                row["partial_close_count"],
            )
            return {"ok": True, "updated": 0, "skipped": "partial_state_mt5_close"}

        # Build the UPDATE dynamically so optional exit_price/realized_pnl
        # only get set when the EA supplied them. Older EA builds POST just
        # `reason` and the new columns stay NULL for those rows.
        sets = ["status='closed'", "closed_at=?", "close_reason=?"]
        params: list = [datetime.now(timezone.utc).isoformat(), body.reason]
        if body.exit_price is not None:
            sets.append("exit_price=?")
            params.append(body.exit_price)
        if body.realized_pnl is not None:
            # COALESCE: if any partial-close deltas already accumulated via
            # /positions/{ticket}/update, the close-time pnl REPLACES the
            # running total — the EA's close handler computes the
            # all-deals total via DEAL_PROFIT and that's authoritative.
            sets.append("realized_pnl=?")
            params.append(body.realized_pnl)
        params.append(ticket)
        cur = conn.execute(
            f"UPDATE positions SET {', '.join(sets)} "
            "WHERE mt5_ticket=? AND status='open'",
            params,
        )
        if cur.rowcount > 0:
            trades.info(
                "position_closed ticket=%s reason=%s exit_price=%s pnl=%s",
                ticket, body.reason or "",
                body.exit_price if body.exit_price is not None else "-",
                body.realized_pnl if body.realized_pnl is not None else "-",
            )
            # Day-3 cleanup: position-close DMs now flow through the v2
            # per-bot dispatcher just like terminal-action and alert
            # events. Closes the Step-7 gap that left position closes on
            # the legacy polling path. No-op when v2 binding absent —
            # legacy position_close_notifier in bot.py still covers that
            # case (mutex enforced at post_init).
            try:
                pos = conn.execute(
                    "SELECT id, action_id FROM positions WHERE mt5_ticket=?",
                    (ticket,),
                ).fetchone()
                if pos is not None:
                    meta = None
                    if pos["action_id"] is not None:
                        meta = conn.execute(
                            "SELECT source_channel_id, route_id "
                            "FROM actions WHERE id=?",
                            (pos["action_id"],),
                        ).fetchone()
                    from src.notification_dispatcher import dispatch_notification
                    dispatch_notification(
                        conn,
                        event_type="position_closed",
                        action_id=pos["action_id"],
                        source_channel_id=meta["source_channel_id"] if meta else None,
                        route_id=meta["route_id"] if meta else None,
                        extra_payload={"position_id": pos["id"]},
                    )
            except Exception:
                log.exception(
                    "dispatch_notification(position_closed) failed for "
                    "ticket=%s; legacy position_close_notifier will still "
                    "cover (when active).", ticket,
                )
        return {"ok": True, "updated": cur.rowcount}

    @app.post("/positions/recover")
    def recover_position(body: PositionRecoverBody):
        """Reverse-reconciliation sink. The EA's MT5->DB pass POSTs here when
        it holds a live broker position (our magic number + symbol) that the
        DB has no open row for — the orphan case from a lost OPEN result POST.
        Insert it as an `open` row flagged recovered=1 and DM the operator.

        Idempotency / safety:
          - INSERT OR IGNORE on the UNIQUE mt5_ticket: a repeat POST for the
            same ticket is a no-op, AND an existing `closed` row for that
            ticket is never resurrected (first-insert-wins, same rule the
            post_result OR IGNORE relies on). Only a genuinely new ticket
            inserts a row and fires the alert.
          - original_volume snapshots volume (mirrors post_result) so the
            AI's partial-close reasoning has a baseline.
        """
        if body.symbol.upper() not in config.SUPPORTED_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol: {body.symbol}")
        now = datetime.now(timezone.utc).isoformat()
        alert_text = (
            f"Recovered orphan position ticket={body.mt5_ticket} "
            f"{body.side} {body.symbol.upper()} vol={body.volume:g} "
            f"entry={body.entry_price:g} — broker had it open but the DB did "
            f"not (likely a lost OPEN result POST). Review."
        )
        # Atomic: the recovered position row and the operator-alert row must
        # commit together. Under isolation_level=None (autocommit) each
        # statement is its own transaction, so without this BEGIN/COMMIT a
        # crash between the two INSERTs could leave a recovered position with
        # no alert — the divergence would never surface to the operator.
        # Mirrors post_result's atomicity guard.
        alert_id: int | None = None
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT OR IGNORE INTO positions(action_id, mt5_ticket, symbol, side, "
                "volume, original_volume, entry_price, sl, tp, status, opened_at, recovered) "
                "VALUES(NULL, ?, ?, ?, ?, ?, ?, ?, ?, 'open', ?, 1)",
                (body.mt5_ticket, body.symbol.upper(), body.side, body.volume,
                 body.volume, body.entry_price, body.sl, body.tp, now),
            )
            inserted = cur.rowcount > 0
            if inserted:
                # DM the operator: a recovered position means an OPEN result
                # POST was lost. Insert an ALERT terminal row (the
                # notification_dispatcher DMs executed ALERTs), mirroring
                # post_alert.
                alert_payload = json.dumps({"level": "warning", "text": alert_text})
                alert_cur = conn.execute(
                    "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                    "status, executed_at) VALUES(NULL, 'ALERT', ?, 'executed', ?)",
                    (alert_payload, now),
                )
                alert_id = alert_cur.lastrowid
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

        if not inserted:
            # Ticket already known (open OR closed) — no-op, no alert. This is
            # the guard that stops the EA's per-tick reverse pass from
            # re-recovering a position the DB already tracks or has closed.
            return {"ok": True, "recovered": False, "mt5_ticket": body.mt5_ticket}

        # Log + fire the v2 per-bot dispatch AFTER commit so a rolled-back
        # transaction leaves no phantom lifecycle lines (same discipline as
        # post_result).
        trades.info(
            "position_recovered ticket=%s side=%s vol=%s entry=%s sl=%s tp=%s",
            body.mt5_ticket, body.side, body.volume, body.entry_price,
            body.sl if body.sl is not None else "-",
            body.tp if body.tp is not None else "-",
        )
        trades.info("alert_posted id=%s level=warning (position_recovered)", alert_id)
        try:
            from src.notification_dispatcher import dispatch_notification
            dispatch_notification(
                conn,
                event_type="alert",
                action_id=alert_id,
                extra_payload={"level": "warning", "text": alert_text},
            )
        except Exception:
            log.exception("dispatch_notification(alert) failed for recovered "
                          "position ticket=%s; legacy notification_dispatcher "
                          "will still cover", body.mt5_ticket)
        return {
            "ok": True, "recovered": True,
            "mt5_ticket": body.mt5_ticket, "alert_id": alert_id,
        }

    @app.get("/positions/last_closed")
    def last_closed_position(symbol: str = "XAUUSD", within_hours: int = 24):
        """Most recent REPLAYABLE closed position for `symbol` within the
        last `within_hours` hours, joined with the originating signal
        payload so the EA can reconstruct entry zone / SL / TPs for
        REOPEN_LAST and REINFORCE.

        "Replayable" means the signal payload (direct join, or walked
        forward to ATTACH_SIGNAL for OPEN_INSTANT lineage) has
        entry_low + entry_high + sl + non-empty tps. Closed positions
        whose origin is an OPEN_INSTANT that never received an
        ATTACH_SIGNAL are skipped: the EA's BuildOpenPayloadFromLastClosed
        parser rejects them with `last_closed_unparseable`, so surfacing
        them here only triggers downstream failures. Walking back to
        the next candidate is the honest behavior — REOPEN_LAST is a
        "replay the most recent SIGNAL," not "replay the most recent
        execution."

        404 when nothing replayable in window — caller should treat that
        as "no recent trade to reopen" and skip the action (the AI
        prompt's rules say emit ALERT instead).
        """
        if within_hours < 1 or within_hours > 168:
            within_hours = 24
        found = find_last_replayable_closed(
            conn, symbol=symbol, within_hours=within_hours,
        )
        if found is None:
            raise HTTPException(404, "no replayable closed position in window")
        row, signal = found
        return {
            "ticket": row["mt5_ticket"],
            "symbol": row["symbol"],
            "side": row["side"],
            "original_volume": row["original_volume"],
            "volume_at_close": row["volume"],
            "partial_close_count": row["partial_close_count"],
            "entry_price": row["entry_price"],
            "sl_at_close": row["sl"],
            "tp_at_close": row["tp"],
            "opened_at": row["opened_at"],
            "closed_at": row["closed_at"],
            "close_reason": row["close_reason"],
            "signal": signal,
        }

    @app.get("/positions/by_ticket/{ticket}")
    def position_by_ticket(ticket: int):
        """Return the open position for `ticket` joined with the originating
        signal payload — same shape as /positions/last_closed but for the
        live position. The EA uses this for REINFORCE so it can snapshot
        the signal params (entry zone, SL, TPs, side) BEFORE closing the
        position; otherwise the fire-and-forget /positions/{t}/close POST
        races against the subsequent /positions/last_closed query and may
        return an older trade's parameters.
        """
        row = conn.execute(
            "SELECT p.mt5_ticket, p.symbol, p.side, p.volume, p.original_volume, "
            "       p.partial_close_count, p.entry_price, p.sl, p.tp, "
            "       p.opened_at, p.status, p.action_id, "
            "       a.payload_json AS signal_payload "
            "FROM positions p "
            "LEFT JOIN actions a ON a.id = p.action_id "
            "WHERE p.mt5_ticket = ? AND p.status = 'open'",
            (ticket,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no open position for ticket")
        # Walk forward to ATTACH_SIGNAL if the originating action lacks
        # signal fields (Phase-4 OPEN_INSTANT lineage). Same reasoning as
        # /positions/last_closed above.
        signal = _resolve_signal_payload(
            conn,
            joined_payload=row["signal_payload"],
            symbol=row["symbol"],
            side=row["side"],
            opened_at=row["opened_at"],
            until=None,
        )
        return {
            "ticket": row["mt5_ticket"],
            "symbol": row["symbol"],
            "side": row["side"],
            "volume": row["volume"],
            "original_volume": row["original_volume"],
            "partial_close_count": row["partial_close_count"],
            "entry_price": row["entry_price"],
            "sl": row["sl"],
            "tp": row["tp"],
            "opened_at": row["opened_at"],
            "status": row["status"],
            "signal": signal,
        }

    @app.get("/actions/latest_open_evaluation")
    def latest_open_evaluation():
        """Return the most recent OPEN action's signal-quality evaluation
        (set by src/ai_evaluator.py inside the action's payload_json
        under the 'evaluation' key). Used by the EA dashboard to render
        the SIGNAL QUALITY widget.

        Shape of returned evaluation field is whatever the evaluator
        wrote (typically `{score:int, verdict:str, key_factor:str,
        summary:str, factors:dict, data_quality:str}`). 404 when no
        OPEN action exists or none has been evaluated yet.
        """
        row = conn.execute(
            "SELECT id, source_msg_id, payload_json, created_at "
            "FROM actions WHERE action_type='OPEN' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no OPEN action yet")
        try:
            payload = json.loads(row["payload_json"]) if row["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        evaluation = payload.get("evaluation")
        if evaluation is None:
            raise HTTPException(404, "latest OPEN has no evaluation")
        return {
            "action_id": row["id"],
            "source_msg_id": row["source_msg_id"],
            "created_at": row["created_at"],
            "evaluation": evaluation,
        }

    # Registered AFTER the literal /actions/latest_open_evaluation so
    # FastAPI doesn't match "latest_open_evaluation" against the int
    # path-param. EA's ManagePendingOrders() polls this to detect
    # server-side cancellation of a pending limit (watching -> rejected).
    @app.get("/actions/{action_id}")
    def get_action(action_id: int):
        """Return a single action row by id. 404 when the id doesn't
        exist; the EA treats that the same as a cancellation."""
        row = conn.execute(
            "SELECT id, action_type, payload_json, status, ea_response, "
            "       created_at, executed_at "
            "FROM actions WHERE id=?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        return {
            "id": row["id"],
            "action_type": row["action_type"],
            "payload": json.loads(row["payload_json"]) if row["payload_json"] else {},
            "status": row["status"],
            "ea_response": row["ea_response"] or "",
            "created_at": row["created_at"],
            "executed_at": row["executed_at"],
        }

    # Max lots accepted from the operator override. Pulled from
    # settings.risk_budget.max_open_lots when present; otherwise a sane
    # absolute ceiling so a fat-finger can't request 500 lots.
    _RESIZE_MAX_LOTS_FALLBACK = 100.0
    _BALANCE_STALE_SEC = 60

    def _max_open_lots() -> float:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='risk_budget'"
        ).fetchone()
        if row and row["value"]:
            try:
                return float(json.loads(row["value"]).get("max_open_lots")
                             or _RESIZE_MAX_LOTS_FALLBACK)
            except (ValueError, TypeError):
                pass
        return _RESIZE_MAX_LOTS_FALLBACK

    def _fresh_balance() -> float | None:
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('account_balance','account_at')"
            ).fetchall()
        }
        bal, at = rows.get("account_balance"), rows.get("account_at")
        if bal is None or at is None:
            return None
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(at)).total_seconds()
        except ValueError:
            return None
        if age > _BALANCE_STALE_SEC:
            return None
        try:
            return float(bal)
        except ValueError:
            return None

    @app.post("/actions/{action_id}/resize_pending")
    def resize_pending(action_id: int, body: ResizePendingBody):
        """Set an explicit new lot on a pending (watching) OPEN. Writes
        `pending_lot_override` + a monotonic `resize_seq` into the action
        payload; the EA picks it up on its next /actions/{id} poll, deletes
        the broker order, and re-places at the new lot. Status is unchanged.
        """
        if body.lots <= 0 or body.lots > _max_open_lots():
            raise HTTPException(422, f"lots out of range: {body.lots}")
        row = conn.execute(
            "SELECT action_type, payload_json, status FROM actions WHERE id=?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        payload = parse_payload(row["payload_json"]) or {}
        if (row["status"] != "watching" or row["action_type"] != "OPEN"
                or payload.get("pending") is not True):
            raise HTTPException(
                409,
                f"action {action_id} is not a pending watching OPEN "
                f"(status={row['status']}, type={row['action_type']}, "
                f"pending={payload.get('pending')})",
            )
        payload["pending_lot_override"] = body.lots
        payload["resize_seq"] = int(payload.get("resize_seq", 0)) + 1
        conn.execute("BEGIN")
        try:
            conn.execute(
                "UPDATE actions SET payload_json=? WHERE id=?",
                (json.dumps(payload), action_id),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        entry = (float(payload.get("entry_low", 0)) + float(payload.get("entry_high", 0))) / 2.0
        dollars, pct = _pending_risk(body.lots, entry, float(payload.get("sl", 0)),
                                     _fresh_balance())
        return {
            "lots": body.lots,
            "resize_seq": payload["resize_seq"],
            "risk_dollars": dollars,
            "risk_pct_estimate": pct,
        }

    @app.get("/events/recent")
    def events_recent(limit: int = 20):
        """Recent action stream for the EA's LogPanel widget.

        Returns the last N rows of `actions` (newest-first) enriched with
        a one-line summary string. The summary is produced by the same
        `_payload_summary` helper the bot uses for its terminal-state DMs,
        so the side-panel rows and the operator's Telegram history stay
        in sync wording-wise.

        Response:
          {"events": [
             {"id": int, "type": str, "status": str,
              "summary": str, "ea_response": str|"",
              "created_at": str (ISO-8601 UTC)},
             ...
          ]}

        EA polls this on a 3 s throttle (FetchRecentEvents); LogPanel
        hashes the id list so the panel only repaints when the head
        rows actually change.
        """
        # Local import: avoids forcing the API process to load the
        # telegram-format helpers at startup if they ever grow heavy
        # (today they're tiny — pure-string formatting only).
        from src.telegram_format import _payload_summary
        if limit < 1: limit = 1
        if limit > 200: limit = 200
        # Only surface actions the bot actually DMed to the operator —
        # notified_at is stamped by notification_dispatcher when (or just
        # before) the Telegram DM goes out. This keeps the dashboard log
        # panel in lockstep with what the operator sees in their bot chat.
        rows = conn.execute(
            "SELECT id, action_type, status, payload_json, ea_response, "
            "       created_at "
            "FROM actions WHERE notified_at IS NOT NULL "
            "ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        events: list[dict] = []
        for r in rows:
            try:
                payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
            except (TypeError, ValueError):
                payload = {}
            events.append({
                "id": r["id"],
                "type": r["action_type"],
                "status": r["status"],
                "summary": _payload_summary(r["action_type"], payload),
                "ea_response": r["ea_response"] or "",
                "created_at": r["created_at"],
            })
        return {"events": events}

    @app.post("/alerts")
    def post_alert(body: AlertBody):
        """EA escape hatch for events that need operator attention but
        aren't tied to a specific action lifecycle (e.g. ManagePlans gave
        up on a staged partial after PartialMaxRetries). Inserts an ALERT
        row that the bot's notification_dispatcher already DMs.
        """
        payload = json.dumps({"level": body.level, "text": body.text})
        now = datetime.now(timezone.utc).isoformat()
        # Insert ALERT terminal so notification_dispatcher (executed-only)
        # picks it up. See orchestrator.py + REVIEW.md P1 — ALERTs are
        # notification-only and must not sit in 'pending' forever.
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, "
            "status, executed_at) "
            "VALUES(NULL, 'ALERT', ?, 'executed', ?)",
            (payload, now),
        )
        trades.info("alert_posted id=%s level=%s", cur.lastrowid, body.level)
        # Step 7: enqueue outbox row for the alert. Payload includes
        # level + text so the tailer can render without a fresh fetch.
        try:
            from src.notification_dispatcher import dispatch_notification
            dispatch_notification(
                conn,
                event_type="alert",
                action_id=cur.lastrowid,
                extra_payload={"level": body.level, "text": body.text},
            )
        except Exception:
            log.exception("dispatch_notification(alert) failed; "
                          "legacy notification_dispatcher will still cover")
        return {"ok": True, "id": cur.lastrowid}

    @app.post("/market/snapshot")
    def post_market_snapshot(body: MarketSnapshotBody):
        """Multi-timeframe OHLC + ATR snapshot pushed by the EA every minute.
        Consumed by the signal-quality evaluator (src/ai_evaluator.py) which
        runs async after the orchestrator emits an OPEN action. Stored as
        JSON in the existing settings table to avoid a schema change.

        Stale (>120s) snapshots should be ignored by the consumer; the
        evaluator falls back to a reduced-context score with an explicit
        "data_quality: stale" flag rather than blocking on missing data.
        """
        if body.symbol.upper() not in config.SUPPORTED_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol: {body.symbol}")
        sym = body.symbol.upper()
        now = datetime.now(timezone.utc).isoformat()
        snapshot_dict: dict = {
            "m15": body.m15.model_dump(),
            "h1": body.h1.model_dump(),
            "h4": body.h4.model_dump(),
        }
        # Directional-rubric extensions — only embed when the EA provided them
        # so a downgrade from the new EA back to the old one cleanly drops
        # the extra keys instead of leaving stale rows in settings.
        if body.d1 is not None:               snapshot_dict["d1"] = body.d1.model_dump()
        if body.d1_prev is not None:          snapshot_dict["d1_prev"] = body.d1_prev.model_dump()
        if body.adr20 is not None:            snapshot_dict["adr20"] = body.adr20
        if body.adx_h1 is not None:           snapshot_dict["adx_h1"] = body.adx_h1
        if body.h1_recent_closes is not None: snapshot_dict["h1_recent_closes"] = body.h1_recent_closes
        snapshot_json = json.dumps(snapshot_dict)
        conn.execute("BEGIN")
        try:
            for key, val in (
                (f"market_snapshot_{sym}", snapshot_json),
                (f"market_snapshot_{sym}_at", now),
            ):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "recorded_at": now}

    @app.post("/market/snapshot/state")
    def post_market_snapshot_state(body: MarketSnapshotStateBody):
        """Sentinel for the GUI services-bar: tells the dashboard that
        snapshot publishing is intentionally off vs just stale. EA posts
        this at OnInit (and on input flip) so the Snapshot pill renders
        muted/"off" instead of going red after 3 minutes when the
        operator disables ``EnableMarketSnapshot``.

        Idempotent: stores ``market_snapshot_{sym}_disabled`` = "1"/"0"
        in settings.
        """
        if body.symbol.upper() not in config.SUPPORTED_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol: {body.symbol}")
        sym = body.symbol.upper()
        val = "0" if body.enabled else "1"
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (f"market_snapshot_{sym}_disabled", val),
        )
        conn.commit()
        return {"ok": True, "enabled": body.enabled}

    @app.post("/market/price")
    def post_market_price(body: MarketPriceBody):
        """EA heartbeats the live bid/ask here every few seconds. Stored as
        a settings row keyed by symbol so the AI prompt can pick the most
        recent price for shorthand SL decoding (e.g. "ستوبك 56" -> 4856).
        Stale (>60s) prices should be ignored by the consumer.
        """
        if body.symbol.upper() not in config.SUPPORTED_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol: {body.symbol}")
        now = datetime.now(timezone.utc).isoformat()
        sym = body.symbol.upper()
        # Atomic: a reader between the bid and the at write would otherwise
        # see a fresh bid paired with a stale 'at' timestamp (or worse, the
        # new bid with the old ask). The window is microseconds under WAL
        # but the AI prompt's MARKET block reads all three keys; an
        # inconsistent snapshot can mis-trigger the STALE marker logic.
        rows_to_write = [
            (f"market_{sym}_bid", str(body.bid)),
            (f"market_{sym}_ask", str(body.ask)),
            (f"market_{sym}_at", now),
        ]
        if body.account_balance is not None:
            rows_to_write.append(("account_balance", str(body.account_balance)))
            rows_to_write.append(("account_at", now))
        if body.account_equity is not None:
            rows_to_write.append(("account_equity", str(body.account_equity)))
        conn.execute("BEGIN")
        try:
            for key, val in rows_to_write:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "recorded_at": now}

    @app.get("/market/price")
    def get_market_price(symbol: str = "XAUUSD"):
        sym = symbol.upper()
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?,?,?)",
                (f"market_{sym}_bid", f"market_{sym}_ask", f"market_{sym}_at"),
            ).fetchall()
        }
        bid = rows.get(f"market_{sym}_bid")
        ask = rows.get(f"market_{sym}_ask")
        at = rows.get(f"market_{sym}_at")
        if bid is None or ask is None or at is None:
            raise HTTPException(404, "no market price recorded")
        bid_f = float(bid)
        ask_f = float(ask)
        return {
            "symbol": sym,
            "bid": bid_f,
            "ask": ask_f,
            "mid": (bid_f + ask_f) / 2.0,
            "recorded_at": at,
        }

    _CANDLE_STALE_SEC = 120

    @app.post("/market/candles")
    def post_market_candles(body: MarketCandlesBody):
        """EA pushes a bounded OHLC series for one symbol+timeframe. Stored
        as a JSON blob in settings (mirrors /market/snapshot) so there is no
        schema migration; the GUI chart polls GET /market/candles."""
        if body.symbol.upper() not in config.SUPPORTED_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol: {body.symbol}")
        sym = body.symbol.upper()
        tf = body.timeframe
        now = datetime.now(timezone.utc).isoformat()
        bars_json = json.dumps([b.model_dump() for b in body.bars])
        conn.execute("BEGIN")
        try:
            for key, val in (
                (f"market_candles_{sym}_{tf}", bars_json),
                (f"market_candles_{sym}_{tf}_at", now),
            ):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "recorded_at": now, "count": len(body.bars)}

    @app.get("/market/candles")
    def get_market_candles(symbol: str = "XAUUSD", timeframe: str = "M15"):
        sym = symbol.upper()
        tf = timeframe
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?,?)",
                (f"market_candles_{sym}_{tf}", f"market_candles_{sym}_{tf}_at"),
            ).fetchall()
        }
        raw = rows.get(f"market_candles_{sym}_{tf}")
        at = rows.get(f"market_candles_{sym}_{tf}_at")
        if raw is None or at is None:
            return {"symbol": sym, "timeframe": tf, "bars": [], "at": None, "stale": True}
        try:
            bars = json.loads(raw)
        except (ValueError, TypeError):
            bars = []
        stale = True
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
            stale = age > _CANDLE_STALE_SEC
        except (ValueError, TypeError):
            stale = True
        return {"symbol": sym, "timeframe": tf, "bars": bars, "at": at, "stale": stale}

    return app




def run() -> None:
    import uvicorn
    from pathlib import Path
    from src import config
    from src.ai import AIClient
    from src.ai_triage import TriageClient
    from src.config import AI_TRIAGE_ENABLED, LOGS_DIR
    from src.db import connect, init_schema
    from src.llm_provider import default_interpreter_model, default_triage_model
    from src.notify import notify_owner
    from src.profile_context import get_profile_context
    # Fall back to safe defaults when the operator hasn't populated
    # api_host / api_port in the stack DB yet (fresh install path):
    # this lets the service start with 127.0.0.1:8765 instead of
    # exiting on an empty-host failure. The Settings UI still drives
    # the real values once the wizard / Tuning tab is filled in.
    host = (config.API_HOST or "").strip() or DEFAULT_API_HOST
    port = int(config.API_PORT) if config.API_PORT else DEFAULT_API_PORT
    _enforce_auth_bind_policy(host, config.EA_SHARED_TOKEN)
    conn = connect(config.DB_PATH)
    init_schema(conn)

    # Step 4 of multi-channel plan: the API process now runs the
    # orchestrator for POST /incoming_message. It needs the same runtime
    # context the listener used to own (ProfileContext, AIClient,
    # TriageClient, ai_log_path). Failure to load any of these is
    # tolerated — POST /incoming_message will return 503 with a clear
    # error, but EA endpoints (the bulk of API traffic) still work.
    profile_ctx = None
    try:
        from src.ai import _resolve_profile_path
        profile_path = _resolve_profile_path()
        if profile_path.exists():
            profile_ctx = get_profile_context(
                profile_path,
                name=config.CHANNEL_PROFILE or profile_path.stem,
            )
            log.info(
                "api profile context loaded: name=%s symbol=%s",
                profile_ctx.name, profile_ctx.symbol,
            )
    except (RuntimeError, FileNotFoundError, OSError) as e:
        log.warning("api profile context unavailable: %s", e)

    ai_client = None
    triage_client = None
    try:
        interp_model = default_interpreter_model()
        ai_client = AIClient(
            model=interp_model,
            system_prompt=(profile_ctx.system_prompt if profile_ctx else None),
        )
        if AI_TRIAGE_ENABLED:
            triage_model = default_triage_model()
            triage_client = TriageClient(
                model=triage_model,
                system_prompt=(profile_ctx.triage_prompt if profile_ctx else None),
            )
        log.info("api ai context: provider=%s interpreter=%s triage=%s",
                 config.AI_PROVIDER, interp_model,
                 triage_model if AI_TRIAGE_ENABLED else "disabled")
    except Exception as e:
        # AI provider misconfigured (e.g. missing API key on a fresh install).
        # EA endpoints continue to work; /incoming_message will 503.
        log.warning("api ai context unavailable: %s", e)

    ai_log_path = Path(LOGS_DIR) / "ai_calls.jsonl"

    # Step 4 backfill: tag any pre-v2 rows (NULL source_channel_id) with
    # the channel that routes to this destination. Single-stack today =
    # exactly one route per destination, so the channel is unambiguous.
    # Multi-channel destinations (Step 12 deferred) will skip this — they
    # can't pick "the" channel automatically. No-op when v2 config or
    # matching route can't be found.
    try:
        _backfill_destination_channel_on_startup(conn, config.DB_PATH)
    except Exception as e:
        log.warning("startup channel-id backfill skipped: %s", e)

    app = build_app(
        conn,
        ai_client=ai_client,
        triage_client=triage_client,
        profile=profile_ctx,
        ai_log_path=ai_log_path,
    )
    notify_owner(f"🌐 API started on http://{host}:{port}")
    # timeout_keep_alive=0 disables HTTP keep-alive entirely. MT5's
    # WebRequest doesn't handle keep-alive well — after POSTing, MT5
    # tears down the TCP connection before reading the full response,
    # which uvicorn surfaces as
    #   "WinError 10054 — connection forcibly closed by remote host"
    # in nssm-api.err.log, and the EA logs the partial response as
    # a bogus "HTTP 1003". Forcing Connection: close per request makes
    # MT5's tear-down sequence the EXPECTED flow rather than a race.
    uvicorn.run(app, host=host, port=port, timeout_keep_alive=0)


if __name__ == "__main__":
    run()
