"""Inject a synthetic OPEN action directly into the DB at status='sent' so the
running EA picks it up on its next poll. Bypasses the AI and the orchestrator
so you're testing the EA binary, not the Python pipeline.

Use this to verify the compiled EA handles 3 TPs correctly: MT5-side TP should
land on tp3 (farthest), with partial closes at tp1 and tp2.

Usage:
    python scripts/test_ea_signal.py --price 4734
    python scripts/test_ea_signal.py --price 4734 --side SELL
    python scripts/test_ea_signal.py --price 4734 --tp-offsets 10,20,35

Layout around the current price:
    BUY  → entries [price-1, price],  SL = price-10, TPs = price + offsets
    SELL → entries [price, price+1], SL = price+10, TPs = price - offsets

ONLY RUN ON A DEMO ACCOUNT — the EA will open a real (demo) position.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402


def build_payload(price: float, side: str, tp_offsets: list[float]) -> dict:
    side = side.upper()
    if side not in ("BUY", "SELL"):
        raise ValueError(f"side must be BUY or SELL, got {side!r}")
    if len(tp_offsets) != 3:
        raise ValueError(f"need exactly 3 tp_offsets, got {tp_offsets!r}")

    if side == "BUY":
        entry_low = round(price - 1.0, 2)
        entry_high = round(price, 2)
        sl = round(price - 10.0, 2)
        tps = [round(price + o, 2) for o in tp_offsets]
    else:
        entry_low = round(price, 2)
        entry_high = round(price + 1.0, 2)
        sl = round(price + 10.0, 2)
        tps = [round(price - o, 2) for o in tp_offsets]

    return {
        "type": "OPEN",
        "symbol": "XAUUSD",
        "side": side,
        "entry_low": entry_low,
        "entry_high": entry_high,
        "tps": tps,
        "sl": sl,
        "comment": "ea-test",
    }


def insert_action(db_path: str, payload: dict) -> int:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO actions"
            "(source_msg_id, action_type, payload_json, status, execute_after, created_at) "
            "VALUES(NULL, 'OPEN', ?, 'sent', ?, ?)",
            (json.dumps(payload), now, now),
        )
        conn.commit()
        return int(cur.lastrowid)
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--price", type=float, required=True,
                    help="current XAUUSD price, e.g. 4734")
    ap.add_argument("--side", default="BUY",
                    choices=["BUY", "SELL", "buy", "sell"],
                    help="trade direction (default BUY)")
    ap.add_argument("--tp-offsets", default="10,20,35",
                    help="comma-separated offsets from price for tp1,tp2,tp3 "
                         "(default 10,20,35)")
    ap.add_argument("--db", default=config.DB_PATH, help="sqlite DB path")
    args = ap.parse_args()

    tp_offsets = [float(x) for x in args.tp_offsets.split(",")]
    payload = build_payload(args.price, args.side, tp_offsets)
    action_id = insert_action(args.db, payload)

    print(f"inserted action id={action_id} status=sent")
    print(f"  side   = {payload['side']}")
    print(f"  entry  = {payload['entry_low']} - {payload['entry_high']}")
    print(f"  sl     = {payload['sl']}")
    print(f"  tps    = {payload['tps']}")
    print(f"  tp3    = {payload['tps'][-1]}  <- should be the MT5-side TP on the position")
    print()
    print("The EA polls /actions?status=sent every tick; it should pick this up")
    print("within a second or two. Watch logs/api_http.log and the MT5 Experts tab.")


if __name__ == "__main__":
    main()
