import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA_PATH.read_text())
    _migrate_actions_for_claim(conn)
    _migrate_actions_add_fingerprint(conn)
    _migrate_signal_memory_add_chat_id(conn)
    _migrate_actions_for_watching(conn)
    _migrate_positions_state(conn)
    _migrate_actions_add_phase2_types(conn)
    _migrate_actions_drop_watch_columns(conn)


def _migrate_actions_add_phase2_types(conn: sqlite3.Connection) -> None:
    """Expand actions.action_type CHECK to allow Phase-2 management types.

    Why: orchestrator inserts MOVE_SL_BE / MOVE_SL / CLOSE_PARTIAL /
    CLOSE_FULL / REOPEN_LAST / REINFORCE / TIGHTEN_SL once the AI prompt
    learns to emit them. Pre-Phase-2 databases reject these via the old
    CHECK, so we rebuild the table preserving every row when the new
    types are absent. Same pattern as _migrate_actions_for_watching.
    """
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'"
    ).fetchone()
    if not sql_row or "'MOVE_SL_BE'" in sql_row["sql"]:
        return  # already migrated (or no actions table yet)
    conn.execute(
        "UPDATE actions SET source_msg_id = NULL "
        "WHERE source_msg_id IS NOT NULL "
        "  AND source_msg_id NOT IN (SELECT id FROM messages)"
    )
    conn.execute("PRAGMA foreign_keys=OFF")
    try:
        conn.executescript(
            "BEGIN;"
            "CREATE TABLE actions_new ("
            "  id              INTEGER PRIMARY KEY,"
            "  source_msg_id   INTEGER REFERENCES messages(id),"
            "  action_type     TEXT NOT NULL CHECK(action_type IN ("
            "                    'OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT',"
            "                    'MOVE_SL_BE','MOVE_SL','CLOSE_PARTIAL','CLOSE_FULL',"
            "                    'REOPEN_LAST','REINFORCE','TIGHTEN_SL'"
            "                  )),"
            "  payload_json    TEXT NOT NULL,"
            "  status          TEXT NOT NULL DEFAULT 'pending'"
            "                  CHECK(status IN ('pending','cancelled','sent','claimed','watching','executed','failed','rejected')),"
            "  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  notified_at     DATETIME,"
            "  execute_after   DATETIME,"
            "  claimed_at      DATETIME,"
            "  executed_at     DATETIME,"
            "  ea_response     TEXT,"
            "  fingerprint     TEXT,"
            "  watch_json      TEXT,"
            "  expires_at      DATETIME"
            ");"
            "INSERT INTO actions_new(id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint, watch_json, expires_at) "
            "SELECT id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint, watch_json, expires_at "
            "FROM actions;"
            "DROP TABLE actions;"
            "ALTER TABLE actions_new RENAME TO actions;"
            "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);"
            "CREATE INDEX IF NOT EXISTS idx_actions_fingerprint ON actions(fingerprint);"
            "CREATE INDEX IF NOT EXISTS idx_actions_watching_expires "
            "  ON actions(expires_at) WHERE status='watching';"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_positions_state(conn: sqlite3.Connection) -> None:
    """Add original_volume / partial_close_count / sl_moved_at to positions.

    Why: the AI prompt needs to know what's already been done to the open
    position so reminder messages (e.g. "متاح حجز الارباح لو ما لحقت") do not
    re-trigger a partial close that already happened. See state_summary.py
    for how these fields surface to the model.

    Backfill: existing open rows get original_volume = current volume so the
    "started at" reading isn't NULL for trades opened before the migration.
    partial_close_count defaults to 0 (we don't know history). sl_moved_at
    stays NULL — there's no way to tell after the fact whether SL was moved,
    and treating "unknown" as "not moved" is safer (the AI will be willing
    to emit MOVE_SL_BE, which is itself idempotent on current_sl == entry).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "original_volume" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN original_volume REAL")
        conn.execute(
            "UPDATE positions SET original_volume = volume "
            "WHERE original_volume IS NULL"
        )
    if "partial_close_count" not in cols:
        conn.execute(
            "ALTER TABLE positions ADD COLUMN "
            "partial_close_count INTEGER NOT NULL DEFAULT 0"
        )
    if "sl_moved_at" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN sl_moved_at DATETIME")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_positions_closed_recent "
        "ON positions(symbol, status, closed_at)"
    )


def _migrate_actions_for_watching(conn: sqlite3.Connection) -> None:
    """Add 'watching' status + watch_json/expires_at columns for synthetic pending limits.

    Why: synthetic-pending signals must survive EA/terminal restarts and stay
    visible to the bot for cancellation. DB is the source of truth; the EA
    rebuilds its watchlist from GET /actions?status=watching on every OnInit.
    SQLite can't ALTER a CHECK constraint, so when 'watching' is absent from
    the stored sql, we rebuild the table preserving every row.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'"
    ).fetchone()
    check_has_watching = bool(sql_row) and "'watching'" in sql_row["sql"]

    if not check_has_watching:
        # NULL out orphaned source_msg_id before the rebuild so the INSERT
        # can't fail on FK enforcement. PRAGMA foreign_keys toggling is
        # unreliable across executescript's implicit transaction boundaries,
        # so sanitize the data itself instead.
        conn.execute(
            "UPDATE actions SET source_msg_id = NULL "
            "WHERE source_msg_id IS NOT NULL "
            "  AND source_msg_id NOT IN (SELECT id FROM messages)"
        )
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript(
                "BEGIN;"
                "CREATE TABLE actions_new ("
                "  id              INTEGER PRIMARY KEY,"
                "  source_msg_id   INTEGER REFERENCES messages(id),"
                "  action_type     TEXT NOT NULL CHECK(action_type IN ('OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT')),"
                "  payload_json    TEXT NOT NULL,"
                "  status          TEXT NOT NULL DEFAULT 'pending'"
                "                  CHECK(status IN ('pending','cancelled','sent','claimed','watching','executed','failed','rejected')),"
                "  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "  notified_at     DATETIME,"
                "  execute_after   DATETIME,"
                "  claimed_at      DATETIME,"
                "  executed_at     DATETIME,"
                "  ea_response     TEXT,"
                "  fingerprint     TEXT,"
                "  watch_json      TEXT,"
                "  expires_at      DATETIME"
                ");"
                "INSERT INTO actions_new(id, source_msg_id, action_type, payload_json, status,"
                "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response, fingerprint) "
                "SELECT id, source_msg_id, action_type, payload_json, status,"
                "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response, fingerprint "
                "FROM actions;"
                "DROP TABLE actions;"
                "ALTER TABLE actions_new RENAME TO actions;"
                "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);"
                "CREATE INDEX IF NOT EXISTS idx_actions_fingerprint ON actions(fingerprint);"
                "COMMIT;"
            )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")
    else:
        if "watch_json" not in cols:
            conn.execute("ALTER TABLE actions ADD COLUMN watch_json TEXT")
        if "expires_at" not in cols:
            conn.execute("ALTER TABLE actions ADD COLUMN expires_at DATETIME")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_watching_expires "
        "ON actions(expires_at) WHERE status='watching'"
    )


def _migrate_signal_memory_add_chat_id(conn: sqlite3.Connection) -> None:
    """Add chat_id column to signal_memory + scoped index.

    Why: deliberation memory was global across chats, so a resolved OPEN in
    one channel would clear context in another. Existing rows backfill to 0,
    which is outside real Telegram chat IDs and isolates legacy entries.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(signal_memory)").fetchall()}
    if "chat_id" not in cols:
        conn.execute("ALTER TABLE signal_memory ADD COLUMN chat_id INTEGER NOT NULL DEFAULT 0")
    conn.execute("DROP INDEX IF EXISTS idx_signal_memory_active")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_signal_memory_active "
        "ON signal_memory(chat_id, cleared_at, created_at)"
    )


def _migrate_actions_add_fingerprint(conn: sqlite3.Connection) -> None:
    """Add fingerprint column + index to existing databases."""
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE actions ADD COLUMN fingerprint TEXT")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_actions_fingerprint ON actions(fingerprint)")


def _migrate_actions_for_claim(conn: sqlite3.Connection) -> None:
    """Make existing prod databases tolerate the 'claimed' status and claimed_at column.

    Why: schema.sql is only applied on fresh DBs (CREATE TABLE IF NOT EXISTS).
    Existing databases predate the claim/execute/confirm protocol and would reject
    'claimed' from the old CHECK constraint, plus lack the claimed_at column.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
    if "claimed_at" not in cols:
        conn.execute("ALTER TABLE actions ADD COLUMN claimed_at DATETIME")

    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'"
    ).fetchone()
    if sql_row and "'claimed'" not in sql_row["sql"]:
        # See _migrate_actions_for_watching: sanitize orphaned source_msg_id
        # before the rebuild so the INSERT can't violate FK.
        conn.execute(
            "UPDATE actions SET source_msg_id = NULL "
            "WHERE source_msg_id IS NOT NULL "
            "  AND source_msg_id NOT IN (SELECT id FROM messages)"
        )
        conn.execute("PRAGMA foreign_keys=OFF")
        try:
            conn.executescript(
                "BEGIN;"
                "CREATE TABLE actions_new ("
                "  id              INTEGER PRIMARY KEY,"
                "  source_msg_id   INTEGER REFERENCES messages(id),"
                "  action_type     TEXT NOT NULL CHECK(action_type IN ('OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT')),"
                "  payload_json    TEXT NOT NULL,"
                "  status          TEXT NOT NULL DEFAULT 'pending'"
                "                  CHECK(status IN ('pending','cancelled','sent','claimed','executed','failed','rejected')),"
                "  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
                "  notified_at     DATETIME,"
                "  execute_after   DATETIME,"
                "  claimed_at      DATETIME,"
                "  executed_at     DATETIME,"
                "  ea_response     TEXT"
                ");"
                "INSERT INTO actions_new(id, source_msg_id, action_type, payload_json, status,"
                "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response) "
                "SELECT id, source_msg_id, action_type, payload_json, status,"
                "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response "
                "FROM actions;"
                "DROP TABLE actions;"
                "ALTER TABLE actions_new RENAME TO actions;"
                "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);"
                "COMMIT;"
            )
        finally:
            conn.execute("PRAGMA foreign_keys=ON")


def _migrate_actions_drop_watch_columns(conn: sqlite3.Connection) -> None:
    """Drop the synthetic-pending watch infrastructure from existing DBs.

    Why: the watch path (out-of-zone signals POSTed as status='watching' with
    a watch_json blob and expires_at) was removed in favor of explicit
    rejection at the EA. This migration converts any leftover 'watching' rows
    to 'rejected', drops the partial index, and drops the watch_json /
    expires_at columns. Idempotent: safe to re-run on a fresh DB (no-op).

    Requires SQLite >= 3.35 for ALTER TABLE DROP COLUMN. The Python stdlib
    sqlite3 in 3.11+ ships with a recent-enough libsqlite; older runtimes
    will silently keep the columns (the catch below swallows the error).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
    has_watch_json = "watch_json" in cols
    has_expires_at = "expires_at" in cols
    if not (has_watch_json or has_expires_at):
        return  # already cleaned up

    conn.execute(
        "UPDATE actions SET status='rejected', ea_response='watch_path_removed' "
        "WHERE status='watching'"
    )
    conn.execute("DROP INDEX IF EXISTS idx_actions_watching_expires")
    if has_watch_json:
        try:
            conn.execute("ALTER TABLE actions DROP COLUMN watch_json")
        except sqlite3.OperationalError:
            pass
    if has_expires_at:
        try:
            conn.execute("ALTER TABLE actions DROP COLUMN expires_at")
        except sqlite3.OperationalError:
            pass


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
