"""Strip every `evaluation` block from `actions.payload_json`.

Usage:
  python tools/clear_evaluations.py --db "C:\\path\\to\\copytrades.db" [--dry-run] [--no-backup]

By default the script:
  1. Counts rows that have `payload_json.evaluation`.
  2. Copies the DB to <db>.bak-<timestamp>.
  3. Runs `UPDATE ... SET payload_json = json_remove(payload_json, '$.evaluation')`.
  4. Re-counts so the operator sees the delta.

`--dry-run` skips the UPDATE so you can see what would change first.
`--no-backup` skips the .bak file (faster, riskier).

Why json_remove vs full payload rewrite: json_remove ships with SQLite
3.38+ which the project already requires (it's used by other migrations).
It preserves the rest of the payload byte-for-byte — no chance of
accidentally dropping `side`, `sl`, `tps`, `comment`, etc. The legacy
v1 evaluator and the new v2 evaluator both wrote into the same
`evaluation` key, so this single strip clears both.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path


_COUNT_SQL = (
    "SELECT COUNT(*) FROM actions "
    "WHERE payload_json IS NOT NULL "
    "  AND json_extract(payload_json, '$.evaluation') IS NOT NULL"
)

_STRIP_SQL = (
    "UPDATE actions "
    "SET payload_json = json_remove(payload_json, '$.evaluation') "
    "WHERE payload_json IS NOT NULL "
    "  AND json_extract(payload_json, '$.evaluation') IS NOT NULL"
)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--db", required=True, help="Path to the stack's copytrades.db")
    p.add_argument("--dry-run", action="store_true",
                   help="Count only; do not modify.")
    p.add_argument("--no-backup", action="store_true",
                   help="Skip the .bak copy before modifying.")
    args = p.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"DB not found: {db_path}", file=sys.stderr)
        return 2

    try:
        conn = sqlite3.connect(str(db_path))
    except sqlite3.Error as e:
        print(f"could not open {db_path}: {e}", file=sys.stderr)
        return 2

    try:
        before = conn.execute(_COUNT_SQL).fetchone()[0]
    except sqlite3.Error as e:
        print(f"count query failed (json1 unavailable?): {e}", file=sys.stderr)
        return 2

    print(f"actions with `evaluation` key: {before}")
    if before == 0:
        print("nothing to strip.")
        return 0

    if args.dry_run:
        print("--dry-run: not modifying.")
        return 0

    if not args.no_backup:
        ts = time.strftime("%Y%m%dT%H%M%S")
        backup = db_path.with_suffix(f"{db_path.suffix}.bak-{ts}")
        print(f"backing up to {backup} ...")
        shutil.copy2(db_path, backup)

    try:
        cur = conn.execute(_STRIP_SQL)
        conn.commit()
    except sqlite3.Error as e:
        print(f"strip query failed: {e}", file=sys.stderr)
        return 2

    after = conn.execute(_COUNT_SQL).fetchone()[0]
    print(f"updated rows: {cur.rowcount}")
    print(f"actions with `evaluation` key after: {after}")
    if after != 0:
        print("warning: some rows still report an evaluation key — "
              "check for corrupt JSON.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
