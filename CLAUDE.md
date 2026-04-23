# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

CopyTrades: Telegram → AI → MT5 signal bridge for XAUUSD. Four long-running processes share one SQLite DB (`copytrades.db`) as the sole coordination medium. There is no message queue, no IPC, no RPC — every state transition is a row update.

## Common commands

All Python commands assume the venv. On Windows use `.venv\Scripts\python.exe` (the `python` alias may resolve to the system interpreter).

```bash
# Full dev install (first time)
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"

# Run the full stack (opens 3 console windows; guards port 8765)
launch.bat

# Run a single process
python -m src.api         # FastAPI bridge for the EA (port 8765)
python -m src.bot         # Telegram control bot + promoter + sweepers
python -m src.listener    # Telethon channel watcher + AI pipeline

# Tests
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m pytest tests/test_api.py::test_post_result_executed -v
.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing

# Live AI replay (costs money, requires ANTHROPIC_API_KEY)
.venv\Scripts\python.exe -m pytest tests/test_replay.py -v

# Ad-hoc channel ID lookup (Telethon)
python scripts/find_channel_id.py
```

EA build: open `ea/CopyTrades.mq5` in MetaEditor (F4 from MT5), F7 to compile. WebRequest URL `http://127.0.0.1:8765` must be whitelisted in Tools → Options → Expert Advisors.

## Architecture

### Pipeline
```
Telegram channel
   → listener.py (Telethon)
   → orchestrator.process_message()
   → AI triage (Haiku) → interpreter (Sonnet, extended thinking)
   → validators.Action → INSERT actions (status='pending')
   → bot.py notification_dispatcher → DM owner with inline keyboard
   → bot.py promotion_loop → promote_due_actions() flips pending→sent
   → MT5 EA polls GET /actions?status=sent
   → POST /actions/{id}/claim → sent→claimed
   → EA executes order or begins zone-watch
   → POST /actions/{id}/result → claimed→executed|failed|rejected|watching
   → EA ReconcileClosedPositions mirrors broker-side closes to DB
```

The action lifecycle (`pending → sent → claimed → {executed|failed|rejected|watching}`) is enforced in `src/schema.sql` as a CHECK constraint and is the central contract across all four processes.

### Why these boundaries exist
- **listener ↔ bot split**: the listener runs Telethon (user account credentials, session file, long-lived chat sub). The bot runs python-telegram-bot (bot token, polling getUpdates). Keeping them in separate processes keeps user-account rate limits away from the bot and lets either crash/restart independently.
- **bot owns the promoter**: `promote_due_actions`, `release_stale_claims`, `expire_stale_watches` all run inside `bot.py` as asyncio tasks (see `post_init`), not as separate cron jobs. The bot process is always running when the user expects signals, so co-location avoids a 4th service.
- **api.py is dumb**: it only translates HTTP↔SQL. No business logic. The EA decides whether to open, watch, chase, or skip — the API just records the result.

### Key modules
- `src/orchestrator.py` — the AI pipeline entrypoint. Handles dedup (fingerprint band), signal memory accumulation, triage gate, and writes validated actions + an optional ALERT summary row. `is_backfill` flag threads through so replayed old messages are processed with context but flagged.
- `src/validators.py` — Pydantic models for AI output. `validate_action` is the single gate: if it raises, orchestrator emits an ALERT action instead of a trade. Malformed JSON from the model becomes user-visible, not silent drops.
- `src/signal_memory.py` — per-chat summary buffer that replaces the raw 20-message chat window. Cleared on OPEN (deliberation resolved). Toggle with `SIGNAL_MEMORY_ENABLED`.
- `src/fingerprint.py` — `signal_fingerprint(action)` buckets resent/quoted signals within `FINGERPRINT_BAND_PRICE` and `FINGERPRINT_WINDOW_HOURS` so the same trade isn't queued twice.
- `src/promoter.py` — three lifecycle sweepers (see above). `expire_stale_watches` is authoritative even when the EA is offline.
- `src/db.py` — `connect()` sets row_factory + WAL, `init_schema()` runs `schema.sql` + migrations (notably `_migrate_actions_for_watching` that adds `watch_json`/`expires_at` columns on old DBs).
- `src/logging_setup.py` — every entrypoint calls `configure_logging(name)` once. Tees to stderr AND rotating `logs/<name>.log` (10MB × 5). `http_logger()` is a separate non-propagating logger for `logs/api_http.log` (one line per HTTP request).
- `src/api.py` — the FastAPI app. `GET /positions?status=open` is the EA's reconciliation oracle: any DB-open ticket MT5 doesn't know about gets closed via `POST /positions/{t}/close`.
- `ea/CopyTrades.mq5` + `ea/Dashboard.mqh` — MQL5 EA. Uses magic number `919191` to own positions. Canvas dashboard (left-anchored, 380×560px) with hash-gated repaint. `g_plans[]` persisted to MT5 `GlobalVariables` so restart doesn't lose in-flight zone watches.

### Synthetic pending ("watching")
When the EA can't fill a zone immediately (price hasn't reached the entry band), it POSTs `status="watching"` with a `watch` payload and `expires_at`. The action sits in `status='watching'` — neither executed nor failed. If price enters the zone, the EA triggers and POSTs a terminal result. If `expires_at` passes first, the bot's `watch_sweeper_loop` flips it to `rejected`.

### Reconciliation
`ReconcileClosedPositions` in the EA runs on OnTimer. Two passes:
1. Recent-history scan (48h) via `HistorySelect`/`HistoryDealsTotal`/`DEAL_ENTRY_OUT`.
2. DB-authoritative: `GET /positions?status=open`, and for any ticket MT5 doesn't recognize, `POST /positions/{t}/close` with reason `mt5_not_found`.

No throttle — the DB-side pass is cheap and must converge within one timer tick to avoid stale "open" rows confusing the dashboard and the risk caps.

## Conventions specific to this codebase

- **All timestamps are ISO-8601 UTC with explicit `+00:00`**. The `execute_after` and `expires_at` columns are string-compared against `datetime.now(timezone.utc).isoformat()` in SQL, so any naive datetime or local-tz string will silently compare wrong. Never use `datetime.utcnow()` — it produces a naive string that sorts before tz-aware ones.
- **`OR IGNORE` vs `OR REPLACE` in positions insert is deliberate**: a re-POST for an already-inserted ticket must NOT resurrect a closed row (see `api.py` post_result). First insert wins.
- **`configure_logging(name)` is idempotent** — tests re-import entrypoints and must not duplicate handlers.
- **No `logging.basicConfig(...)` in entrypoints**. Always `from src.logging_setup import configure_logging`.
- **422 handler in `api.py`** persists the raw body and parsed errors, because EA-side JSON bugs at 3 AM are otherwise un-debuggable.
- **`bootstrap_retries=-1` on `app.run_polling`** and `connection_retries=-1` on Telethon — a single startup network blip must not crash either long-running process.

## Testing notes

- `tests/test_replay.py` makes live Anthropic calls — excluded by default, requires `ANTHROPIC_API_KEY`. All other tests are hermetic.
- Integration test spins up a real FastAPI app against an in-memory-like SQLite and a mock EA loop. Don't mock the DB in tests — schema bugs have historically only surfaced end-to-end.
- `freezegun` is used for deterministic time in backfill/promoter tests.

## Operational gotchas

- Console windows closing does not lose logs — everything rotates into `logs/`. Grep `logs/listener.log`, `logs/bot.log`, `logs/api.log`, `logs/api_http.log` when something is wrong.
- Kill switch (`/halt`) only stops promotion. Already-sent actions will still execute. Use `/cancel <id>` for in-flight ones.
- `MaxLotsPerSignal=0.01` in EA inputs for first 2 weeks live. Demo account ≥2 weeks first.
