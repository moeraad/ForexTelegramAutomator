PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
  id              INTEGER PRIMARY KEY,
  tg_message_id   INTEGER NOT NULL,
  chat_id         INTEGER NOT NULL,
  sender          TEXT,
  text            TEXT NOT NULL,
  received_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  is_backfill     INTEGER DEFAULT 0,
  UNIQUE(chat_id, tg_message_id)
);

CREATE TABLE IF NOT EXISTS actions (
  id              INTEGER PRIMARY KEY,
  source_msg_id   INTEGER REFERENCES messages(id),
  action_type     TEXT NOT NULL CHECK(action_type IN ('OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT')),
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','cancelled','sent','claimed','watching','executed','failed','rejected')),
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  notified_at     DATETIME,
  execute_after   DATETIME,
  claimed_at      DATETIME,
  executed_at     DATETIME,
  ea_response     TEXT,
  fingerprint     TEXT,
  watch_json      TEXT,
  expires_at      DATETIME
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
-- Partial index on expires_at is created by _migrate_actions_for_watching in
-- db.py, not here: on existing pre-watching DBs the column doesn't exist until
-- the migration adds it, so a CREATE INDEX in schema.sql would crash before
-- the migration runs.

CREATE TABLE IF NOT EXISTS positions (
  id              INTEGER PRIMARY KEY,
  action_id       INTEGER REFERENCES actions(id),
  mt5_ticket      INTEGER UNIQUE,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  volume          REAL NOT NULL,
  entry_price     REAL,
  sl              REAL,
  tp              REAL,
  status          TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
  opened_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at       DATETIME,
  close_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS signal_memory (
  id              INTEGER PRIMARY KEY,
  message_id      INTEGER REFERENCES messages(id),
  chat_id         INTEGER NOT NULL,
  category        TEXT NOT NULL CHECK(category IN ('context','signal','partial_signal')),
  summary         TEXT NOT NULL,
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  cleared_at      DATETIME
);
CREATE INDEX IF NOT EXISTS idx_signal_memory_active
  ON signal_memory(chat_id, cleared_at, created_at);

CREATE TABLE IF NOT EXISTS settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);

INSERT OR IGNORE INTO settings(key, value) VALUES
  ('kill_switch', 'off'),
  ('auto_execute_delay_sec', '0'),
  ('last_seen_tg_msg_id', '0');
