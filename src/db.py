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


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO settings(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )
