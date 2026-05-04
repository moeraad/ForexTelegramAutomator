import json
import logging
import sqlite3
import time
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.logging_setup import configure_logging, http_logger

log = configure_logging("api")
_http_log = http_logger()


class LegResult(BaseModel):
    mt5_ticket: int
    snapshot: dict


class ResultBody(BaseModel):
    status: str  # "executed" | "failed" | "rejected" | "watching"
    mt5_ticket: int | None = None
    error: str | None = None
    snapshot: dict | None = None
    legs: list[LegResult] | None = None
    watch: dict | None = None          # required when status == "watching"
    expires_at: str | None = None      # ISO-8601 UTC, required when status == "watching"


class CloseBody(BaseModel):
    reason: str = ""


class PositionUpdateBody(BaseModel):
    volume: float | None = None
    sl: float | None = None
    tp: float | None = None


class MarketPriceBody(BaseModel):
    """Heartbeat from the EA so the AI prompt has a current price for
    two-digit SL shorthand decoding (e.g. "ستوبك 56" -> 4856 only if we
    know gold is around 4850).
    """
    symbol: str = "XAUUSD"
    bid: float
    ask: float


def build_app(conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI()

    @app.middleware("http")
    async def http_access_log(request: Request, call_next):
        # One line per request into logs/api_http.log. Survives console
        # closure, so a 3 a.m. 422 from the EA still has forensic history.
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            ms = int((time.perf_counter() - start) * 1000)
            _http_log.info(
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
            "SELECT id, action_type, payload_json, status, created_at, "
            "       watch_json, expires_at "
            "FROM actions WHERE status=? ORDER BY id ASC LIMIT ?",
            (status, limit),
        ).fetchall()
        out = []
        for r in rows:
            item = {
                "id": r["id"],
                "action_type": r["action_type"],
                "payload": json.loads(r["payload_json"]),
                "status": r["status"],
                "created_at": r["created_at"],
            }
            if r["watch_json"]:
                item["watch"] = json.loads(r["watch_json"])
            if r["expires_at"]:
                item["expires_at"] = r["expires_at"]
            out.append(item)
        return {"actions": out}

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
        return {"ok": True, "claimed_at": now}

    @app.post("/actions/{action_id}/result")
    def post_result(action_id: int, body: ResultBody):
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        now = datetime.now(timezone.utc).isoformat()
        if body.status == "watching":
            # Synthetic pending: EA is watching the zone, no fill yet.
            # watch + expires_at are required.
            if body.watch is None or body.expires_at is None:
                raise HTTPException(422, "watching requires watch and expires_at")
            conn.execute(
                "UPDATE actions SET status='watching', watch_json=?, expires_at=?, "
                "ea_response=NULL WHERE id=?",
                (json.dumps(body.watch), body.expires_at, action_id),
            )
            return {"ok": True}
        # Terminal states: record executed_at and error string.
        conn.execute(
            "UPDATE actions SET status=?, executed_at=?, ea_response=? WHERE id=?",
            (body.status, now, body.error, action_id),
        )
        if body.status == "executed":
            legs = body.legs
            if legs is None and body.mt5_ticket and body.snapshot:
                legs = [LegResult(mt5_ticket=body.mt5_ticket, snapshot=body.snapshot)]
            for leg in legs or []:
                s = leg.snapshot
                # OR IGNORE (not OR REPLACE): a re-POST for an already-inserted
                # ticket must not resurrect a position that has since been closed
                # (manual MT5 close, reconcile, etc.). First insert wins.
                conn.execute(
                    "INSERT OR IGNORE INTO positions(action_id, mt5_ticket, symbol, side, "
                    "volume, original_volume, entry_price, sl, tp, status, opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?, 'open', ?)",
                    (action_id, leg.mt5_ticket, s.get("symbol"), s.get("side"),
                     s.get("volume"), s.get("volume"),
                     s.get("entry_price"), s.get("sl"), s.get("tp"), now),
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
        if not sets:
            return {"ok": True, "updated": 0}
        args.append(ticket)
        conn.execute(
            f"UPDATE positions SET {', '.join(sets)} WHERE mt5_ticket=?",
            args,
        )
        return {"ok": True, "updated": explicit_fields}

    @app.post("/positions/{ticket}/close")
    def close_position(ticket: int, body: CloseBody):
        """Idempotent: re-POSTing close for an already-closed row is a no-op.

        This matters because the EA's ReconcileClosedPositions scans the
        last 48h of MT5 history every OnTimer tick and POSTs close for
        every closing deal it finds — including ones already in our DB
        as 'closed'. Without the status='open' guard below, every tick
        re-stamped closed_at, which the position_close_notifier then
        read as a "new" close and DM'd indefinitely.
        """
        row = conn.execute(
            "SELECT 1 FROM positions WHERE mt5_ticket=?", (ticket,)
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        cur = conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=? "
            "WHERE mt5_ticket=? AND status='open'",
            (datetime.now(timezone.utc).isoformat(), body.reason, ticket),
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

    @app.post("/market/price")
    def post_market_price(body: MarketPriceBody):
        """EA heartbeats the live bid/ask here every few seconds. Stored as
        a settings row keyed by symbol so the AI prompt can pick the most
        recent price for shorthand SL decoding (e.g. "ستوبك 56" -> 4856).
        Stale (>60s) prices should be ignored by the consumer.
        """
        now = datetime.now(timezone.utc).isoformat()
        sym = body.symbol.upper()
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
