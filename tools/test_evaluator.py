"""Quick diagnostic for the v2 evaluator.

Usage:
  # Tier 1: deterministic only (no LLM, no API key needed).
  python tools/test_evaluator.py --side BUY

  # Tier 2: include the LLM synthesizer.
  python tools/test_evaluator.py --side BUY --with-llm

  # Point at a specific stack DB:
  python tools/test_evaluator.py --side BUY --db "C:\\Users\\Administrator\\Desktop\\copytrades.db"

  # Force a style override via the signal text:
  python tools/test_evaluator.py --side BUY --message "scalp this one"

Prints the full evaluation dict + a one-line summary of which feeds
were available, the regime tag, score, sizing decision, and any vetoes.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from src.db import connect
from src.evaluator.evaluator import evaluate_signal_v2


def _build_llm_client():
    """Construct an ai_client using the project's existing provider
    abstraction. Same path the orchestrator's evaluator worker uses, so
    behavior here matches production."""
    try:
        from src.orchestrator import _build_evaluator_ai_client
    except ImportError:
        print("could not import _build_evaluator_ai_client", file=sys.stderr)
        return None
    return _build_evaluator_ai_client()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--side", choices=["BUY", "SELL"], default="BUY")
    parser.add_argument("--db", default=None, help="Stack DB path; default = config.DB_PATH")
    parser.add_argument("--with-llm", action="store_true",
                        help="Include the LLM synthesizer (costs a call).")
    parser.add_argument("--message", default="", help="Signal text (for style override probe).")
    parser.add_argument("--style", default=None,
                        help="Override the stack default for this run "
                             "(scalp/intraday/swing/position).")
    args = parser.parse_args()

    if args.db is None:
        try:
            from src import config
            db_path = config.DB_PATH
        except Exception as e:
            print(f"could not resolve DB_PATH: {e}", file=sys.stderr)
            return 2
    else:
        db_path = args.db

    db_path = Path(db_path)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    if args.style:
        # Temporarily override the stack default. Uses set_str so the
        # change persists — caller can reset by passing a different
        # --style on a later run.
        from src import db_settings
        db_settings.set_str(db_path, "trading_style", args.style)

    conn = connect(str(db_path))
    ai_client = _build_llm_client() if args.with_llm else None
    if args.with_llm and ai_client is None:
        print("LLM client unavailable — no API key set, or provider import failed.",
              file=sys.stderr)
        return 2

    signal = {"side": args.side, "comment": args.message}
    print(f"=== evaluate_signal_v2 (side={args.side}, llm={'on' if ai_client else 'off'}) ===")
    out = evaluate_signal_v2(signal, conn, ai_client, db_path=db_path)

    # One-line summary.
    print()
    print(f"score:           {out.get('score')}  ({out.get('verdict')})")
    print(f"direction:       {out.get('direction')}")
    print(f"effective_style: {out.get('effective_style')} ({out.get('style_source')})")
    print(f"data_quality:    {out.get('data_quality')}")
    if out.get("vetoes"):
        print(f"vetoes:          {out['vetoes']}")
    regime = out.get("regime") or {}
    if regime:
        print(f"regime:          {regime.get('summary', regime)}")
    sizing = out.get("sizing") or {}
    if sizing:
        print(f"sizing:          mult={sizing.get('multiplier')} "
              f"skip={sizing.get('skip')} p={sizing.get('p_profit_used')} "
              f"({sizing.get('reason')})")
    probs = out.get("probabilities")
    if probs:
        print("probabilities:")
        for p in probs:
            print(f"   {p.get('minutes')}min: p_profit={p.get('p_profit')} "
                  f"p_drawdown_first={p.get('p_drawdown_first')}")
    if out.get("edge_thesis"):
        print(f"edge_thesis:     {out['edge_thesis']}")
    if out.get("key_risks"):
        print("key_risks:")
        for r in out["key_risks"]:
            print(f"   - {r}")
    if out.get("missing"):
        print(f"missing_features ({len(out['missing'])}):")
        for m in out["missing"][:15]:
            print(f"   - {m}")

    print()
    print("=== full evaluation dict ===")
    print(json.dumps(out, indent=2, ensure_ascii=False, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
