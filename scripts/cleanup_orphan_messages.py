"""Bulk-delete messages that no action ever referenced.

Per project policy the messages table only retains rows that produced at
least one action. The orchestrator already enforces that going forward
(see src/orchestrator.py:_cleanup_message_if_orphan), but pre-policy
backlogs may have thousands of stale rows. This script finds them and
removes them in one pass.

Safety:
  - Dry-run by default. You must pass --execute to actually delete.
  - signal_memory rows referencing an orphan message have their
    message_id NULLed first so the FK doesn't dangle. The distilled
    summary survives.
  - Reports counts before and after.
  - --vacuum reclaims disk space; skip if services are running and you
    care about uptime (VACUUM briefly locks the DB).
  - Safe to run while the listener/bot/api are running, but reads under
    WAL mode see a snapshot — services may briefly observe rows about to
    be deleted as still present until the transaction commits.

Examples:
  python scripts/cleanup_orphan_messages.py            # dry-run
  python scripts/cleanup_orphan_messages.py --execute  # actually delete
  python scripts/cleanup_orphan_messages.py --execute --vacuum
  python scripts/cleanup_orphan_messages.py --execute --samples 5
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow `python scripts/cleanup_orphan_messages.py` from the project root
# without `pip install -e .` re-routing the import. Same shim pattern as
# scripts/test_ai_messages.py.
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import config  # noqa: E402
from src.db import connect  # noqa: E402


_ORPHAN_FILTER = """
    LEFT JOIN actions a ON a.source_msg_id = m.id
    WHERE a.id IS NULL
"""


def _count(conn, sql: str) -> int:
    return int(conn.execute(sql).fetchone()[0])


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db", default=config.DB_PATH,
                        help=f"DB path (default: {config.DB_PATH})")
    parser.add_argument("--execute", action="store_true",
                        help="Actually delete. Without this flag, runs in dry-run mode.")
    parser.add_argument("--vacuum", action="store_true",
                        help="VACUUM after deleting. Reclaims disk space; "
                             "briefly locks the DB. Skip if services are running.")
    parser.add_argument("--samples", type=int, default=0, metavar="N",
                        help="Print the first N orphan rows (id, tg_message_id, "
                             "first 60 chars of text) so you can sanity-check "
                             "before passing --execute.")
    args = parser.parse_args()

    conn = connect(args.db)

    total = _count(conn, "SELECT COUNT(*) FROM messages")
    orphans = _count(conn, f"SELECT COUNT(*) FROM messages m {_ORPHAN_FILTER}")
    sm_refs = _count(conn, f"""
        SELECT COUNT(*) FROM signal_memory sm
        WHERE sm.message_id IN (
            SELECT m.id FROM messages m {_ORPHAN_FILTER}
        )
    """)

    print(f"DB: {args.db}")
    print(f"Total messages: {total}")
    print(f"Orphans (no action references): {orphans}")
    print(f"signal_memory rows referencing orphans: {sm_refs}")
    print(f"Will keep: {total - orphans}")

    if args.samples > 0 and orphans > 0:
        print()
        print(f"--- First {args.samples} orphan(s) ---")
        rows = conn.execute(f"""
            SELECT m.id, m.tg_message_id, m.sender, m.text
            FROM messages m {_ORPHAN_FILTER}
            ORDER BY m.id LIMIT ?
        """, (args.samples,)).fetchall()
        for r in rows:
            text = (r["text"] or "").replace("\n", " ")[:60]
            print(f"  id={r['id']:>6} tg={r['tg_message_id']:>8} "
                  f"sender={r['sender']!r:<20} text={text!r}")

    if not args.execute:
        print()
        print("[dry-run] no changes made. Re-run with --execute to delete.")
        return 0

    if orphans == 0:
        print()
        print("Nothing to delete. Done.")
        return 0

    print()
    print("Executing cleanup...")
    sm_updated = conn.execute(f"""
        UPDATE signal_memory SET message_id = NULL
        WHERE message_id IN (
            SELECT m.id FROM messages m {_ORPHAN_FILTER}
        )
    """).rowcount
    deleted = conn.execute(f"""
        DELETE FROM messages WHERE id IN (
            SELECT m.id FROM messages m {_ORPHAN_FILTER}
        )
    """).rowcount
    print(f"  signal_memory rows nulled: {sm_updated}")
    print(f"  messages deleted: {deleted}")
    print(f"  remaining messages: {_count(conn, 'SELECT COUNT(*) FROM messages')}")

    if args.vacuum:
        print()
        print("Running VACUUM (this can take a moment on a large DB)...")
        # VACUUM cannot run inside a transaction; the connect() helper sets
        # isolation_level=None already, but be explicit.
        conn.isolation_level = None
        conn.execute("VACUUM")
        print("  done.")
    else:
        print()
        print("Skipping VACUUM. Pass --vacuum to reclaim disk space "
              "(requires brief DB lock).")

    return 0


if __name__ == "__main__":
    sys.exit(main())
