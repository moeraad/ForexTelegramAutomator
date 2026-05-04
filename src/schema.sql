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
  action_type     TEXT NOT NULL CHECK(action_type IN (
                    'OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT',
                    'MOVE_SL_BE','MOVE_SL','CLOSE_PARTIAL','CLOSE_FULL',
                    'REOPEN_LAST','REINFORCE','TIGHTEN_SL'
                  )),
  payload_json    TEXT NOT NULL,
  status          TEXT NOT NULL DEFAULT 'pending'
                  CHECK(status IN ('pending','cancelled','sent','claimed','executed','failed','rejected')),
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  notified_at     DATETIME,
  execute_after   DATETIME,
  claimed_at      DATETIME,
  executed_at     DATETIME,
  ea_response     TEXT,
  fingerprint     TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);

CREATE TABLE IF NOT EXISTS positions (
  id                   INTEGER PRIMARY KEY,
  action_id            INTEGER REFERENCES actions(id),
  mt5_ticket           INTEGER UNIQUE,
  symbol               TEXT NOT NULL,
  side                 TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
  volume               REAL NOT NULL,
  -- original_volume: snapshot of `volume` at insert time. Never updated.
  -- Lets the AI prompt distinguish "the trade started at 0.08 and is now
  -- 0.04 because TP1 already partial-closed" from "this is a 0.04-lot trade".
  original_volume      REAL,
  -- partial_close_count: incremented every time /positions/{ticket}/update
  -- receives a smaller `volume`. Drives the idempotency rule: if >0, do not
  -- re-emit CLOSE_PARTIAL on a reminder message.
  partial_close_count  INTEGER NOT NULL DEFAULT 0,
  -- sl_moved_at: set the first time /positions/{ticket}/update changes `sl`.
  -- NULL means "SL is still the original signal SL".
  sl_moved_at          DATETIME,
  entry_price          REAL,
  sl                   REAL,
  tp                   REAL,
  status               TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
  opened_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at            DATETIME,
  close_reason         TEXT
);
CREATE INDEX IF NOT EXISTS idx_positions_status ON positions(status);
-- Hot path: GET /positions/last_closed?symbol=X&within_hours=N
CREATE INDEX IF NOT EXISTS idx_positions_closed_recent
  ON positions(symbol, status, closed_at);

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
