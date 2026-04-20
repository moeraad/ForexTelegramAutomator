import json
import logging
import sqlite3
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel

log = logging.getLogger("copytrades.api")
logging.basicConfig(level=logging.INFO)


class LegResult(BaseModel):
    mt5_ticket: int
    snapshot: dict


class ResultBody(BaseModel):
    status: str  # "executed" | "failed" | "rejected"
    mt5_ticket: int | None = None
    error: str | None = None
    snapshot: dict | None = None
    legs: list[LegResult] | None = None


class CloseBody(BaseModel):
    reason: str = ""


def build_app(conn: sqlite3.Connection) -> FastAPI:
    app = FastAPI()

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
        return {"ok": True, "claimed_at": now}

    @app.post("/actions/{action_id}/result")
    def post_result(action_id: int, body: ResultBody):
        row = conn.execute("SELECT * FROM actions WHERE id=?", (action_id,)).fetchone()
        if row is None:
            raise HTTPException(404)
        now = datetime.now(timezone.utc).isoformat()
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
                conn.execute(
                    "INSERT OR REPLACE INTO positions(action_id, mt5_ticket, symbol, side, "
                    "volume, entry_price, sl, tp, status, opened_at) "
                    "VALUES(?,?,?,?,?,?,?,?, 'open', ?)",
                    (action_id, leg.mt5_ticket, s.get("symbol"), s.get("side"),
                     s.get("volume"), s.get("entry_price"), s.get("sl"), s.get("tp"), now),
                )
        return {"ok": True}

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
