import sqlite3
from collections import defaultdict


def render_open_positions(conn: sqlite3.Connection) -> str:
    rows = conn.execute(
        "SELECT action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp FROM positions WHERE status='open' "
        "ORDER BY action_id, mt5_ticket"
    ).fetchall()

    lines = ["OPEN POSITIONS (from this channel):"]
    if not rows:
        lines.append("  (none)")
        return "\n".join(lines)

    by_signal: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for r in rows:
        by_signal[r["action_id"]].append(r)

    for action_id, group in by_signal.items():
        lines.append(f"  Signal #{action_id}:")
        for r in group:
            lines.append(
                f"    ticket={r['mt5_ticket']}  {r['side']} {r['symbol']}  "
                f"vol={r['volume']:.2f}  entry={r['entry_price']:.2f}  "
                f"sl={r['sl']:.2f}  tp={r['tp']:.2f}"
            )
    return "\n".join(lines)
