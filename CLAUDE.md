# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**CopyTrades**: Telegram → AI → MT5 signal bridge for **XAUUSD** (gold) signals from an Arabic-language channel. Four long-running processes share one SQLite DB (`copytrades.db`) as the sole coordination medium. There is no message queue, no IPC, no RPC — every state transition is a row update.

**Hard invariants** (the prompt and the EA both rely on these):
- **Single symbol**: XAUUSD only.
- **Single open position at a time**: at most one open trade. Management actions (move SL, close partial, etc.) implicitly target the singleton — they do NOT carry an `mt5_ticket`.
- **Fully automated, no human approval gate**: bot's promoter auto-promotes every action after the configured delay. AI accuracy is the only safety net between misclassification and money lost.

## Common commands

All Python commands assume the venv. On Windows use `.venv\Scripts\python.exe` (the `python` alias may resolve to the system interpreter).

```bash
# Full dev install (first time)
python -m venv .venv && .venv\Scripts\activate && pip install -e ".[dev]"

# One-shot setup (Windows)
setup.bat

# Run the full stack (opens 3 console windows; guards port 8765)
launch.bat

# Run a single process
python -m src.api         # FastAPI bridge for the EA (port 8765)
python -m src.bot         # Telegram control bot + promoter + sweepers
python -m src.listener    # Telethon channel watcher + AI pipeline

# Tests
.venv\Scripts\python.exe -m pytest -q                 # hermetic suite
.venv\Scripts\python.exe -m pytest tests/test_api.py::test_post_result_executed -v
.venv\Scripts\python.exe -m pytest --cov=src --cov-report=term-missing

# Live AI replay (costs money, requires ANTHROPIC_API_KEY or OPENAI_API_KEY)
.venv\Scripts\python.exe -m pytest tests/test_replay.py -v             # OPEN-only fixtures
.venv\Scripts\python.exe -m pytest tests/test_management_replay.py -v  # 7 management types

# Ad-hoc helpers
python scripts/find_channel_id.py        # list Telethon dialogs to set TG_WATCHED_CHAT_ID
python scripts/switch_account.py         # interactive listener/bot credential swap
python scripts/test_ai_messages.py       # one-off interpreter call
python scripts/test_ea_signal.py         # inject a synthetic OPEN action straight to the EA
```

EA build: open `ea/CopyTrades.mq5` in MetaEditor (F4 from MT5), F7 to compile. WebRequest URL `http://127.0.0.1:8765` must be whitelisted in Tools → Options → Expert Advisors.

## Architecture

### Pipeline
```
Telegram channel
   → listener.py (Telethon)
   → orchestrator.process_message()
   → AI triage (Haiku / gpt-5-nano) — binary keep|ignore
   → AI interpreter (Sonnet w/ extended thinking, or gpt-5/gpt-5-mini)
   → validators.Action (12 action types) → INSERT actions (status='pending')
   → bot.py notification_dispatcher → DM owner with inline keyboard
   → bot.py promotion_loop → promote_due_actions() flips pending→sent
   → MT5 EA polls GET /actions?status=sent
   → POST /actions/{id}/claim → sent→claimed
   → EA dispatcher routes by action_type → DoOpen / DoMoveSlBe / DoClosePartial / DoReopenLast / DoReinforce / etc.
   → POST /actions/{id}/result → claimed→executed|failed|rejected|watching
   → EA HeartbeatMarketPrice → POST /market/price every 15s (unconditional)
   → EA ReconcileClosedPositions mirrors broker-side closes to DB
```

The action lifecycle (`pending → sent → claimed → {executed|failed|rejected|watching}`) is enforced in `src/schema.sql` as a CHECK constraint and is the central contract across all four processes. **One AI message can emit multiple actions** (compound responses, e.g. `MOVE_SL_BE + CLOSE_PARTIAL`), each becoming its own `actions` row.

### Why these boundaries exist
- **listener ↔ bot split**: the listener runs Telethon (user account credentials, session file, long-lived chat sub). The bot runs python-telegram-bot (bot token, polling getUpdates). Keeping them in separate processes keeps user-account rate limits away from the bot and lets either crash/restart independently.
- **bot owns the promoter**: `promote_due_actions`, `release_stale_claims`, `expire_stale_watches` all run inside `bot.py` as asyncio tasks (see `post_init`), not as separate cron jobs. The bot process is always running when the user expects signals, so co-location avoids a 4th service.
- **api.py is dumb**: it only translates HTTP↔SQL. No business logic. The EA decides whether to open, watch, chase, or skip — the API just records the result. The two state-tracking columns it does maintain (`partial_close_count`, `sl_moved_at`) are derived bookkeeping driven entirely by what the EA POSTs.

### Action types (12 total)

Defined in `src/validators.py` and enforced by the `actions.action_type` CHECK constraint in `schema.sql` (Phase-2 migration `_migrate_actions_add_phase2_types` widens the CHECK on existing DBs).

| Type | Payload | When AI emits |
|---|---|---|
| `OPEN` | `symbol, side, entry_low, entry_high, sl, tps[], comment` | Full new trade signal |
| `MOVE_SL_BE` | `{}` | "أمن دخولك" — move SL to entry (BE). No ticket. |
| `MOVE_SL` | `{price}` | "ستوبك 26" / "ستوبك ثابت 4806" — move SL to specific price. Shorthand decoded against MARKET block. |
| `CLOSE_PARTIAL` | `{fraction=0.5}` | "احجز نصف أرباحك" — close % of original_volume. |
| `CLOSE_FULL` | `{}` | "خرجنا" — close the singleton. |
| `REOPEN_LAST` | `{within_hours=24}` | "متاحة للدخول لو مش داخل" — re-enter last-closed if no current position. |
| `REINFORCE` | `{side}` | "عزز شراء" — close current (any PnL) + reopen with prior params. |
| `TIGHTEN_SL` | `{by_fraction=0.5}` | "ستوبك صغير" — reduce SL distance. |
| `ALERT` | `{level, text}` | Anything ambiguous; bot DMs operator. |
| `MODIFY` | `{mt5_ticket, new_sl?, new_tp?}` | **Legacy** — kept for compat; prefer the new types. |
| `CLOSE` | `{mt5_ticket, reason}` | **Legacy** — prefer `CLOSE_FULL`. |
| `CLOSE_ALL` | `{symbol, reason}` | **Legacy** — single-position mode makes this rare. |

The new management types (MOVE_SL_BE…TIGHTEN_SL) **never carry `mt5_ticket`** — the EA infers the singleton via `FindSingletonOpenTicket(Symbol_Override)`. The legacy types still require a ticket.

### Position-state context (Phase 1 plumbing)

The AI prompt's idempotency rules require knowing what's already been done to the open position. Three fields drive this, surfaced in the SYSTEM STATE block by `state_summary.render_open_positions(conn)`:

- `positions.original_volume` — snapshot at insert time, never updated. Lets the prompt see "0.04 of 0.08 orig" so a reminder message can tell that a partial already fired.
- `positions.partial_close_count` — incremented inside `POST /positions/{ticket}/update` when new `volume < current volume`. Drives the "skip CLOSE_PARTIAL on reminder" rule.
- `positions.sl_moved_at` — set the FIRST time `sl` changes via `POST /positions/{ticket}/update`. Drives the "moved" flag in the rendered block.

Plus two read-side blocks the prompt also consumes:

- **`LAST CLOSED POSITION (XAUUSD, within 24h)`** — `state_summary` queries `positions LEFT JOIN actions` and renders the most recent closed row + the originating signal payload (`entry_low/high, sl, tps[]`). This is the source of params for `REOPEN_LAST` and `REINFORCE`. Backed by `GET /positions/last_closed?symbol=XAUUSD&within_hours=24`.
- **`MARKET (XAUUSD)`** — bid/ask/mid from the `settings` table (`market_XAUUSD_bid|ask|at`), updated every 15s by the EA's `HeartbeatMarketPrice()` POST. Required for two-digit SL shorthand decoding (e.g. `ستوبك 56` → 4856 only when the model knows gold is around 4850). Renders `STALE` when `>60s` old; the prompt is told not to guess in that case.

### EA staged-close policy

`ManagePlans()` in `ea/CopyTrades.mq5` handles automated partial closes when an MT5-side TP is hit. Per signal-TP-count:

| TPs | TP1 hit | TP2 hit | Final exit |
|---|---|---|---|
| **1** | — (no `TradePlan` registered; broker SL+TP ride to closure) | — | TP1 closes the position |
| **2** | Close **70 %** • **SL → `SignalAnchorSl`** • broker TP **removed** • trail starts on remaining 30 % | — (broker TP gone) | Trail SL hit on reversal |
| **3** | Close **50 %** • **SL → `SignalAnchorSl`** • broker TP unchanged | Close **30 % of original** • **SL → TP1 price** • broker TP **removed** • trail starts on remaining 20 % | Trail SL hit on reversal |

`SignalAnchorSl(p, exitPrice)` returns `entry_low` for BUY / `entry_high` for SELL — the edge of the signal zone behind the chase fill. Falls back to `entry` (chased fill) when the anchor would be looser than `slOrig`, would trigger an immediate stop-out, or the plan has no zone persisted (legacy in-flight plans pre-2026-05-09).

Trailing: `TrailStage2Sls()` activates for `(tpCount==2, stage>=1)` and `(tpCount==3, stage>=2)`. Gap = `|finalTp − entry| / N`, with `N=2` for 2-TP (wider, single-stage remainder) and `N=3` for 3-TP (tighter, SL already at TP1). Step threshold 5 × point; ratchet-only.

Channel-driven AI instructions can override this at any point (`MOVE_SL_BE` / `MOVE_SL` / `CLOSE_PARTIAL` / `CLOSE_FULL`). Any successful `PositionModify` from a channel instruction calls `RemovePlanByTicket` so the next automatic stage doesn't stomp the operator override.

`RegisterPlan` **dedupes by ticket** — without that guard, two plans for one ticket fired each stage twice (`5.6 of 8.43 lots closed at TP1 instead of 1/3` was the original symptom).

### Synthetic pending ("watching")
When the EA can't fill a zone immediately (price hasn't reached the entry band) and `SyntheticLimitEnabled=true`, it POSTs `status="watching"` with a `watch` payload and `expires_at`. The action sits in `status='watching'` — neither executed nor failed. If price enters the zone, the EA triggers and POSTs a terminal result. If `expires_at` passes first, the bot's `watch_sweeper_loop` flips it to `rejected` (authoritative even when the EA is offline).

### Chase-price
`DoOpen` checks: if price is past the entry zone (`>entry_high` for BUY, `<entry_low` for SELL), `ChasePriceEnabled=true`, SL still on the protective side, AND `remaining/orig_reward >= ChaseMinRewardRatio` (default 0.5) — open at current market instead of waiting. `g_stats_chased++` increments and a `CT chase:` line goes into the EA log.

### Reconciliation
`ReconcileClosedPositions` in the EA runs on OnTimer. Two passes:
1. Recent-history scan (48h) via `HistorySelect`/`HistoryDealsTotal`/`DEAL_ENTRY_OUT`.
2. DB-authoritative: `GET /positions?status=open`, and for any ticket MT5 doesn't recognize, `POST /positions/{t}/close` with reason `mt5_not_found`.

No throttle — the DB-side pass is cheap and must converge within one timer tick to avoid stale "open" rows confusing the dashboard and the AI prompt's state context.

### Key modules
- `src/orchestrator.py` — AI pipeline entrypoint. Dedup (fingerprint band), signal memory accumulation, triage gate, validates + persists actions. `is_backfill` flag threads through so replayed old messages are processed with context but flagged.
- `src/validators.py` — 12 Pydantic models + `Action` Union + `_ACTION_BY_TYPE` registry + `validate_action`. Phase-2 management actions are pass-through at validate time; range checks (`fraction > 0`, `price > 0`, `within_hours <= 168`) are enforced by `pydantic.Field` constraints at parse time. State guards (is a position open? has the partial fired?) are deferred to the EA so the validator doesn't race execution.
- `src/ai.py` — `AIClient` + `SYSTEM_PROMPT`. The prompt teaches the Arabic vocabulary → action_type map, idempotency rules using the SYSTEM STATE block, two-digit shorthand decoding (anchored on MARKET mid), past-tense-as-imperative rule, single-position invariant. ~11K chars, cache-eligible. 15 worked examples drawn from `messages.csv`.
- `src/ai_triage.py` — cheap binary `keep|ignore` pre-filter (Haiku/gpt-5-nano). Has explicit Arabic management triggers list so short messages like *عزز شراء* always reach the interpreter.
- `src/state_summary.py` — `render_open_positions(conn)` returns a 4-block string: OPEN POSITIONS (enriched: `vol/orig`, `partials_taken`, `at_BE`, `moved`, `age`), PENDING OPEN SIGNALS, LAST CLOSED POSITION (within 24h, with originating signal), MARKET (bid/ask/mid + age, with `STALE` marker).
- `src/signal_memory.py` — per-chat summary buffer that replaces the raw 20-message chat window. Cleared on OPEN. Toggle with `SIGNAL_MEMORY_ENABLED`.
- `src/fingerprint.py` — `signal_fingerprint(action)` buckets resent/quoted signals within `FINGERPRINT_BAND_PRICE` and `FINGERPRINT_WINDOW_HOURS` so the same trade isn't queued twice.
- `src/promoter.py` — three lifecycle sweepers: `promote_due_actions`, `release_stale_claims`, `expire_stale_watches`. The watch sweeper is authoritative even when the EA is offline.
- `src/db.py` — `connect()` sets row_factory + WAL. `init_schema()` runs `schema.sql` + 6 idempotent migrations: `_migrate_actions_for_claim`, `_migrate_actions_add_fingerprint`, `_migrate_signal_memory_add_chat_id`, `_migrate_actions_for_watching`, `_migrate_positions_state` (Phase 1: `original_volume` + `partial_close_count` + `sl_moved_at`), `_migrate_actions_add_phase2_types` (Phase 2: widen `actions.action_type` CHECK).
- `src/logging_setup.py` — every entrypoint calls `configure_logging(name)` once. Tees to stderr AND rotating `logs/<name>.log` (10MB × 5). `http_logger()` is a separate non-propagating logger for `logs/api_http.log`.
- `src/api.py` — FastAPI app. `GET /positions?status=open` is the EA's reconciliation oracle. `GET /positions/last_closed` and `POST/GET /market/price` are the Phase-1 endpoints driving the SYSTEM STATE prompt block.
- `src/llm_provider.py` — provider abstraction. `LLMProvider` Protocol + `AnthropicProvider` and `OpenAIProvider` concretes. `AI_PROVIDER` env switch picks one. Usage dict normalized to `{input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens}` so `logs/ai_calls.jsonl` is provider-agnostic.
- `ea/CopyTrades.mq5` + `ea/Dashboard.mqh` + `ea/LogPanel.mqh` — MQL5 EA. Magic number `919191`. Canvas dashboard (left-anchored, 380×900px, hash-gated repaint). Side `LogPanel` widget (360×900, mounted at `DashboardX + 388` by default; toggle `ShowLogPanel`) renders the last ~20 actions from `GET /events/recent` on a 3 s poll; same hash-gated repaint pattern as the dashboard. `g_plans[]` persisted to MT5 `GlobalVariables` so restart doesn't lose in-flight zone watches. `ExecuteOne` dispatcher routes 11 action types (12 types minus `ALERT` which only the bot consumes). `HeartbeatMarketPrice()` POSTs bid/ask every `MarketPriceHeartbeatSec` (default 15s).

## AI prompt design (Phase 3)

The interpreter prompt is the central piece of IP. Key sections inside `SYSTEM_PROMPT` in `src/ai.py`:

1. **HARD INVARIANTS** — single symbol, single position, no `mt5_ticket` on management types.
2. **TRIAGE FLOW** — 4 tiers: `ignore`, `context`, `signal`, `partial_signal`. Compound messages emit multiple actions in one response.
3. **ARABIC VOCABULARY → ACTION MAP** — verbatim phrases from the channel mapped to action types, including conditional/reminder forms.
4. **PRICE DECODING** — two-digit shorthand expansion. For OPEN signals anchored on the explicit 4-digit SL/TP in the same message; for `MOVE_SL` shorthand anchored on the `MARKET` mid. STALE/missing market price → emit `ALERT` warning, never guess.
5. **IDEMPOTENCY RULES** — explicit table mapping `{state, message}` → emit-or-skip. The most important ones:
   - `partials_taken >= 1 + reminder language` → skip (unless message says "النصف الثاني")
   - `at_BE + "أمن دخولك"` → skip
   - `MOVE_SL price within 0.05 of current sl` → skip
   - `REOPEN_LAST + position already open` → skip
6. **PAST-TENSE AS IMPERATIVE** — "طلعنا تأمين دخول" treated as MOVE_SL_BE imperative when state shows it isn't yet applied.
7. **COMMENTARY FILTER** — religious phrases, time references, encouragement, self-justification stripped.
8. **REOPEN_LAST / REINFORCE DETAILS** — uses `LAST CLOSED POSITION (within 24h)` block; emits ALERT if none.
9. **15 WORKED EXAMPLES** — drawn from `messages.csv`, each pairs an input message + state with the expected action list. These are the few-shot anchors.

## Conventions specific to this codebase

- **All timestamps are ISO-8601 UTC with explicit `+00:00`**. The `execute_after`, `expires_at`, `closed_at`, `sl_moved_at`, `market_XAUUSD_at` columns are string-compared against `datetime.now(timezone.utc).isoformat()` in SQL. **Never use `datetime.utcnow()`** — it produces a naive string that sorts before tz-aware ones.
- **`OR IGNORE` vs `OR REPLACE` in positions insert is deliberate**: a re-POST for an already-inserted ticket must NOT resurrect a closed row (see `api.py post_result`). First insert wins.
- **`original_volume = volume` on insert** in `post_result` — this snapshot must never be updated; it's the basis for the AI's "already partial-closed" reasoning.
- **`partial_close_count` increment is conditional**: only when `body.volume < row["volume"]`. Repeated POSTs of the same volume are safe no-ops.
- **`sl_moved_at` is set ONCE** — first SL change. Subsequent moves don't update it. The "moved" flag in the prompt is binary.
- **EA-side: management actions never carry `mt5_ticket`** — `FindSingletonOpenTicket(Symbol_Override)` resolves it. If no open position, the handler POSTs `rejected` with reason `no_open_position`.
- **EA-side: `RegisterPlan` dedupes by ticket** — replace-in-place if a plan already exists for that ticket. Without this guard, `ManagePlans` fired each stage twice and over-closed.
- **EA-side: `HeartbeatMarketPrice` runs unconditionally** in OnTimer (even when kill switch is on) so the AI's price context stays fresh while halted.
- **`configure_logging(name)` is idempotent** — tests re-import entrypoints and must not duplicate handlers.
- **No `logging.basicConfig(...)` in entrypoints**. Always `from src.logging_setup import configure_logging`.
- **422 handler in `api.py`** persists the raw body and parsed errors, because EA-side JSON bugs at 3 AM are otherwise un-debuggable.
- **`bootstrap_retries=-1` on `app.run_polling`** and `connection_retries=-1` on Telethon — a single startup network blip must not crash either long-running process.

## Testing notes

- `tests/test_replay.py` — live AI on OPEN-only fixtures (`fixtures/messages.jsonl`, pre-rendered blocks). Excluded by default; requires `ANTHROPIC_API_KEY`.
- `tests/test_management_replay.py` — live AI on the 7 new management types using **state-driven** fixtures (`fixtures/management_messages.jsonl`). Each row has a structured `state` (open_position, last_closed, market) which the test seeds into the DB; `state_summary.render_open_positions(conn)` renders the SYSTEM STATE block dynamically. Skipped unless a provider key is set. Soft-fails on `category` mismatch (informational only); hard-asserts on action types. **This is the prompt-drift safety net** — re-run after any prompt edit.
- All other tests are hermetic. 166 total in the hermetic suite.
- Integration test (`tests/test_integration.py`) spins up a real FastAPI app against an in-memory-like SQLite and a mock EA loop. **Don't mock the DB** — schema bugs have historically only surfaced end-to-end.
- `freezegun` is used for deterministic time in backfill/promoter tests.
- Adding a new fixture row: pick an Arabic phrase → action mapping that's not yet covered, fill in `state` to make the desired answer correct, run the replay, iterate prompt or fixture if it fails.

## Operational gotchas

- Console windows closing does not lose logs — everything rotates into `logs/`. Grep `logs/listener.log`, `logs/bot.log`, `logs/api.log`, `logs/api_http.log` when something is wrong.
- Kill switch (`/halt`) only stops promotion. Already-sent actions will still execute. Use `/cancel <id>` for in-flight ones.
- `MaxLotsPerSignal=0.01` in EA inputs for first 2 weeks live. Demo account ≥2 weeks first.
- `ChaseMinRewardRatio=0.5` is the default. Raise to 0.6+ if you want stricter "must have most of the move ahead" behavior.
- `MarketPriceHeartbeatSec=15` is the default heartbeat. The prompt treats anything older than 60s as STALE and refuses to decode shorthand SL.
- If the AI prompt rewrite causes drift on a fixture: it's faster to edit the example IN the prompt than to fight the model with new rules. The 15 worked examples in `SYSTEM_PROMPT` are the strongest signal.
- `REINFORCE` closes the current position regardless of PnL (per channel semantics — the channel says reinforce as a directional conviction signal, not a profit-take). The EA-side handler unconditionally closes then re-opens.
- `REOPEN_LAST` only fires if no current position. If the prompt mistakenly emits one while a position exists, the EA POSTs `rejected` with reason `already_open` — visible in `logs/api_http.log`.
