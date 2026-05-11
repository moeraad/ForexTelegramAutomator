import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src import config
from src.logging_setup import configure_logging, trades_log

log = configure_logging("api")
trades = trades_log()


class LegResult(BaseModel):
    mt5_ticket: int
    snapshot: dict


class ResultBody(BaseModel):
    # Pydantic Literal rejects any other status string at parse time
    # (returns 422). Without this, the schema's CHECK constraint was the
    # last line of defense against an EA bug or attacker pushing the
    # action into an unconstrained state.
    status: Literal["executed", "failed", "rejected"]
    mt5_ticket: int | None = None
    error: str | None = None
    snapshot: dict | None = None
    legs: list[LegResult] | None = None


class CloseBody(BaseModel):
    reason: str = ""
    # Realized-P&L extension (Step 3 of AI_EVALUATOR_ROADMAP). Both
    # optional — older EA builds posting just `reason` keep working.
    # exit_price = price of the closing deal; realized_pnl = total
    # broker-reported profit on the position (sum across all exit deals).
    exit_price: float | None = None
    realized_pnl: float | None = None


class PositionUpdateBody(BaseModel):
    volume: float | None = None
    sl: float | None = None
    tp: float | None = None
    # Used by the EA on partial closes: the broker-reported P&L of THIS
    # deal alone, added to the position's running realized_pnl. None means
    # "leave realized_pnl alone" — old EA builds keep working.
    realized_pnl_delta: float | None = None


class AttachSignalBody(BaseModel):
    """EA-posted body for /positions/{ticket}/attach_signal.

    Called after the EA successfully modifies the broker-side SL/TP on a
    naked position. Clears is_naked, sets sl/tp on the row. Final-TP only —
    the broker only holds one TP; the full tps[] ladder lives on the
    ATTACH_SIGNAL action payload and is consumed by the EA's RegisterPlan
    for the staged exit.
    """
    sl: float
    tp: float


class MarketPriceBody(BaseModel):
    """Heartbeat from the EA so the AI prompt has a current price for
    two-digit SL shorthand decoding (e.g. "ستوبك 56" -> 4856 only if we
    know gold is around 4850).
    """
    symbol: str = "XAUUSD"
    bid: float
    ask: float


class TimeframeOhlcBody(BaseModel):
    """OHLC + ATR for one timeframe in the market snapshot.

    sma50/sma200 are optional — only the D1 block populates them today
    (the directional evaluator uses D1 trend filters). Older EA builds
    that POST without these fields keep working.
    """
    open: float
    high: float
    low: float
    close: float
    atr14: float
    sma50: float | None = None
    sma200: float | None = None


class MarketSnapshotBody(BaseModel):
    """Multi-timeframe OHLC snapshot pushed by the EA roughly every minute.
    Consumed by the directional-bias evaluator (see src/ai_evaluator.py).

    The original m15/h1/h4 fields are required (older EA builds keep
    working). The directional-rubric extensions (d1, d1_prev, adr20,
    adx_h1, h1_recent_closes) are optional so an EA upgrade can roll out
    independently from the API server. The evaluator falls back to
    `data_quality=reduced` when the new fields are absent.
    """
    symbol: str = "XAUUSD"
    m15: TimeframeOhlcBody
    h1: TimeframeOhlcBody
    h4: TimeframeOhlcBody
    # Directional-rubric extensions (added 2026-05-09).
    d1: TimeframeOhlcBody | None = None         # current day's OHLC + SMA50/200
    d1_prev: TimeframeOhlcBody | None = None    # yesterday's OHLC (for gap/continuation context)
    adr20: float | None = None                  # rolling avg of last 20 D1 ranges
    adx_h1: float | None = None                 # ADX(14) on H1 — trend vs chop
    h1_recent_closes: list[float] | None = None # last ~5 H1 closes (oldest first), for structure


class AlertBody(BaseModel):
    """Operator-visible alert posted by the EA when something needs human
    attention (e.g. a staged partial-close gave up after PartialMaxRetries).
    Inserted into actions as an ALERT row; the bot's notification_dispatcher
    DMs the owner because it already handles ALERT rows.
    """
    level: str = "warning"  # "info" | "warning" | "critical"
    text: str


def build_app(conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def auth_gate(request: Request, call_next):
        # Shared-secret header. When EA_SHARED_TOKEN is blank we run
        # unauthenticated (dev mode + tests that don't set it). When set,
        # every request must carry a matching X-EA-Token header — without
        # it the EA can't reach any endpoint, which is the whole point.
        # Read config.EA_SHARED_TOKEN per-request rather than capturing at
        # middleware-build time so test fixtures can flip it via
        # monkeypatch without rebuilding the app.
        expected = config.EA_SHARED_TOKEN
        if expected:
            if request.headers.get("X-EA-Token") != expected:
                client_host = request.client.host if request.client else "?"
                log.warning("auth rejected from %s on %s %s",
                            client_host, request.method, request.url.path)
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
                log.warning(
                    "%s %s -> %s %dms",
                    request.method, request.url.path, status, ms,
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
            conn.execute(
                "UPDATE actions SET status=?, executed_at=?, ea_response=? WHERE id=?",
                (body.status, now, body.error, action_id),
            )
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
            "SELECT status, volume, sl, sl_moved_at FROM positions WHERE mt5_ticket=?",
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
        return {"ok": True, "updated": cur.rowcount}

    @app.get("/positions/last_closed")
    def last_closed_position(symbol: str = "XAUUSD", within_hours: int = 24):
        """Most recent closed position for `symbol` within the last
        `within_hours` hours, joined with the originating OPEN action's
        payload so the AI can reconstruct the full signal (entry zone,
        SL, TPs, side) for REOPEN_LAST and REINFORCE.

        404 when nothing in window — caller should treat that as
        "no recent trade to reopen" and skip the action.
        """
        if within_hours < 1 or within_hours > 168:
            within_hours = 24
        row = conn.execute(
            "SELECT p.mt5_ticket, p.symbol, p.side, p.volume, p.original_volume, "
            "       p.partial_close_count, p.entry_price, p.sl, p.tp, "
            "       p.opened_at, p.closed_at, p.close_reason, "
            "       a.payload_json AS signal_payload "
            "FROM positions p "
            "LEFT JOIN actions a ON a.id = p.action_id "
            "WHERE p.symbol = ? AND p.status = 'closed' "
            "  AND p.closed_at IS NOT NULL "
            "  AND p.closed_at > datetime('now', ?) "
            "ORDER BY p.closed_at DESC LIMIT 1",
            (symbol, f"-{within_hours} hours"),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no closed position in window")
        try:
            signal = json.loads(row["signal_payload"]) if row["signal_payload"] else None
        except (TypeError, ValueError):
            signal = None
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
            "       p.opened_at, p.status, "
            "       a.payload_json AS signal_payload "
            "FROM positions p "
            "LEFT JOIN actions a ON a.id = p.action_id "
            "WHERE p.mt5_ticket = ? AND p.status = 'open'",
            (ticket,),
        ).fetchone()
        if row is None:
            raise HTTPException(404, "no open position for ticket")
        try:
            signal = json.loads(row["signal_payload"]) if row["signal_payload"] else None
        except (TypeError, ValueError):
            signal = None
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
        cur = conn.execute(
            "INSERT INTO actions(source_msg_id, action_type, payload_json, status) "
            "VALUES(NULL, 'ALERT', ?, 'pending')",
            (payload,),
        )
        trades.info("alert_posted id=%s level=%s", cur.lastrowid, body.level)
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
        conn.execute("BEGIN")
        try:
            for key, val in (
                (f"market_{sym}_bid", str(body.bid)),
                (f"market_{sym}_ask", str(body.ask)),
                (f"market_{sym}_at", now),
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

    return app


def run() -> None:
    import uvicorn
    from src import config
    from src.db import connect, init_schema
    from src.notify import notify_owner
    conn = connect(config.DB_PATH)
    init_schema(conn)
    app = build_app(conn)
    notify_owner(
        f"🌐 API started on http://{config.API_HOST}:{config.API_PORT}"
    )
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)


if __name__ == "__main__":
    run()
