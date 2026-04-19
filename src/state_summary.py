import sqlite3
from collections import defaultdict


def _fmt(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "-"


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
                f"vol={_fmt(r['volume'])}  entry={_fmt(r['entry_price'])}  "
                f"sl={_fmt(r['sl'])}  tp={_fmt(r['tp'])}"
            )
    return "\n".join(lines)
