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
  action_type     TEXT NOT NULL,
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending',
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  notified_at     DATETIME,
  execute_after   DATETIME,
  executed_at     DATETIME,
  ea_response     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);

CREATE TABLE IF NOT EXISTS positions (
  id              INTEGER PRIMARY KEY,
  action_id       INTEGER REFERENCES actions(id),
  mt5_ticket      INTEGER UNIQUE,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,
  volume          REAL NOT NULL,
  entry_price     REAL,
  sl              REAL,
  tp              REAL,
  status          TEXT NOT NULL DEFAULT 'open',
  opened_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at       DATETIME,
  close_reason    TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);

CREATE TABLE IF NOT EXISTS settings (
  key             TEXT PRIMARY KEY,
  value           TEXT NOT NULL
);

INSERT OR IGNORE INTO settings(key, value) VALUES
  ('kill_switch', 'off'),
  ('auto_execute_delay_sec', '30'),
  ('last_seen_tg_msg_id', '0');
