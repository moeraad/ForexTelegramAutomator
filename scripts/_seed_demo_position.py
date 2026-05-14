"""One-shot: seed a synthetic open position into the demo DB for GUI testing.

Ticket 99999999 is reserved for demo. Rollback: DELETE FROM positions
WHERE mt5_ticket=99999999;
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

DB = r"C:\Users\Administrator\Desktop\db\copytrades.db"
TICKET = 99999999
ACTION_ID = 258


def main() -> None:
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(DB)
    conn.execute("DELETE FROM positions WHERE mt5_ticket=?", (TICKET,))
    conn.execute(
        """INSERT INTO positions(
            action_id, mt5_ticket, symbol, side, volume, original_volume,
            entry_price, sl, tp, status, opened_at, partial_close_count
        ) VALUES (?, ?, 'XAUUSD', 'BUY', 0.05, 0.05, 4670.00, 4660.00, 4700.00, 'open', ?, 0)""",
        (ACTION_ID, TICKET, now),
    )
    conn.execute(
        "UPDATE settings SET value=? WHERE key='market_XAUUSD_at'",
        (now,),
    )
    conn.commit()
    bid = conn.execute(
        "SELECT value FROM settings WHERE key='market_XAUUSD_bid'"
    ).fetchone()[0]
    pnl = (float(bid) - 4670.0) * 0.05 * 100
    print(f"seeded BUY 0.05 @ 4670  ·  bid {bid}  ·  expected PnL ${pnl:+.2f}")


if __name__ == "__main__":
    main()
