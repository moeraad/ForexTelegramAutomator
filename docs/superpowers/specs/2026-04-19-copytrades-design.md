# CopyTrades — Telegram-to-MT5 AI Signal Bridge

**Status:** Design approved, pending implementation plan
**Date:** 2026-04-19
**Owner:** moe.raad@gmail.com

## Goal

Automate trading from a forex Telegram channel (FXENGIN VIP, gold-only signals) into MetaTrader 5. An AI reads channel messages in real time, maintains conversation context, and emits structured trading actions (open/modify/close) for an MT5 Expert Advisor to execute. The user remains in the loop via a Telegram control bot with a notify-and-auto-execute window and a kill switch.

## Non-goals (explicitly out of scope for v1)

- Backtesting framework. Live forward-test only.
- Web dashboard. Telegram bot + DB queries are the UI.
- Multi-account or multi-channel support. One channel, one MT5 account.
- Multi-symbol. Gold (XAUUSD) only. Non-gold signals are rejected by the validator.
- ML / fine-tuning. Anthropic Sonnet 4.6 with prompt caching is the model.

## Key decisions

| Decision | Choice |
|---|---|
| Telegram access | Telethon with user account (regular member of channel) |
| LLM | Claude Sonnet 4.6 via Anthropic API, with prompt caching |
| AI ↔ EA bridge | SQLite as source of truth + localhost HTTP for MT5 reads |
| Oversight | Notify-and-auto-execute (default 30 sec delay) with cancel/halt commands |
| Notification channel | Dedicated Telegram bot (separate from listener) |
| Risk management | Lives entirely inside the MT5 EA |
| Symbols | XAUUSD only |
| Hosting | Dev on local Windows PC; production on Windows VPS |

## Architecture

Four processes communicating through one SQLite database (WAL mode).

```
Telegram (FXENGIN VIP)
        │ MTProto
        ▼
┌──────────────────┐    ┌────────────────────┐
│  listener.py     │    │  bot.py            │
│  - Telethon      │    │  - Telegram bot    │
│  - Calls AI      │    │  - DMs signals     │
│  - Writes actions│    │  - /cancel /halt…  │
└────────┬─────────┘    └─────────┬──────────┘
         │                        │
         ▼                        ▼
   ┌───────────────────────────────────┐
   │       SQLite DB (shared file)     │
   └────────┬─────────────────────┬────┘
            │                     │
            │ HTTP (localhost)    │ writes mirror
            ▼                     ▲
   ┌───────────────────┐          │
   │  api.py (FastAPI) │──────────┘
   │  GET /actions     │
   │  POST /actions/   │
   │       {id}/result │
   └────────┬──────────┘
            │ WebRequest()
            ▼
   ┌───────────────────┐
   │  CopyTrades.mq5   │
   │  - Reads sent     │
   │    actions        │
   │  - Risk + sizing  │
   │  - OrderSend      │
   │  - Reconciles     │
   │    positions      │
   └───────────────────┘
```

### Process responsibilities

**`listener.py`** — Telethon client subscribed to the FXENGIN VIP chat. On each new message:
1. Insert into `messages`.
2. Build the AI prompt (system + cached recent chat + fresh state block + new message).
3. Call Claude Sonnet 4.6.
4. Validate the response (JSON schema + business rules).
5. Insert valid actions into `actions` with `status='pending'` and `execute_after = now + delay`.
6. Trigger `bot.py` to notify (via DB poll or in-process queue — implementation detail).

**`bot.py`** — Telegram bot owned by the user. Two responsibilities:
- Outbound: DM the user when a new action is created. Include cancel/execute-now buttons.
- Inbound: handle `/cancel`, `/execute`, `/halt`, `/resume`, `/status`, `/positions`, `/closeall`. All modify the DB.
- Promotion worker (small loop): every second, find actions where `status='pending' AND execute_after <= now AND kill_switch='off'` → set `status='sent'`.

**`api.py`** — Tiny FastAPI app on `localhost:PORT`, MT5-whitelisted in WebRequest URLs. Two endpoints:
- `GET /actions?status=sent` — returns ordered list of executable actions.
- `POST /actions/{id}/result` — body: `{status: "executed"|"failed", mt5_ticket?, error?, position_snapshot?}`. Updates `actions` and `positions`.

**`CopyTrades.mq5`** — MT5 EA. `OnTimer()` every 1 second:
1. `WebRequest GET /actions?status=sent`.
2. For each: check kill switch (also queried via API), apply risk rules, `OrderSend`/`PositionModify`/`PositionClose`, `POST` result.
3. Reconciliation: poll MT5 for our open tickets; if a ticket closed in MT5 (TP/SL/manual), POST close back to API to update `positions`.

## Data model

```sql
CREATE TABLE messages (
  id              INTEGER PRIMARY KEY,
  tg_message_id   INTEGER NOT NULL,
  chat_id         INTEGER NOT NULL,
  sender          TEXT,
  text            TEXT NOT NULL,
  received_at     DATETIME DEFAULT CURRENT_TIMESTAMP,
  is_backfill     INTEGER DEFAULT 0
);

CREATE TABLE actions (
  id              INTEGER PRIMARY KEY,
  source_msg_id   INTEGER REFERENCES messages(id),
  action_type     TEXT NOT NULL,            -- OPEN | MODIFY | CLOSE | CLOSE_ALL | ALERT
  payload_json    TEXT NOT NULL,
  status          TEXT DEFAULT 'pending',   -- pending | cancelled | sent | executed | failed | rejected
  created_at      DATETIME DEFAULT CURRENT_TIMESTAMP,
  notified_at     DATETIME,
  execute_after   DATETIME,
  executed_at     DATETIME,
  ea_response     TEXT
);
CREATE INDEX idx_actions_status ON actions(status);

CREATE TABLE positions (
  id              INTEGER PRIMARY KEY,
  action_id       INTEGER REFERENCES actions(id),
  mt5_ticket      INTEGER UNIQUE,
  symbol          TEXT NOT NULL,
  side            TEXT NOT NULL,            -- BUY | SELL
  volume          REAL NOT NULL,
  entry_price     REAL,
  sl              REAL,
  tp              REAL,
  status          TEXT,                     -- open | closed
  opened_at       DATETIME,
  closed_at       DATETIME,
  close_reason    TEXT
);
CREATE INDEX idx_positions_status ON positions(status);

CREATE TABLE settings (
  key             TEXT PRIMARY KEY,
  value           TEXT
);
-- seed: ('kill_switch','off'), ('auto_execute_delay_sec','30')
```

### Action `payload_json` schemas

```json
// OPEN
{ "symbol":"XAUUSD", "side":"BUY",
  "entry_low":4864, "entry_high":4866,
  "tps":[4880,4900,4920], "sl":4855,
  "comment":"FXENGIN signal" }

// MODIFY
{ "mt5_ticket":12345678, "new_sl":4866, "new_tp":null }

// CLOSE
{ "mt5_ticket":12345678, "reason":"ai_interpreted_close" }

// CLOSE_ALL
{ "symbol":"XAUUSD", "reason":"trader_emergency_exit" }

// ALERT
{ "level":"info|warning", "text":"..." }
```

## AI invocation

**Trigger:** every new Telegram message in the watched chat (excluding backfilled messages, which are skipped or alerted only).

**Prompt structure** (three blocks, first two cacheable):

1. **System prompt** (cached) — defines role, action schema, decision rules.
2. **Recent chat** (cached, sliding window of ~20 messages) — gives short-term context.
3. **Fresh** — current `OPEN POSITIONS` block (rendered from DB) + the new message.

**Decision rules in system prompt:**
- Emit `OPEN` only for clear new trade signals (entry, SL, ≥1 TP).
- For modification/closure language referencing existing positions, emit `MODIFY` or `CLOSE` targeting the relevant `mt5_ticket` from the open positions block.
- "Close all gold" / "exit everything" → `CLOSE_ALL`.
- News warnings, opinions, ambiguous text → `ALERT` (or empty actions array).
- **Never emit `OPEN` for a signal already represented in the open positions block.**
- When in doubt, emit `ALERT` with reasoning.

**Output format:** strict JSON `{actions: [...], reasoning: "..."}`. Validated by Pydantic.

**Validation pipeline (Python-side, after AI response):**
- Schema validation (Pydantic).
- Business rules:
  - `OPEN.symbol` must be `XAUUSD` (else reject + ALERT).
  - `OPEN` must have `entry_low/high`, `sl`, ≥1 `tp`.
  - `MODIFY/CLOSE.mt5_ticket` must exist and be `open` in `positions`.
  - Duplicate `OPEN` (same symbol + side + overlapping entry zone with an existing open position from the same source signal) → reject.
- Rejected actions are recorded with `status='rejected'` and an ALERT is sent to the user.

**Cost control:** prompt caching on system + recent-chat blocks. Sonnet 4.6 list price ~$3/M input → ~$0.30/M on cache hit. Cache TTL 5 min; sparse-signal periods incur occasional misses.

**Failure handling:**

| Failure | Response |
|---|---|
| Anthropic API error/timeout | Retry 2× with backoff; on persistent failure, write ALERT row + bot DM |
| Unparseable JSON | Log raw output, write ALERT, no trade |
| Reference to unknown ticket in CLOSE/MODIFY | Reject action, ALERT |
| Duplicate OPEN | Reject (deterministic check, not AI's responsibility) |

## Telegram bot (control plane)

**Outbound notification (per new action):**

```
🟢 NEW SIGNAL #87  (auto-execute in 30s)

OPEN BUY XAUUSD
Entry: 4864–4866
SL:    4855
TPs:   4880 / 4900 / 4920

Source: "GOLD ✅ BUY ✅ @ 4866 - 4864..."

[ Cancel ]   [ Execute now ]
```

ALERT-type actions are sent without buttons.

**Commands:**

| Command | Effect |
|---|---|
| `/cancel <id>` (or button) | Set action `status='cancelled'`. EA will skip it. |
| `/execute <id>` (or button) | Promote `pending → sent` immediately, bypassing the wait. |
| `/halt` | Set `kill_switch='on'`. Promotion worker stops promoting. AI continues parsing. **Does not cancel actions already promoted to `sent`.** |
| `/resume` | Set `kill_switch='off'`. |
| `/status` | Kill switch state, count of pending/sent actions, count of open positions. |
| `/positions` | List open positions with current P&L (queried from MT5 via the EA reconciliation snapshot in the DB). |
| `/closeall` | Manually emit a CLOSE_ALL action. |

**Auto-execute mechanism:**
- New action → `execute_after = now + auto_execute_delay_sec` (default 30, configurable in `settings`).
- Promotion worker (in `bot.py`): every 1s, find `pending` actions past their `execute_after` while `kill_switch='off'`, set them to `sent`.
- EA also re-checks kill switch before executing (defense in depth).

## MT5 EA — `CopyTrades.mq5`

**EA inputs (configurable in MT5 terminal):**

```mq5
input double  RiskPercentPerTrade        = 1.0;
input double  MaxLotsPerSignal           = 0.50;
input int     MaxOpenPositions           = 3;
input int     EntryZoneMode              = 1;   // 0=midpoint limit, 1=market if in zone, 2=split limits
input int     TPMode                     = 1;   // 0=single TP1, 1=split N positions per TP
input int     SlippagePoints             = 50;
input string  ApiBaseUrl                 = "http://127.0.0.1:8765";
input int     PollIntervalSec            = 1;
```

**Loop (`OnTimer` every `PollIntervalSec`):**
1. `WebRequest GET /actions?status=sent` → array of actions.
2. Check `/settings/kill_switch` — if on, skip step 3.
3. For each action:
   - Apply EA risk rules. If rules reject (e.g., MaxOpenPositions reached), POST `{status:"rejected", error:"max_positions"}`.
   - Execute via `OrderSend` / `PositionModify` / `PositionClose`.
   - For OPEN with `TPMode=1`: open N partial positions, one per TP, all sharing SL. All link to the same `actions.id`.
   - POST result with `mt5_ticket`(s) and a position snapshot.
4. Reconciliation: for each row in `positions` where `status='open'`, query MT5 for the ticket. If closed in MT5, POST close-update.

**SQLite access from MT5:** via the FastAPI bridge (`api.py`). Chosen over direct `sqlite3.dll` because `WebRequest()` is well-documented and easier to debug. Cost: one extra small process.

## Safety, edge cases, observability

**Defense in depth:**

| Layer | Catches |
|---|---|
| AI prompt rules | Duplicate OPEN, malformed intent |
| JSON schema validation | Hallucinated field/format |
| Action validator | Wrong symbol, missing SL, unknown ticket |
| Notify-and-wait window | Lets the user see and cancel |
| Kill switch | Halts new executions instantly |
| EA risk rules | Lot cap, position count cap, sizing cap |
| Broker | Final backstop — rejects bad orders |

**Edge cases handled:**
- Duplicate signal posts → AI sees current open positions, no-ops.
- Out-of-order messages → processed in arrival order; rare and tolerable.
- Long silence then "close all" → AI still has positions in state block, emits `CLOSE_ALL`.
- Non-gold signal posted → action validator rejects, ALERT sent.
- MT5 disconnected → `OrderSend` fails, action `failed`, bot notifies.
- Telethon disconnect → reconnect with backoff; on reconnect, fetch missed messages with `min_id=last_seen_tg_id` and mark them `is_backfill=1`. Backfilled messages are NOT sent to the AI; instead, a single ALERT informs the user "you missed N messages while offline." This avoids opening stale signals.
- SQLite contention → WAL mode + short transactions. Load is well under 1 write/sec.

**Observability:**
- Every AI call logged to `logs/ai_calls.jsonl`: prompt, response, latency, token usage, cache hit ratio.
- Action lifecycle fully recorded in DB via timestamp columns.
- `/status` surfaces live state.
- Optional daily summary DM (out of v1 scope).

## Testing strategy

**Tier 1 — Unit (Python).** Action validator, state-summary builder, status state machine, bot command parsing.

**Tier 2 — AI replay tests.** Build a fixture set of 30–50 real channel messages with expected actions. A test runner replays each (message + state) through the AI and asserts output. Run on every prompt change. **This is the most important test layer.**

**Tier 3 — Integration.** Fake Telegram message → listener → AI → action → bot → promotion → mock-EA reads `sent`, posts back `executed`. End-to-end without a broker.

**Tier 4 — Demo broker.** Real Telegram, real AI, real EA, MT5 demo account, ≥2 weeks. Review every executed signal vs. what the user would have done manually. Tune prompt and validators.

**Tier 5 — Live micro-lots.** Real broker with `MaxLotsPerSignal=0.01` for ≥2 weeks. Promotion criterion to larger lots: zero unexpected actions for 14 consecutive trading days.

## Repository layout (planned)

```
copytrades/
├── README.md
├── pyproject.toml
├── .env.example                    # API keys, telegram credentials
├── src/
│   ├── listener.py                 # Telethon + AI orchestration
│   ├── bot.py                      # Telegram bot + promotion worker
│   ├── api.py                      # FastAPI for MT5 access
│   ├── ai.py                       # Prompt builder + Anthropic client
│   ├── validators.py               # Action schema + business rules (Pydantic)
│   ├── db.py                       # SQLite helpers, schema migrations
│   └── telegram_format.py          # Bot message rendering
├── ea/
│   └── CopyTrades.mq5              # MT5 EA
├── fixtures/
│   └── messages.jsonl              # Replay test corpus
├── tests/
│   ├── test_validators.py
│   ├── test_state_summary.py
│   ├── test_state_machine.py
│   ├── test_replay.py              # AI behavior tests
│   └── test_integration.py
├── docs/superpowers/
│   ├── specs/
│   │   └── 2026-04-19-copytrades-design.md
│   └── plans/                      # Implementation plans live here
└── logs/                           # Runtime logs (gitignored)
```

## Open implementation questions (defer to plan)

- Specific Anthropic SDK pattern for prompt caching (`cache_control` markers placement).
- Exact promotion-worker design (in-process loop in `bot.py` vs. separate `worker.py`).
- Migration tooling for SQLite (raw SQL files vs. Alembic vs. inline `CREATE TABLE IF NOT EXISTS`).
- MT5 polling: `OnTimer` granularity vs. `OnTick` opportunism.
- Service supervision on Windows: NSSM vs. Task Scheduler vs. just running in terminal during dev.

---

*Approved by user on 2026-04-19. Next step: writing-plans skill to produce implementation plan.*
