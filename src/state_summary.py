import json
import sqlite3
from collections import defaultdict


def _fmt(v: float | None) -> str:
    return f"{v:.2f}" if v is not None else "-"


def _render_executed_positions(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp FROM positions WHERE status='open' "
        "ORDER BY action_id, mt5_ticket"
    ).fetchall()

    lines = ["OPEN POSITIONS (from this channel):"]
    if not rows:
        lines.append("  (none)")
        return lines

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
    return lines


def _render_pending_open_signals(conn: sqlite3.Connection) -> list[str]:
    """List OPEN actions still in the pipeline (not yet filled in MT5).

    Why: AI needs to know about pending limit orders it already produced, so
    it doesn't re-emit them when the channel quotes or repeats the signal.
    """
    rows = conn.execute(
        "SELECT id, payload_json, status FROM actions "
        "WHERE action_type='OPEN' AND status IN ('pending','sent','claimed') "
        "ORDER BY id"
    ).fetchall()

    lines = ["PENDING OPEN SIGNALS (awaiting fill or execution):"]
    if not rows:
        lines.append("  (none)")
        return lines
    for r in rows:
        try:
            p = json.loads(r["payload_json"])
        except (ValueError, TypeError):
            p = {}
        tps = p.get("tps") or []
        tps_str = ",".join(f"{t:g}" for t in tps) if tps else "-"
        lines.append(
            f"  Signal #{r['id']} [{r['status']}]: "
            f"{p.get('side','?')} {p.get('symbol','?')} "
            f"entry={_fmt(p.get('entry_low'))}-{_fmt(p.get('entry_high'))} "
            f"sl={_fmt(p.get('sl'))} tps={tps_str}"
        )
    return lines


def render_open_positions(conn: sqlite3.Connection) -> str:
    """AI context: executed positions + pending OPEN signals not yet filled."""
    parts = _render_executed_positions(conn) + [""] + _render_pending_open_signals(conn)
    return "\n".join(parts)
