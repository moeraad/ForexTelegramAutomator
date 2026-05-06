"""Reset copytrades.db to a fresh, empty state.

Deletes the SQLite database file (and its WAL/SHM sidecars) and recreates
every table per src/schema.sql by running the same init_schema() the
long-running processes use at startup.

Usage:
    .venv\\Scripts\\python.exe -m scripts.reset_db          # interactive
    .venv\\Scripts\\python.exe -m scripts.reset_db --yes    # skip prompt

Safety:
    - Refuses to run if the DB file looks busy (another process holds an
      exclusive lock). Stop the API/bot/listener first.
    - Prints a row-count summary BEFORE deleting so a stray run on a real
      session can be aborted.
    - Does NOT touch the logs/ directory. Logs rotate independently and
      often hold forensic context worth keeping across resets.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def _row_counts(db_path: Path) -> dict[str, int]:
    """Best-effort row counts for the user-visible tables. Returns an
    empty dict if the file doesn't exist or is unreadable. -1 indicates
    a table that doesn't exist on a partially-initialized DB."""
    if not db_path.exists():
        return {}
    counts: dict[str, int] = {}
    try:
        conn = sqlite3.connect(str(db_path))
        for table in ("messages", "actions", "positions", "settings", "signal_memory"):
            try:
                row = conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()
                counts[table] = row[0] if row else 0
            except sqlite3.OperationalError:
                counts[table] = -1
        conn.close()
    except sqlite3.DatabaseError:
        return {}
    return counts


def _looks_busy(db_path: Path) -> str | None:
    """Return a human-readable reason if the DB looks like it's being used.
    Returns None if the file is safe to delete. WAL sidecars present is
    normal for WAL-mode SQLite — by themselves they do not indicate a
    live process. The reliable check is to try acquiring an exclusive
    lock; if another process holds the file, BEGIN EXCLUSIVE returns
    SQLITE_BUSY."""
    if not db_path.exists():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=0.5)
        conn.execute("BEGIN EXCLUSIVE")
        conn.execute("ROLLBACK")
        conn.close()
        return None
    except sqlite3.OperationalError as e:
        return f"DB appears busy ({e}). Stop the API/bot/listener first."
    except sqlite3.DatabaseError as e:
        return f"file is not a readable SQLite DB ({e}); refusing to delete."


def _delete_db_files(db_path: Path) -> list[Path]:
    """Remove the main DB file plus its WAL/SHM sidecars. Returns the list
    of paths that were actually deleted."""
    deleted: list[Path] = []
    candidates = [
        db_path,
        db_path.with_name(db_path.name + "-wal"),
        db_path.with_name(db_path.name + "-shm"),
    ]
    for target in candidates:
        if target.exists():
            target.unlink()
            deleted.append(target)
    return deleted


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Reset copytrades.db to a fresh, empty state.")
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the interactive confirmation prompt.")
    args = parser.parse_args(argv)

    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from src import config
    from src.db import connect, init_schema

    db_path = Path(config.DB_PATH).resolve()
    print(f"Target DB: {db_path}")

    busy = _looks_busy(db_path)
    if busy:
        print(f"REFUSING: {busy}", file=sys.stderr)
        return 2

    counts = _row_counts(db_path)
    if counts:
        print("Current row counts:")
        for table, n in counts.items():
            tag = "(missing)" if n < 0 else f"{n} rows"
            print(f"  {table:<16} {tag}")
    else:
        print("(DB file does not exist yet -- will be created fresh.)")

    if not args.yes:
        print(
            f"\nThis will DELETE {db_path.name} (and its -wal/-shm sidecars) "
            f"and recreate empty tables.\n"
            f"All messages, actions, positions, and settings will be lost.\n",
            file=sys.stderr,
        )
        try:
            answer = input("Type 'reset' to proceed: ").strip().lower()
        except EOFError:
            print("aborted (no input)", file=sys.stderr)
            return 1
        if answer != "reset":
            print("aborted", file=sys.stderr)
            return 1

    deleted = _delete_db_files(db_path)
    for p in deleted:
        print(f"  removed {p.name}")

    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(db_path))
    init_schema(conn)
    conn.close()

    print(f"\nFresh DB created at {db_path}")
    print("All tables empty. Restart the API / bot / listener to begin.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
