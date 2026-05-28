PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS messages (
  id                INTEGER PRIMARY KEY,
  tg_message_id     INTEGER NOT NULL,
  chat_id           INTEGER NOT NULL,
  sender            TEXT,
  text              TEXT NOT NULL,
  received_at       DATETIME DEFAULT CURRENT_TIMESTAMP,
  is_backfill       INTEGER DEFAULT 0,
  -- v2 multi-channel: the Channel.id (from stacks_config.json v2) the
  -- message arrived on. NULL on pre-v2 rows; backfilled by the API at
  -- startup. See docs/plans/2026-05-23-multi-channel-routing.md.
  -- NOTE: source_channel_id only exists on fresh installs. Pre-v2 DBs
  -- get the column added by _migrate_messages_add_source_channel; that
  -- migration also creates the matching index. Keep the index out of
  -- schema.sql so init_schema's executescript can't reference a column
  -- that doesn't yet exist on an existing DB.
  source_channel_id TEXT,
  -- Telegram reply_to_msg_id when this message is a reply to a prior
  -- one in the same chat. Captured by the listener so state_summary
  -- can prepend the parent's text to the SYSTEM STATE block — lets
  -- the AI resolve pronouns like "cancel that order". NULL on normal
  -- (non-reply) messages.
  reply_to_tg_message_id INTEGER,
  -- Pipeline decision tracking. Written by orchestrator at each stage's
  -- terminal exit point so the Pipeline GUI view can show "this message
  -- was decided at stage X with outcome Y" without scanning the JSONL.
  --   decided_stage     — one of: prefilter_drop, trigger_text,
  --                       trigger_embedding, triage_ignored,
  --                       interpreted_signal, interpreted_ignore.
  --                       NULL while in flight. Drives the radial chart.
  --   decided_outcome   — short tag for the row's badge (e.g.
  --                       symbol-mismatch, ad-shape, keep, ignore,
  --                       comma-joined action types for matched).
  --   decided_at        — ISO-8601 UTC at decision time.
  --   pipeline_meta_json— optional blob: triage latency, embedding score,
  --                       matched trigger id, raw responses. Rendered in
  --                       the detail panel; not used for filtering.
  decided_stage     TEXT,
  decided_outcome   TEXT,
  decided_at        TIMESTAMP,
  pipeline_meta_json TEXT,
  UNIQUE(chat_id, tg_message_id)
);
-- idx_messages_decided_stage is created inside
-- _migrate_messages_add_pipeline_columns. Pre-v2 DBs run that migration
-- AFTER executescript(schema.sql), so the columns referenced by the
-- index don't exist here yet — keep the CREATE INDEX co-located with
-- the ALTERs.

CREATE TABLE IF NOT EXISTS actions (
  id              INTEGER PRIMARY KEY,
  source_msg_id   INTEGER REFERENCES messages(id),
  action_type     TEXT NOT NULL CHECK(action_type IN (
                    'OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT',
                    'MOVE_SL_BE','MOVE_SL','CLOSE_PARTIAL','CLOSE_FULL',
                    'REOPEN_LAST','REINFORCE','TIGHTEN_SL','MODIFY_TPS',
                    'OPEN_INSTANT','ATTACH_SIGNAL','CANCEL_PENDING'
                  )),
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
  -- v2 multi-channel: Channel.id and Route.id that produced this action.
  -- NULL on pre-v2 rows. See docs/plans/2026-05-23-multi-channel-routing.md.
  source_channel_id TEXT,
  route_id          TEXT
);
CREATE INDEX IF NOT EXISTS idx_actions_status ON actions(status);
-- Partial composite: most rows have fingerprint=NULL (management types,
-- ALERTs). Restricting to NOT NULL keeps the index small; adding
-- created_at lets _has_recent_duplicate_open run as a single range scan.
CREATE INDEX IF NOT EXISTS idx_actions_fingerprint
  ON actions(fingerprint, created_at)
  WHERE fingerprint IS NOT NULL;
-- NOTE: idx_actions_source_channel and idx_actions_route belong to the
-- _migrate_actions_add_source_channel_route migration only. Pre-v2 DBs
-- don't have the columns yet when this script runs; the migration adds
-- column + index together. Don't duplicate the CREATE INDEX here.

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
  -- exit_price / realized_pnl: populated by the EA at close (or via
  -- /positions/{ticket}/update for partial-close deltas) so the
  -- score-vs-outcome calibration script can compute realized R-multiples
  -- per evaluator score band. Both nullable; NULL == not yet known
  -- (pre-migration trades stay NULL forever, which is acceptable —
  -- calibration looks forward).
  exit_price           REAL,
  realized_pnl         REAL,
  status               TEXT NOT NULL DEFAULT 'open' CHECK(status IN ('open','closed')),
  opened_at            DATETIME DEFAULT CURRENT_TIMESTAMP,
  closed_at            DATETIME,
  close_reason         TEXT,
  -- is_naked / naked_opened_at: set by OPEN_INSTANT; cleared by ATTACH_SIGNAL
  -- or by the EA fallback timer when the structured signal never arrives.
  -- A naked position has no signal-defined SL/TP and rides only on the
  -- emergency SL set at open. naked_opened_at is ISO-8601 UTC.
  is_naked             INTEGER NOT NULL DEFAULT 0,
  naked_opened_at      DATETIME
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

-- v2 per-bot notification queue. The notification dispatcher in the API
-- writes events here; each Bot process polls undelivered rows for the
-- bot_id(s) it serves and DMs the operator.
-- See docs/plans/2026-05-23-multi-channel-routing.md Step 7.
CREATE TABLE IF NOT EXISTS bot_outbox (
  id                INTEGER PRIMARY KEY,
  bot_id            TEXT NOT NULL,
  event_type        TEXT NOT NULL,
  event_payload     TEXT NOT NULL,
  source_channel_id TEXT,
  route_id          TEXT,
  action_id         INTEGER REFERENCES actions(id),
  created_at        DATETIME DEFAULT CURRENT_TIMESTAMP,
  delivered_at      DATETIME
);
CREATE INDEX IF NOT EXISTS idx_bot_outbox_undelivered
  ON bot_outbox(bot_id, created_at) WHERE delivered_at IS NULL;

INSERT OR IGNORE INTO settings(key, value) VALUES
  ('kill_switch', 'off'),
  ('auto_execute_delay_sec', '0'),
  ('last_seen_tg_msg_id', '0');
