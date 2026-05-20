"""Pretty-print the most recent live evaluation from a stack's DB.

Usage:
  python tools/inspect_last_eval.py [--db PATH] [--n 5]

Filters for action types that the evaluator runs on (OPEN, OPEN_INSTANT,
REOPEN_LAST, REINFORCE) and pulls the `evaluation` block from each row's
payload_json. Useful for "what did the v2 evaluator just say about my
last trade?" forensic review.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", default=None)
    p.add_argument("--n", type=int, default=5)
    args = p.parse_args()

    if args.db is None:
        try:
            from src import config
            db_path = config.DB_PATH
        except Exception as e:
            print(f"could not resolve DB_PATH: {e}", file=sys.stderr)
            return 2
    else:
        db_path = args.db

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    rows = list(conn.execute(
        "SELECT id, action_type, status, created_at, payload_json "
        "FROM actions "
        "WHERE action_type IN ('OPEN','OPEN_INSTANT','REOPEN_LAST','REINFORCE') "
        "ORDER BY id DESC LIMIT ?",
        (args.n,),
    ))
    if not rows:
        print("no OPEN-shape actions found")
        return 0
    for r in rows:
        print("=" * 60)
        print(f"id={r['id']}  type={r['action_type']}  status={r['status']}  "
              f"at={r['created_at']}")
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            print("  payload_json: <unparseable>")
            continue
        evaluation = payload.get("evaluation")
        if not evaluation:
            print("  no evaluation key in payload")
            continue
        ver = evaluation.get("evaluator_version", "v1")
        print(f"  evaluator: {ver}")
        print(f"  score: {evaluation.get('score')} ({evaluation.get('verdict')})")
        if "effective_style" in evaluation:
            print(f"  style: {evaluation['effective_style']} "
                  f"({evaluation.get('style_source')})")
        regime = evaluation.get("regime") or {}
        if isinstance(regime, dict) and regime.get("summary"):
            print(f"  regime: {regime['summary']}")
        if evaluation.get("vetoes"):
            print(f"  vetoes: {evaluation['vetoes']}")
        sizing = evaluation.get("sizing") or {}
        if sizing:
            print(f"  sizing: mult={sizing.get('multiplier')} "
                  f"skip={sizing.get('skip')} "
                  f"p={sizing.get('p_profit_used')}")
        probs = evaluation.get("probabilities")
        if probs:
            print("  probabilities:")
            for pp in probs:
                print(f"    {pp.get('minutes')}min -> "
                      f"p_profit={pp.get('p_profit')} "
                      f"p_drawdown_first={pp.get('p_drawdown_first')}")
        if evaluation.get("edge_thesis"):
            print(f"  edge_thesis: {evaluation['edge_thesis']}")
        if evaluation.get("key_risks"):
            print("  key_risks:")
            for risk in evaluation["key_risks"]:
                print(f"    - {risk}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
