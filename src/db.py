import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).resolve().parent / "schema.sql"


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    # Pin the auto-checkpoint threshold explicitly. SQLite's default of
    # 1000 pages happens to be what we want, but the default has changed
    # in past versions and isn't guaranteed across the SQLite shipped with
    # different Python installs. Without this, WAL can grow to whatever
    # the new default is between checkpoints, which on a busy 24h day
    # with the EA's 15s heartbeat + 1Hz polling adds up.
    conn.execute("PRAGMA wal_autocheckpoint=1000")
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
    _migrate_actions_add_modify_tps(conn)
    _migrate_positions_add_pnl(conn)
    _migrate_actions_add_instant_types(conn)
    _migrate_positions_add_naked(conn)
    _migrate_actions_add_cancel_pending(conn)
    _migrate_seed_settings_defaults(conn)
    _migrate_env_to_settings(conn)
    _migrate_create_unmatched_messages(conn)
    # v2 multi-channel scaffolding (see docs/plans/2026-05-23-multi-channel-routing.md
    # Step 2). Columns are nullable; rows from before v2 keep NULL until
    # backfilled by the API process via backfill_source_channel_id().
    _migrate_messages_add_source_channel(conn)
    _migrate_actions_add_source_channel_route(conn)
    _migrate_create_bot_outbox(conn)
    _migrate_messages_add_reply_to(conn)
    _migrate_messages_add_pipeline_columns(conn)


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


def _migrate_positions_add_pnl(conn: sqlite3.Connection) -> None:
    """Add exit_price + realized_pnl to positions for the score calibration
    script (Step 3 of AI_EVALUATOR_ROADMAP). Both nullable; pre-migration
    closed rows stay NULL because we don't have the deal history needed
    to backfill them. The calibration script ignores NULL rows — it only
    buckets trades closed AFTER the EA upgrade that started populating
    these columns.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "exit_price" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN exit_price REAL")
    if "realized_pnl" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN realized_pnl REAL")


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
    sql_text = sql_row["sql"] if sql_row else ""
    # Phase-2+ DBs may legitimately lack 'watching' in the stored sql:
    #   - schema.sql ships with the wide action_type CHECK and a status
    #     CHECK that does NOT include 'watching' (the synthetic-pending
    #     path was removed; see _migrate_actions_drop_watch_columns).
    #   - existing prod DBs that went through drop_watch_columns end up
    #     here too.
    # The rebuild below would WRONGLY narrow action_type back to the
    # original 5 values, corrupting any row with MOVE_SL_BE /
    # CLOSE_PARTIAL / etc. The else-branch ADD COLUMN paths would also
    # re-add the watch_json/expires_at columns that drop_watch_columns
    # just removed. Detect the post-Phase-2 state and bail out entirely
    # — drop_watch_columns is the canonical place to manage watch
    # infrastructure on those DBs.
    if "'MOVE_SL_BE'" in sql_text:
        return
    check_has_watching = "'watching'" in sql_text

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
    """Add fingerprint column + partial composite index to existing databases.

    The original index was unconstrained — `CREATE INDEX ... ON
    actions(fingerprint)` — and covered every row including the majority
    where fingerprint IS NULL (management types, ALERTs). The new partial
    composite index `(fingerprint, created_at) WHERE fingerprint IS NOT NULL`
    makes _has_recent_duplicate_open a single range scan instead of a
    fingerprint-only scan + date filter, and is much smaller on disk.

    DROP+CREATE is safe to re-run (idempotent under IF NOT EXISTS).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
    if "fingerprint" not in cols:
        conn.execute("ALTER TABLE actions ADD COLUMN fingerprint TEXT")
    # Replace any old non-partial index from prior versions of this
    # migration before recreating with the partial composite shape.
    conn.execute("DROP INDEX IF EXISTS idx_actions_fingerprint")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_fingerprint "
        "ON actions(fingerprint, created_at) "
        "WHERE fingerprint IS NOT NULL"
    )


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


def _migrate_actions_add_modify_tps(conn: sqlite3.Connection) -> None:
    """Expand actions.action_type CHECK to allow the Phase-3 MODIFY_TPS type.

    Why: the orchestrator inserts MODIFY_TPS once the AI prompt learns to
    emit it for the "new OPEN signal arrives while position open" rule
    (modify SL+TPs in place when not closing+reopening). Pre-Phase-3
    databases reject MODIFY_TPS via the old CHECK, so we rebuild the
    table preserving every row when the new type is absent. Same pattern
    as _migrate_actions_add_phase2_types.
    """
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'"
    ).fetchone()
    if not sql_row or "'MODIFY_TPS'" in sql_row["sql"]:
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
            "                    'REOPEN_LAST','REINFORCE','TIGHTEN_SL','MODIFY_TPS'"
            "                  )),"
            "  payload_json    TEXT NOT NULL,"
            "  status          TEXT NOT NULL DEFAULT 'pending'"
            "                  CHECK(status IN ('pending','cancelled','sent','claimed','executed','failed','rejected')),"
            "  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  notified_at     DATETIME,"
            "  execute_after   DATETIME,"
            "  claimed_at      DATETIME,"
            "  executed_at     DATETIME,"
            "  ea_response     TEXT,"
            "  fingerprint     TEXT"
            ");"
            "INSERT INTO actions_new(id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint) "
            "SELECT id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint "
            "FROM actions;"
            "DROP TABLE actions;"
            "ALTER TABLE actions_new RENAME TO actions;"
            "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);"
            "CREATE INDEX IF NOT EXISTS idx_actions_fingerprint "
            "  ON actions(fingerprint, created_at) "
            "  WHERE fingerprint IS NOT NULL;"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_actions_add_instant_types(conn: sqlite3.Connection) -> None:
    """Widen actions.action_type CHECK to allow OPEN_INSTANT and ATTACH_SIGNAL.

    The directional-command-first flow (Phase 4): channel posts "اشتري الذهب"
    or "بيع الذهب" → AI emits OPEN_INSTANT → EA opens at market with an
    emergency SL → structured signal arrives within ~3 min → AI emits
    ATTACH_SIGNAL → EA modifies SL/TPs and registers the staged plan.
    Pre-Phase-4 databases reject these via the older CHECK, so we rebuild
    the table preserving every row when the new types are absent. Same
    pattern as _migrate_actions_add_modify_tps.
    """
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'"
    ).fetchone()
    if not sql_row or "'OPEN_INSTANT'" in sql_row["sql"]:
        return
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
            "                    'REOPEN_LAST','REINFORCE','TIGHTEN_SL','MODIFY_TPS',"
            "                    'OPEN_INSTANT','ATTACH_SIGNAL'"
            "                  )),"
            "  payload_json    TEXT NOT NULL,"
            "  status          TEXT NOT NULL DEFAULT 'pending'"
            "                  CHECK(status IN ('pending','cancelled','sent','claimed','executed','failed','rejected')),"
            "  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,"
            "  notified_at     DATETIME,"
            "  execute_after   DATETIME,"
            "  claimed_at      DATETIME,"
            "  executed_at     DATETIME,"
            "  ea_response     TEXT,"
            "  fingerprint     TEXT"
            ");"
            "INSERT INTO actions_new(id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint) "
            "SELECT id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint "
            "FROM actions;"
            "DROP TABLE actions;"
            "ALTER TABLE actions_new RENAME TO actions;"
            "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);"
            "CREATE INDEX IF NOT EXISTS idx_actions_fingerprint "
            "  ON actions(fingerprint, created_at) "
            "  WHERE fingerprint IS NOT NULL;"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_positions_add_naked(conn: sqlite3.Connection) -> None:
    """Add is_naked + naked_opened_at to positions for OPEN_INSTANT flow.

    A naked row is one opened via OPEN_INSTANT with only an emergency SL.
    ATTACH_SIGNAL or the EA fallback timer clears is_naked when the position
    becomes a fully-managed trade. Pre-migration rows backfill to is_naked=0
    (legacy positions were never naked).
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(positions)").fetchall()}
    if "is_naked" not in cols:
        conn.execute(
            "ALTER TABLE positions ADD COLUMN "
            "is_naked INTEGER NOT NULL DEFAULT 0"
        )
    if "naked_opened_at" not in cols:
        conn.execute("ALTER TABLE positions ADD COLUMN naked_opened_at DATETIME")


def _migrate_actions_add_cancel_pending(conn: sqlite3.Connection) -> None:
    """Widen actions checks for the pending-limit flow:
      - action_type adds CANCEL_PENDING
      - status re-adds 'watching' (it was present in earlier migrations
        but got dropped by _migrate_actions_add_modify_tps and
        _migrate_actions_add_instant_types when they rebuilt the table)

    Phase-5 SMC channel uses pending limit orders. The EA POSTs the OPEN
    action's result as status='watching' while a broker-side BuyLimit /
    SellLimit waits to fill. CANCEL_PENDING flips watching OPENs to
    'rejected' with reason 'cancelled_by_channel' (server-side, in the
    orchestrator) and the EA's ManagePendingOrders() picks up the
    rejection on its next OnTimer sweep and OrderDelete's the broker
    pending. Same table-rebuild pattern as the other action-type widenings.
    """
    sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='actions'"
    ).fetchone()
    if not sql_row:
        return
    sql_text = sql_row["sql"] or ""
    if "'CANCEL_PENDING'" in sql_text and "'watching'" in sql_text:
        return  # already migrated
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
            "                    'REOPEN_LAST','REINFORCE','TIGHTEN_SL','MODIFY_TPS',"
            "                    'OPEN_INSTANT','ATTACH_SIGNAL','CANCEL_PENDING'"
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
            "  fingerprint     TEXT"
            ");"
            "INSERT INTO actions_new(id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint) "
            "SELECT id, source_msg_id, action_type, payload_json, status,"
            "  created_at, notified_at, execute_after, claimed_at, executed_at, ea_response,"
            "  fingerprint "
            "FROM actions;"
            "DROP TABLE actions;"
            "ALTER TABLE actions_new RENAME TO actions;"
            "CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);"
            "CREATE INDEX IF NOT EXISTS idx_actions_fingerprint "
            "  ON actions(fingerprint, created_at) "
            "  WHERE fingerprint IS NOT NULL;"
            "COMMIT;"
        )
    finally:
        conn.execute("PRAGMA foreign_keys=ON")


def _migrate_create_unmatched_messages(conn: sqlite3.Connection) -> None:
    """Queue of live Telegram messages the trigger matcher missed.

    Populated by the orchestrator on the Sonnet-fallback path when the AI
    emits at least one deterministic-emittable action type — i.e. the
    message *could* legitimately become a trigger, but no operator-curated
    phrase caught it yet. The Triggers tab's "Unmatched" pane reads from
    this table to offer one-click promotion into a real trigger entry.

    `norm_text` is UNIQUE so re-quoted messages don't duplicate — a single
    canonical row per distinct normalized payload. `suggested_action_type`
    is Sonnet's best guess (the first emittable type in its emission list)
    and pre-fills the action_type bucket when the operator promotes.

    The table is queue-shaped: promotion and dismissal both DELETE the
    row, so the table itself never grows beyond pending entries. The
    insert path prunes oldest rows when count exceeds the cap.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS unmatched_messages ("
        "  id                    INTEGER PRIMARY KEY,"
        "  text                  TEXT NOT NULL,"
        "  norm_text             TEXT NOT NULL UNIQUE,"
        "  suggested_action_type TEXT,"
        "  source_msg_id         INTEGER,"
        "  created_at            DATETIME DEFAULT CURRENT_TIMESTAMP"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_unmatched_created "
        "ON unmatched_messages(created_at)"
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


def _migrate_seed_settings_defaults(conn: sqlite3.Connection) -> None:
    """Seed non-critical default values into settings if missing.

    Critical keys (tg_api_id, ..., ai_provider) get NO default — the
    setup wizard fills them. Everything else has a sensible default
    inserted via INSERT OR IGNORE so existing values aren't clobbered.
    """
    from src.db_settings import DEFAULT_SETTINGS
    for key, val in DEFAULT_SETTINGS.items():
        conn.execute(
            "INSERT OR IGNORE INTO settings(key, value) VALUES(?, ?)",
            (key, val),
        )


def _migrate_env_to_settings(conn: sqlite3.Connection) -> None:
    """One-time copy of legacy `.env` keys into settings table.

    Runs only when the settings table is missing critical keys AND a
    `.env` file is reachable in BASE_DIR. Encrypts secrets via DPAPI.
    Renames the .env to .env.migrated.bak so subsequent runs skip.
    """
    cur = conn.execute(
        "SELECT COUNT(*) AS n FROM settings WHERE key IN "
        "('tg_api_id','tg_api_hash','tg_phone','tg_watched_chat_id','tg_bot_token','ai_provider')"
    ).fetchone()
    if cur and (cur["n"] if hasattr(cur, "keys") else cur[0]) >= 6:
        return
    env_path = SCHEMA_PATH.parent.parent / ".env"
    if not env_path.exists():
        return
    pairs: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        pairs[k.strip()] = v.strip().strip('"').strip("'")
    if not pairs:
        return
    mapping = {
        "TG_API_ID": "tg_api_id",
        "TG_API_HASH": "tg_api_hash",
        "TG_PHONE": "tg_phone",
        "TG_SESSION_NAME": "tg_session_name",
        "TG_WATCHED_CHAT_ID": "tg_watched_chat_id",
        "TG_BOT_TOKEN": "tg_bot_token",
        "TG_BOT_OWNER_USER_ID": "tg_bot_owner_user_id",
        "ANTHROPIC_API_KEY": "anthropic_api_key",
        "ANTHROPIC_MODEL": "anthropic_model",
        "OPENAI_API_KEY": "openai_api_key",
        "OPENAI_MODEL": "openai_model",
        "OPENAI_TRIAGE_MODEL": "openai_triage_model",
        "AI_PROVIDER": "ai_provider",
        "AI_TRIAGE_MODEL": "ai_triage_model",
        "AI_TRIAGE_ENABLED": "ai_triage_enabled",
        "AI_THINKING_ENABLED": "ai_thinking_enabled",
        "AI_THINKING_BUDGET_TOKENS": "ai_thinking_budget_tokens",
        "API_HOST": "api_host",
        "API_PORT": "api_port",
        "EA_SHARED_TOKEN": "ea_shared_token",
        "CHANNEL_PROFILE": "channel_profile",
        "FINGERPRINT_BAND_PRICE": "fingerprint_band_price",
        "FINGERPRINT_WINDOW_HOURS": "fingerprint_window_hours",
        "BACKFILL_MAX_AGE_MIN": "backfill_max_age_min",
        "SIGNAL_MEMORY_ENABLED": "signal_memory_enabled",
        "SIGNAL_MEMORY_MAX_ENTRIES": "signal_memory_max_entries",
        "SIGNAL_MEMORY_MAX_AGE_HOURS": "signal_memory_max_age_hours",
    }
    from src.db_settings import SECRET_KEYS
    from src.secret_box import encrypt
    for env_key, settings_key in mapping.items():
        if env_key not in pairs or pairs[env_key] == "":
            continue
        raw_value = pairs[env_key]
        if settings_key in SECRET_KEYS:
            try:
                value = encrypt(raw_value)
            except OSError:
                continue
        else:
            value = raw_value
        conn.execute(
            "INSERT INTO settings(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (settings_key, value),
        )
    # NOTE: .env is left in place after migration. It's now a stale reference
    # only — config.py no longer reads it at runtime. Subsequent runs detect
    # already-migrated state via the missing-critical-keys check above and skip.


# ---------------------------------------------------------------------------
# v2 multi-channel scaffolding (see docs/plans/2026-05-23-multi-channel-routing.md)
#
# These columns and table support routing one DB to N channels (Step 12,
# deferred) and per-bot notification outboxes (Step 7). For the current
# scope (1:1 channel→destination) the columns hold one value per row;
# they're additive so introducing them now doesn't change any behavior.
# ---------------------------------------------------------------------------


def _migrate_messages_add_source_channel(conn: sqlite3.Connection) -> None:
    """Add `source_channel_id` to messages so each row carries the v2 Channel.id
    it came from. Nullable: rows inserted before v2 stay NULL until backfilled
    via backfill_source_channel_id().

    Idempotent: ALTER is gated on PRAGMA table_info; CREATE INDEX uses IF NOT EXISTS.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "source_channel_id" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN source_channel_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_source_channel "
        "ON messages(source_channel_id) WHERE source_channel_id IS NOT NULL"
    )


def _migrate_actions_add_source_channel_route(conn: sqlite3.Connection) -> None:
    """Add `source_channel_id` and `route_id` to actions.

    These let the notification dispatcher (Step 7) and cost tracker (Step 18,
    deferred) attribute each action to the originating Channel and Route. Both
    nullable: pre-v2 rows stay NULL until backfill, and rows from a 1:1
    destination always carry the same pair.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(actions)").fetchall()}
    if "source_channel_id" not in cols:
        conn.execute("ALTER TABLE actions ADD COLUMN source_channel_id TEXT")
    if "route_id" not in cols:
        conn.execute("ALTER TABLE actions ADD COLUMN route_id TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_source_channel "
        "ON actions(source_channel_id) WHERE source_channel_id IS NOT NULL"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_actions_route "
        "ON actions(route_id) WHERE route_id IS NOT NULL"
    )


def _migrate_create_bot_outbox(conn: sqlite3.Connection) -> None:
    """Per-bot notification queue. Step 7 (notification dispatcher) writes into
    it; Step 7's bot_outbox_tailer in each Bot process polls and DMs the operator.

    `delivered_at IS NULL` partial index keeps the hot-path scan (undelivered
    events for one bot) tiny even as the table grows. `event_payload` is JSON;
    keeping it opaque here means new event types don't need schema migrations.
    """
    conn.execute(
        "CREATE TABLE IF NOT EXISTS bot_outbox ("
        "  id                INTEGER PRIMARY KEY,"
        "  bot_id            TEXT NOT NULL,"
        "  event_type        TEXT NOT NULL,"
        "  event_payload     TEXT NOT NULL,"
        "  source_channel_id TEXT,"
        "  route_id          TEXT,"
        "  action_id         INTEGER REFERENCES actions(id),"
        "  created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,"
        "  delivered_at      DATETIME"
        ")"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_bot_outbox_undelivered "
        "ON bot_outbox(bot_id, created_at) WHERE delivered_at IS NULL"
    )


def backfill_source_channel_id(conn: sqlite3.Connection, channel_id: str) -> tuple[int, int]:
    """Set messages.source_channel_id and actions.source_channel_id on rows
    where it's currently NULL.

    Intended for callers that know the v2 Channel mapping — typically the API
    process at startup, which looks at the v2 config, finds the single Route
    pointing at this destination, and passes its channel_id here. With 1:1
    routing (current scope) the mapping is unambiguous.

    Returns (messages_updated, actions_updated) so the caller can log a
    one-line summary at startup.

    Note: route_id backfill is intentionally NOT done here. Once Routes exist
    as logical entities, a separate `backfill_route_id` call (with the
    route_id parameter resolved by the caller) handles that. Keeping the two
    backfills separate means we don't conflate "which channel sent this" with
    "which route picked it up" — they CAN differ in future scopes (mirror /
    aggregate) where one route is one of many for a channel.
    """
    msg_result = conn.execute(
        "UPDATE messages SET source_channel_id=? WHERE source_channel_id IS NULL",
        (channel_id,),
    )
    act_result = conn.execute(
        "UPDATE actions SET source_channel_id=? WHERE source_channel_id IS NULL",
        (channel_id,),
    )
    return (msg_result.rowcount or 0, act_result.rowcount or 0)


def backfill_route_id(conn: sqlite3.Connection, route_id: str) -> int:
    """Set actions.route_id on rows where it's currently NULL.

    Same caller pattern as backfill_source_channel_id; separate function
    because the channel and route are independent dimensions (see that
    function's docstring).

    Returns the number of action rows updated.
    """
    result = conn.execute(
        "UPDATE actions SET route_id=? WHERE route_id IS NULL",
        (route_id,),
    )
    return result.rowcount or 0


def _migrate_messages_add_pipeline_columns(conn: sqlite3.Connection) -> None:
    """Add per-message pipeline-decision tracking columns.

    The Pipeline view (src/gui/views/pipeline_view.py) needs to render
    "this message exited the pipeline at stage X with outcome Y" plus a
    radial histogram of per-bucket counts. Reading logs/ai_calls.jsonl
    on every refresh doesn't scale past a few thousand messages (rotated
    files, no index, full scan), so the orchestrator now writes the
    terminal stage decision directly onto the messages row.

    Columns are all nullable: pre-migration rows stay NULL and render as
    "unknown" in the GUI. The index on (decided_stage, received_at DESC)
    powers both the per-bucket COUNT and the time-windowed listing.
    Idempotent: ALTERs are gated on PRAGMA table_info, index uses IF NOT
    EXISTS.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "decided_stage" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN decided_stage TEXT")
    if "decided_outcome" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN decided_outcome TEXT")
    if "decided_at" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN decided_at TIMESTAMP")
    if "pipeline_meta_json" not in cols:
        conn.execute("ALTER TABLE messages ADD COLUMN pipeline_meta_json TEXT")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_messages_decided_stage "
        "ON messages(decided_stage, received_at DESC)"
    )


def _migrate_messages_add_reply_to(conn: sqlite3.Connection) -> None:
    """Add `reply_to_tg_message_id` to messages.

    Telegram replies carry a pointer to the parent message they answer
    (Telethon: ``event.message.reply_to_msg_id``). The listener writes
    that here so state_summary can prepend the parent's text to the
    SYSTEM STATE block when the AI evaluates a reply — without it the
    AI sees "cancel that order" with no antecedent and falls back to
    ALERT/context.

    Nullable: pre-2026-05-26 rows + non-reply messages stay NULL.
    Idempotent: ALTER is gated on PRAGMA table_info.
    """
    cols = {row["name"] for row in conn.execute("PRAGMA table_info(messages)").fetchall()}
    if "reply_to_tg_message_id" not in cols:
        conn.execute(
            "ALTER TABLE messages ADD COLUMN reply_to_tg_message_id INTEGER"
        )
