"""Score-vs-outcome calibration for the directional-bias evaluator.

Reads every closed XAUUSD position with a populated `realized_pnl`,
joins to its originating OPEN action's evaluation score, and produces
a bucket table answering: "do high-score signals actually outperform
low-score ones?"

Usage:
    python scripts/score_calibration.py
    python scripts/score_calibration.py --db copytrades.db --out docs/calibration/

Writes a markdown report to `docs/calibration/<YYYY-MM-DD>.md` and prints
the same table to stdout. Failure modes (no closed-with-pnl rows, no
evaluations attached, bad payload JSON) print a single line and exit 0
— this is meant to be safe to wire into a cron job.

Core query:
    SELECT score, side, entry_price, sl, volume, realized_pnl, exit_price
    FROM positions p
    JOIN actions a ON a.id = p.action_id
    WHERE a.action_type = 'OPEN'
      AND p.status = 'closed'
      AND p.realized_pnl IS NOT NULL

R-multiple:
    planned_risk = abs(entry_price - sl) * volume * tick_value
    r_multiple = realized_pnl / planned_risk

For XAUUSD spot CFDs, "tick_value" is broker-dependent. We use 100.0
(standard 100-oz contract: $1 move = $100 per lot). Override via
--tick-value on a different broker or instrument.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Bucket boundaries match the evaluator's verdict bands (ai_evaluator.py).
_BUCKETS: list[tuple[str, int, int]] = [
    ("avoid    (0-39)",   0, 40),
    ("weak     (40-59)", 40, 60),
    ("moderate (60-79)", 60, 80),
    ("strong   (80-100)", 80, 101),
]


def _bucket_for(score: int) -> str | None:
    for label, lo, hi in _BUCKETS:
        if lo <= score < hi:
            return label
    return None


def _r_multiple(side: str, entry: float, sl: float, volume: float,
                pnl: float, tick_value: float) -> float | None:
    """Realized P&L in units of planned risk. Returns None when planned
    risk is zero (degenerate signal — entry==sl). Sign of pnl already
    encodes win/loss (broker-reported)."""
    if entry is None or sl is None or volume is None or pnl is None:
        return None
    planned_risk = abs(entry - sl) * volume * tick_value
    if planned_risk <= 0:
        return None
    return pnl / planned_risk


def collect_rows(conn: sqlite3.Connection, tick_value: float) -> list[dict[str, Any]]:
    """Pull eligible (action, position) pairs and decorate each with
    score + r_multiple. Skips rows with missing evaluation, missing P&L,
    or unparseable payload JSON — those just don't contribute."""
    rows = conn.execute(
        "SELECT a.id AS action_id, a.payload_json, "
        "       p.side, p.entry_price, p.sl, p.volume, p.original_volume, "
        "       p.exit_price, p.realized_pnl, p.opened_at, p.closed_at "
        "FROM actions a "
        "JOIN positions p ON p.action_id = a.id "
        "WHERE a.action_type = 'OPEN' "
        "  AND p.status = 'closed' "
        "  AND p.realized_pnl IS NOT NULL "
        "ORDER BY p.closed_at"
    ).fetchall()
    out: list[dict[str, Any]] = []
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            continue
        evaluation = payload.get("evaluation") or {}
        score = evaluation.get("score")
        if score is None:
            continue
        bucket = _bucket_for(int(score))
        if bucket is None:
            continue
        # Use original_volume (size at OPEN time) for the planned-risk
        # calculation, not residual volume after partials. P&L is already
        # the SUM across all exits, so dividing by planned-risk-on-original
        # gives the canonical R-multiple.
        vol = r["original_volume"] if r["original_volume"] is not None else r["volume"]
        rm = _r_multiple(
            r["side"], r["entry_price"], r["sl"], vol,
            r["realized_pnl"], tick_value,
        )
        out.append({
            "action_id": r["action_id"],
            "score": int(score),
            "bucket": bucket,
            "side": r["side"],
            "entry_price": r["entry_price"],
            "sl": r["sl"],
            "volume": vol,
            "realized_pnl": r["realized_pnl"],
            "exit_price": r["exit_price"],
            "r_multiple": rm,
            "opened_at": r["opened_at"],
            "closed_at": r["closed_at"],
        })
    return out


def render_report(rows: list[dict[str, Any]]) -> str:
    """Markdown report. Bucket-by-bucket: count, win rate, avg P&L,
    median R-multiple. Median is more robust to a single outlier blowout
    than mean for small N (we expect <30 trades/week)."""
    parts: list[str] = []
    parts.append(f"# AI Evaluator Score Calibration - {date.today().isoformat()}")
    parts.append("")
    parts.append(f"Eligible closed positions with score + realized_pnl: **{len(rows)}**")
    parts.append("")
    if not rows:
        parts.append("_No eligible rows. Calibration needs at least one closed "
                     "position with both `evaluation.score` and `realized_pnl` "
                     "populated. Wait for a few cycles after the EA + bot have "
                     "been running with Step 1+2+3 of AI_EVALUATOR_ROADMAP._")
        return "\n".join(parts)

    parts.append("## Bucket summary")
    parts.append("")
    parts.append("| Bucket | Count | Win rate | Avg P&L | Median R |")
    parts.append("|---|---|---|---|---|")
    for label, _, _ in _BUCKETS:
        bucket_rows = [r for r in rows if r["bucket"] == label]
        n = len(bucket_rows)
        if n == 0:
            parts.append(f"| {label} | 0 | - | - | - |")
            continue
        wins = sum(1 for r in bucket_rows if r["realized_pnl"] > 0)
        win_rate = wins / n
        avg_pnl = statistics.mean(r["realized_pnl"] for r in bucket_rows)
        rs = [r["r_multiple"] for r in bucket_rows if r["r_multiple"] is not None]
        median_r = statistics.median(rs) if rs else None
        if median_r is not None:
            parts.append(
                f"| {label} | {n} | {win_rate*100:.0f}% | "
                f"${avg_pnl:+.2f} | {median_r:+.2f}R |"
            )
        else:
            parts.append(
                f"| {label} | {n} | {win_rate*100:.0f}% | ${avg_pnl:+.2f} | - |"
            )
    parts.append("")

    # Top + bottom 5 trades for forensic inspection.
    sorted_rows = sorted(rows, key=lambda r: r["realized_pnl"], reverse=True)
    parts.append("## Top 5 winners")
    parts.append("")
    parts.append("| Action | Score | Side | Entry | Exit | P&L | R |")
    parts.append("|---|---|---|---|---|---|---|")
    for r in sorted_rows[:5]:
        rm = f"{r['r_multiple']:+.2f}" if r["r_multiple"] is not None else "-"
        exit_p = f"{r['exit_price']:.2f}" if r["exit_price"] is not None else "-"
        parts.append(
            f"| #{r['action_id']} | {r['score']} | {r['side']} | "
            f"{r['entry_price']:.2f} | {exit_p} | ${r['realized_pnl']:+.2f} | {rm} |"
        )
    parts.append("")
    parts.append("## Top 5 losers")
    parts.append("")
    parts.append("| Action | Score | Side | Entry | Exit | P&L | R |")
    parts.append("|---|---|---|---|---|---|---|")
    for r in sorted_rows[-5:]:
        rm = f"{r['r_multiple']:+.2f}" if r["r_multiple"] is not None else "-"
        exit_p = f"{r['exit_price']:.2f}" if r["exit_price"] is not None else "-"
        parts.append(
            f"| #{r['action_id']} | {r['score']} | {r['side']} | "
            f"{r['entry_price']:.2f} | {exit_p} | ${r['realized_pnl']:+.2f} | {rm} |"
        )
    parts.append("")
    return "\n".join(parts)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluator score-vs-outcome calibration.")
    parser.add_argument("--db", default="copytrades.db", help="Path to SQLite DB.")
    parser.add_argument("--out", default="docs/calibration",
                        help="Directory for the markdown report.")
    parser.add_argument("--tick-value", type=float, default=100.0,
                        help="$ per 1.0 unit move per 1.0 lot (XAUUSD spot CFD = 100).")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 1

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = collect_rows(conn, args.tick_value)
    report = render_report(rows)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{date.today().isoformat()}.md"
    out_path.write_text(report, encoding="utf-8")

    print(report)
    print(f"\nWritten to: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
