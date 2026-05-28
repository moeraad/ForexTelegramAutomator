# 05 — State and Data

**Summary.** Everything lives in **one SQLite database per "stack"** (one channel = one DB). The DB is the only coordination medium; processes communicate by INSERT/UPDATE. Secrets in the `settings` table are DPAPI-encrypted at rest. Logs are rotating files on disk. The audit chain from a closed broker trade back to the Telegram message that caused it is intact (FK joins messages → actions → positions), but operator-facing tooling for that lookup is limited to the Journal GUI tab and grep on the logs.

## Where state lives

| State | Storage | Lifetime |
|---|---|---|
| Telegram messages received | `messages` table | forever (UNIQUE constraint is the dedup tombstone — `_cleanup_message_if_orphan` is a no-op since the team learned that DELETE caused $0.01–0.05 reprocessing loops, see `src/orchestrator.py:141`) |
| AI decisions per message | `messages.decided_stage`, `decided_outcome`, `decided_at`, `pipeline_meta_json` | forever |
| All trading actions | `actions` table | forever |
| All positions opened by EA | `positions` table | forever |
| Per-chat signal memory | `signal_memory` table | until `cleared_at` (OPEN clears history) |
| App config (host, port, models, tuning) | `settings` table | forever |
| Secrets (API keys, Telegram session, EA token, bot token) | `settings` table, DPAPI-encrypted via `src/secret_box.py` | forever |
| Market price heartbeat | `settings.market_XAUUSD_bid / _ask / _at` | last value wins |
| Market OHLC+ATR snapshot | `settings.market_snapshot_XAUUSD` (JSON string) + `_at` | last value wins |
| v2 multi-channel config | `<APPDATA>/CopyTrades/stacks_config.json` (separate JSON file) | forever |
| Channel profile (vocabulary, examples) | `<APPDATA>/CopyTrades/<stack>/profile.json` OR `channels/<name>.json` | forever (operator-edited via GUI) |
| In-flight EA plans (`g_plans[]`) | MT5 GlobalVariables (terminal-local) | until EA restart + reload from GlobalVariables |
| In-flight pending orders (`g_pending_orders[]`) | MT5 GlobalVariables | same |
| Naked positions tracker | MT5 GlobalVariables | same |
| Retry queue for failed POSTs | files in `MQL5\Files\` | until next OnTimer tick succeeds |
| Telethon session | `settings.tg_session_blob` (DPAPI-encrypted StringSession) + Telethon side files | forever |
| AI call log (cost / latency) | `logs/ai_calls.jsonl` (rotates by file pattern, not size — appends forever) | until operator prunes |
| Trade log | `logs/trades.log` (rotating 10 MB × 5) | rotation eviction |
| API HTTP error log | `logs/api_http.log` (rotating) | rotation eviction |
| Per-service log | `logs/api.log`, `logs/bot.log`, `logs/listener.log` | rotation eviction |
| NSSM service logs | `logs/nssm-*.out.log`, `logs/nssm-*.err.log` | rotating per NSSM config (10 MB) |

## Schemas

`src/schema.sql` defines the initial shape; 17 idempotent migration functions in `src/db.py:init_schema` widen CHECKs and add columns on every startup.

### `messages`

| Column | Type | Purpose |
|---|---|---|
| id | INTEGER PK | local row id |
| tg_message_id | INTEGER | Telegram's per-chat message id |
| chat_id | INTEGER | Telegram chat id; combined with tg_message_id forms UNIQUE |
| sender | TEXT | username or first_name |
| text | TEXT | raw message body |
| received_at | DATETIME | default CURRENT_TIMESTAMP |
| is_backfill | INTEGER (0/1) | replay vs live |
| source_channel_id | TEXT (nullable) | v2 Channel.id; NULL on pre-v2 rows |
| reply_to_tg_message_id | INTEGER (nullable) | Telegram's reply pointer |
| decided_stage | TEXT (nullable) | prefilter_drop / trigger_text / trigger_embedding / triage_ignored / interpreted_signal / interpreted_ignore |
| decided_outcome | TEXT (nullable) | short tag for radial chart |
| decided_at | TIMESTAMP (nullable) | when stage decision was made |
| pipeline_meta_json | TEXT (nullable) | latencies, raw responses, embedding scores |

### `actions`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| source_msg_id | INTEGER FK → messages | nullable (ALERTs, /closeall) |
| action_type | TEXT CHECK | 16-element whitelist |
| payload_json | TEXT | json.dumps(model.model_dump(exclude={'type'})) |
| status | TEXT CHECK | 8-element lifecycle |
| created_at | DATETIME | default CURRENT_TIMESTAMP |
| notified_at | DATETIME | bot legacy DM mark |
| execute_after | DATETIME | promoter gate; NULL for parked/ALERT |
| claimed_at | DATETIME | EA claim time |
| executed_at | DATETIME | terminal time |
| ea_response | TEXT | error string OR reason ("duplicate_signal", "no_open_position", "claim_expired", etc.) |
| fingerprint | TEXT | OPENs only |
| source_channel_id | TEXT | v2 attribution |
| route_id | TEXT | v2 attribution |

### `positions`

| Column | Type | Notes |
|---|---|---|
| id | INTEGER PK | |
| action_id | INTEGER FK → actions | the OPEN-shape action that birthed this position |
| mt5_ticket | INTEGER UNIQUE | broker-side ticket id |
| symbol, side, volume, entry_price, sl, tp | various | live broker state |
| original_volume | REAL | snapshot; NEVER updated except healing case |
| partial_close_count | INTEGER | incremented when a /update brings smaller volume |
| sl_moved_at | DATETIME | set once on first SL change |
| exit_price, realized_pnl | REAL | populated at close (or via /update deltas) |
| status | TEXT CHECK ('open'|'closed') | |
| opened_at, closed_at, close_reason | various | |
| is_naked | INTEGER (0/1) | set by OPEN_INSTANT; cleared by ATTACH_SIGNAL or fallback timer |
| naked_opened_at | DATETIME | |

### Lifecycle constraints (enforced at the SQL layer)

- `actions.status` ∈ `{pending, cancelled, sent, claimed, watching, executed, failed, rejected}`. CHECK constraint at `src/schema.sql:64`.
- `actions.action_type` ∈ 16 values. CHECK constraint at `src/schema.sql:56`.
- `positions.status` ∈ `{open, closed}`. CHECK constraint at `src/schema.sql:118`.
- `positions.mt5_ticket UNIQUE`. INSERT uses `OR IGNORE` — first insert wins.

### `settings`

Free-form key→value store. Selected keys:

- Boot: `api_host`, `api_port`, `tg_api_id`, `tg_api_hash` (encrypted), `tg_phone`, `tg_session_blob` (encrypted), `tg_watched_chat_id`, `tg_bot_token` (encrypted), `tg_bot_owner_user_id`, `ea_shared_token` (encrypted), `listener_shared_token` (encrypted).
- AI: `ai_provider`, `anthropic_api_key` (encrypted), `anthropic_model`, `openai_api_key` (encrypted), `openai_model`, `openai_triage_model`, `ai_triage_enabled`, `ai_triage_model`, `ai_thinking_enabled`, `ai_thinking_budget_tokens`, `evaluator_version` (v1/v2), `ai_evaluator_enabled`.
- Pipeline tuning: `signal_memory_enabled`, `signal_memory_max_entries`, `signal_memory_max_age_hours`, `fingerprint_band_price`, `fingerprint_window_hours`, `backfill_max_age_min`, `default_auto_execute_delay_sec`, `recent_chat_window`.
- Profile: `channel_profile`.
- Runtime: `kill_switch` (on|off), `kill_switch_reason` (operator|cost_guard), `last_seen_tg_msg_id`, `bot_telegram_ok_at`, `listener_telegram_ok_at`.
- Cost guard: `cost_daily_budget_usd`, `cost_cap_multiplier`.
- Market: `market_XAUUSD_bid`, `_ask`, `_at`, `market_snapshot_XAUUSD`, `market_snapshot_XAUUSD_at`, `market_snapshot_XAUUSD_disabled`.
- Risk: `max_sl_loss_percent` (read by EA on OnInit and on /settings/{key}).

`src/db_settings.py:CRITICAL_KEYS` lists keys without which services refuse to start. `SECRET_KEYS` lists keys auto-encrypted by `set_secret`.

### `signal_memory`

Per-chat summary buffer that replaces raw chat window. Cleared on OPEN.

### `bot_outbox`

v2 per-bot notification queue. `bot_id, event_type, event_payload(json), source_channel_id, route_id, action_id, created_at, delivered_at`. Partial index on `delivered_at IS NULL`.

### `unmatched_messages`

Curation backlog: messages where Sonnet emitted a deterministic-shaped action that the trigger matcher missed. Surfaces in the GUI Triggers tab so the operator can convert them into curated triggers (`src/unmatched_store.py`).

## What survives a restart vs what is lost

| | Survives | Lost |
|---|---|---|
| Service crash (any of api/bot/listener) | All DB state, EA in-flight state, logs | In-flight HTTP requests (retried), ProfileContext cache, AI provider clients |
| EA restart / re-attach | DB state, GlobalVariables-persisted plans/pending/naked | `g_dashboard` cache, `g_eval_*` cache (refetched in 5 s), in-flight WebRequest |
| Machine reboot | DB state, logs | All in-memory state; NSSM restarts services on boot; Telethon session blob re-decrypted from DB |
| Telegram session revoked from another device | DB state | session blob useless; listener refuses to start (`listener.py:516`) — operator must re-run wizard |
| Channel profile edited mid-run | DB state | ProfileContext cache invalidated on file mtime change (per-message check, `src/profile_context.py:100`) — operator does NOT need to restart services |
| Settings edited via GUI | All | Module-level config cache (`src/config.py:_cache`) needs `invalidate_cache()` — services pick up changes on subsequent reads, but some module-level captures (e.g., listener's TG_WATCHED_CHAT_ID at decorator-attach) require restart (REVIEW.md P1) |

## Logging

`src/logging_setup.py` is the single entrypoint. Behavior:

- Every service calls `configure_logging("api"|"bot"|"listener")` once. Tees to `stderr` AND a rotating file `<LOGS_DIR>/<name>.log` (max 10 MB, 5 backups).
- A separate non-propagating `http_logger` writes `logs/api_http.log`.
- A separate non-propagating `trades_log` writes `logs/trades.log` — keeping action lifecycle lines out of the noisy service logs.
- `logs/ai_calls.jsonl` is written by `src/ai_logger.py:log_call` — each call appends one JSON row with: `ts`, `msg_id`, `stage` (`triage|interpret|evaluator|...`), `raw_response`, `latency_ms`, `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_tokens`, `model`, `cost` (computed), `source_channel_id`, `route_id`. This is the only durable record of LLM cost.
- `LOGS_DIR` resolves to `<DB_PATH>.parent/logs` — i.e., per-stack logs live next to the per-stack DB (`src/config.py:41`).

Retention: rotation by file count and size only. There is no offsite log shipping, no log aggregator, no log-to-DB pipeline. Operators are expected to grep on disk.

## Auditability — Telegram message → executed trade and back

**Telegram → trade**: `messages.id (= source_msg_id) → actions.id (= action_id) → positions.id`. `GET /positions/by_ticket/{ticket}` joins all three.

**Trade → Telegram**: same join, run in reverse. The Journal GUI tab (`src/gui/views/journal_view.py`) renders this lineage.

**Two gaps**:

1. **Telegram media is not stored.** If the channel posts an image-with-text, the listener only stores the caption (`msg.message or ""`). Re-running the audit against an image-only signal returns empty text.
2. **EA-side broker fills are summarized** in the `legs[].snapshot` payload of `POST /actions/{id}/result` but the broker's MT5-side `OrderSend` retcode, slippage details, and the actual fill price ladder for multi-leg deals are not stored beyond `entry_price`, `volume`, and `ea_response`. The MT5 terminal's own Journal tab is the source of truth for raw broker data.

## How the system uses time

- All timestamps written from Python are ISO-8601 UTC with explicit `+00:00`. Code-comment policy: "**never use `datetime.utcnow()`**" (CLAUDE.md, `src/api.py:397`).
- SQL DEFAULT `CURRENT_TIMESTAMP` produces `'YYYY-MM-DD HH:MM:SS'` (no timezone, no `T`). `state_summary._age_seconds` normalizes both forms (`src/state_summary.py:46`).
- The `execute_after`, `expires_at`, `closed_at`, `sl_moved_at`, `market_XAUUSD_at` columns are STRING-compared against `datetime.now(timezone.utc).isoformat()` in SQL. This is correct only because the lexical ordering of ISO-8601 matches chronological ordering for UTC strings — fragile if a non-UTC value ever lands.
- EA's `TimeCurrent()` returns broker-server time, NOT UTC. **UNCLEAR** whether the EA normalizes before POSTing — inspection of `HeartbeatMarketPrice` shows it does not include a timestamp in the body (the API stamps `now()` server-side), so the broker/server time gap is invisible to the DB. Reconciliation 48h scan uses `TimeCurrent() - 48*3600` which IS broker time.
