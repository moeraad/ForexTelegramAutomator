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
                    "volume, entry_price, sl, tp, status, opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?, 'open', ?)",
                    (action_id, leg.mt5_ticket, s.get("symbol"), s.get("side"),
                     s.get("volume"), s.get("entry_price"), s.get("sl"), s.get("tp"), now),
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
        Only touches open positions; closed ones are immutable here."""
        row = conn.execute(
            "SELECT status FROM positions WHERE mt5_ticket=?", (ticket,)
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        if row["status"] != "open":
            raise HTTPException(409, f"position is {row['status']}")
        sets: list[str] = []
        args: list = []
        if body.volume is not None:
            sets.append("volume=?"); args.append(body.volume)
        if body.sl is not None:
            sets.append("sl=?"); args.append(body.sl)
        if body.tp is not None:
            sets.append("tp=?"); args.append(body.tp)
        if not sets:
            return {"ok": True, "updated": 0}
        args.append(ticket)
        conn.execute(
            f"UPDATE positions SET {', '.join(sets)} WHERE mt5_ticket=?",
            args,
        )
        return {"ok": True, "updated": len(sets)}

    @app.post("/positions/{ticket}/close")
    def close_position(ticket: int, body: CloseBody):
        row = conn.execute(
            "SELECT 1 FROM positions WHERE mt5_ticket=?", (ticket,)
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        conn.execute(
            "UPDATE positions SET status='closed', closed_at=?, close_reason=? "
            "WHERE mt5_ticket=?",
            (datetime.now(timezone.utc).isoformat(), body.reason, ticket),
        )
        return {"ok": True}

    return app


def run() -> None:
    import uvicorn
    from src import config
    from src.db import connect, init_schema
    conn = connect(config.DB_PATH)
    init_schema(conn)
    app = build_app(conn)
    uvicorn.run(app, host=config.API_HOST, port=config.API_PORT)


if __name__ == "__main__":
    run()
