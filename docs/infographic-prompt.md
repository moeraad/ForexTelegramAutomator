# Infographic Brief: "CopyTrades — AI-Driven Telegram→MT5 Signal Bridge"

## 1. Goal & audience

Produce a single-page **technical infographic** (portrait or scrollable A2/A3) that explains how the **CopyTrades** system turns free-form Telegram messages from a paid signal channel into automated XAUUSD trades on MetaTrader 5, fully managed end-to-end. Audience: a developer or operator who has never seen the system but understands trading, APIs, and SQL.

## 2. Visual style

- **Modern fintech / dev-tool aesthetic.** Think Linear × Stripe × Hex.tech.
- Dark background (deep navy `#0B1220`) with high-contrast neon accents: cyan `#22D3EE` for data, amber `#F59E0B` for AI, green `#10B981` for executed, red `#EF4444` for rejected/failed, violet `#8B5CF6` for state machine.
- Clean monospace labels for code/JSON, sans-serif for prose (Inter or Geist).
- Iconography: outline icons for components, filled icons for data, animated-looking arrows showing direction of flow. Subtle dotted grid background.
- Use **isometric component blocks** for the four long-running processes; flat panels for state diagrams; pipeline arrows between them.

## 3. Core message to communicate (write this somewhere prominent)

> Four independent processes, **one SQLite database as the sole coordination medium**. No queue, no IPC, no RPC — every state transition is a row update. SQLite is the contract; every component reads and writes the same table.

## 4. Required sections (in this order, top → bottom)

### Section A — Hero / one-line system summary
- Title: **CopyTrades**
- Subtitle: *Telegram → AI → MT5 signal bridge for XAUUSD*
- Strap: *Four processes, one SQLite database, zero queues.*
- Show 4 process icons (Listener, Bot, API, EA) connected to a central SQLite cylinder labeled `copytrades.db`.

### Section B — End-to-end pipeline (the big one)

Show this as a **horizontal flowing river** with stages numbered 1–10. Each stage = an icon + a one-line caption + the artifact it produces (DB row, HTTP call, MT5 ticket).

1. **Telegram channel** (paid signal provider). Icon: chat bubble. Note: free-form messages, not structured.
2. **Listener (`listener.py`, Telethon)** — long-lived user-account session subscribed to the channel. Captures every message.
3. **Orchestrator (`orchestrator.process_message`)** — entry into the AI pipeline. Performs:
   - **Fingerprint dedup** (price band + time window — drops resent/quoted signals)
   - **Signal-memory accumulation** (deliberation summary, replaces 20-msg raw chat window)
4. **AI Triage** — fast/cheap pre-filter (Haiku / gpt-5-nano). 4-tier output:
   - `ignore` (chitchat) — drop
   - `context` (commentary) — store for memory only
   - `signal` (full trade signal) — pass to interpreter
   - `partial_signal` (incomplete; needs accumulation) — buffer
5. **AI Interpreter** — Sonnet (extended thinking) or gpt-5/gpt-5-mini. Provider switchable via `AI_PROVIDER` env. Outputs structured JSON: side, entry_low/high, sl, tps[], reasoning.
6. **Validators (`validators.py`, Pydantic)** — single gate. If JSON is invalid → emit an `ALERT` action (visible to operator) instead of a trade.
7. **DB write**: row inserted into `actions` table with `status='pending'`. Optional `ALERT` summary row.
8. **Bot (`bot.py`, python-telegram-bot)** — DMs the owner an inline-keyboard prompt: ✅ Approve / ❌ Cancel / 🛑 Halt.
9. **Promoter (sweeper inside bot.py)** — `promote_due_actions()` flips approved rows from `pending → sent` once `execute_after` passes.
10. **MT5 EA (`CopyTrades.mq5`)** — polls `GET /actions?status=sent`, claims atomically, executes, posts result.

Annotate **between stages 7 and 8**: "Telegram bot has its own bot-token process; Telethon (listener) uses a user-account session. Splitting them keeps user-account rate limits away from the bot, and either can crash/restart independently."

### Section C — The action state machine (★ central diagram)

Render as a directed graph with these nodes and color-coded edges:

```
pending ──(promoter)──▶ sent ──(EA claim)──▶ claimed
                                                │
                                                ├──▶ executed   (order placed)
                                                ├──▶ failed     (broker reject)
                                                ├──▶ rejected   (max_positions / kill switch)
                                                └──▶ watching   (synthetic limit; price not yet in zone)
                                                         │
                                                         ├──▶ executed (price re-enters zone → market fill)
                                                         └──▶ rejected (expires_at passes; bot's watch sweeper)
```

Caption: *The lifecycle is a `CHECK` constraint in `schema.sql`. Every component agrees on these 5 terminal states and nothing else.*

### Section D — The four processes (responsibilities table)

Four side-by-side cards, each with: icon, name, runtime (asyncio / FastAPI / MQL5), responsibilities, what it OWNS in the DB.

| Process | Runtime | Owns | Talks to |
|---|---|---|---|
| **`listener.py`** | Python asyncio (Telethon) | inserts `actions` & `ALERT` rows; updates `signal_memory` | Telegram (user account) |
| **`bot.py`** | Python asyncio (python-telegram-bot) | runs `promote_due_actions`, `release_stale_claims`, `expire_stale_watches`; sends DMs | Telegram bot, DB |
| **`api.py`** | FastAPI on `127.0.0.1:8765` | only translates HTTP↔SQL — no business logic | EA over WebRequest |
| **`CopyTrades.mq5` EA** | MQL5 in MT5 | actually places, manages, partial-closes, reconciles trades | API + broker |

Highlight: *`api.py` is dumb on purpose — every business decision lives in either the orchestrator or the EA.*

### Section E — EA execution decision tree

When the EA picks up a `sent` action and runs `DoOpen`, it walks this tree (show as a flowchart):

```
Is current price ∈ [entry_low, entry_high]?
   ├─ YES → MARKET BUY/SELL at current price ──┐
   └─ NO  → ChasePriceEnabled?                 │
            ├─ YES, price past zone, remaining/orig ≥ 0.5 → MARKET at current price ──┤
            └─ NO  → SyntheticLimitEnabled?                                            │
                     ├─ YES → POST `watching` + `expires_at` → wait for price re-entry ┤
                     └─ NO  → BuyLimit/SellLimit at midpoint                           │
                                                                                       ▼
                                            Plan registered (RegisterPlan) iff ≥2 TPs
```

Annotate: *Chase fires when price ran past the zone but ≥50% of the original reward is still ahead — captures fast moves at the cost of worse entry.*

### Section F — 3-TP staged partial-close (★ second central diagram)

Show a price chart with a BUY entry @ E, three TP lines (TP1 < TP2 < TP3), an SL line. Animate with arrows three "stages":

- **Stage 0 — at TP1 hit:** close 1/3 of original lots, move SL → entry (Break-Even).
- **Stage 1 — at TP2 hit:** close 1/3 of original lots, move SL → TP1 (locks profit).
- **Stage 2 — TP3 is set on MT5 itself:** broker auto-closes the final third.

Caption: *State persisted to MT5 `GlobalVariables` per ticket, so an EA restart mid-trade resumes the correct stage. `RegisterPlan` dedupes by ticket — duplicate registrations would fire each stage twice and over-close.*

### Section G — Synthetic "watching" path

A small inline diagram:

- Signal arrives, price out of zone → EA POSTs `status='watching'` with `watch_json` and `expires_at`.
- Action sits in `watching` — neither executed nor failed.
- If price enters zone before expiry → EA market-fills, POSTs `executed`.
- If `expires_at` passes first → bot's `watch_sweeper_loop` flips it to `rejected`. **Authoritative even if EA is offline.**

### Section H — Reconciliation (DB ↔ MT5)

Show a small loop on `OnTimer`:

1. **History scan (48h)**: `HistorySelect` → for each `DEAL_ENTRY_OUT`, mark DB position closed.
2. **DB-authoritative pass**: `GET /positions?status=open` → for any ticket MT5 doesn't recognize → `POST /positions/{t}/close` with reason `mt5_not_found`.

Caption: *No throttling. Stale "open" rows would corrupt risk caps and the dashboard, so this must converge within one timer tick.*

### Section I — Live dashboard (the operator's view)

Render the canvas dashboard as it actually appears in MT5: 380×560px, left-anchored, hash-gated repaint. Sections to label:

- Header: equity, balance, day P&L
- Stats: signals received, executed, chased, rejected
- Open trades: per ticket — `#ticket BUY/SELL vol @ entry`, then per-TP line (`TPn price   +X.XX USD` with state glyph `* > -`), then `Total if all hit   +Y.YY USD`
- Last action: id, type, status, age

### Section J — Failure-mode glossary (small footer table)

| Symptom | Likely cause |
|---|---|
| Trade opens far above signal entry | `ChasePriceEnabled` fired between AI emit and EA fill |
| 2/3 of lots closed at TP1 instead of 1/3 | Duplicate `TradePlan` for one ticket; `RegisterPlan` now dedupes |
| Action stuck in `watching` forever | EA offline; bot's `watch_sweeper_loop` will mark `rejected` at `expires_at` |
| MT5 says position closed but DB still open | Reconciliation delayed; check `/positions?status=open` |
| Signal fired twice | Fingerprint band misconfigured (`FINGERPRINT_BAND_PRICE`, `FINGERPRINT_WINDOW_HOURS`) |

### Section K — Operational guardrails (small badge row)

Six pill-shaped callouts:
- 🛑 **Kill switch** (`/halt` Telegram command — only blocks promotion, not in-flight)
- ⏱ **Stale-claim release** (sweeper unsticks abandoned `claimed` rows)
- 🧠 **Magic number 919191** (EA only manages positions it owns)
- 📦 **WAL + idempotent migrations** (`db.connect()`, `init_schema()`)
- 🧾 **Rotating logs** (`logs/listener.log`, `bot.log`, `api.log`, `api_http.log`)
- 🪙 **0.01 max lots first 2 weeks live** (operator-imposed cap)

## 5. Concrete labels / strings to render verbatim

These are real values from the codebase — preserve them in the artwork:

- DB file: `copytrades.db`
- API origin: `http://127.0.0.1:8765`
- Polling: `GET /actions?status=sent` (every `PollIntervalSec` = 1s)
- Claim: `POST /actions/{id}/claim` (atomic CAS, returns 409 on conflict)
- Result: `POST /actions/{id}/result` with body `{status, legs, watch?, expires_at?}`
- Position close: `POST /positions/{t}/close {reason: "mt5_not_found" | "ai_close"}`
- Magic number: `919191`
- Default chase ratio: `ChaseMinRewardRatio = 0.5`
- Action statuses (exact tokens): `pending | sent | claimed | executed | failed | rejected | watching`
- Triage tiers (exact tokens): `ignore | context | signal | partial_signal`

## 6. Layout suggestion

```
┌──────────────────────────────────────────────────────────┐
│  HERO  (Section A)                                        │
├──────────────────────────────────────────────────────────┤
│  END-TO-END PIPELINE  (Section B — full width river)      │
├──────────────────────────────────────────────────────────┤
│  STATE MACHINE          │  FOUR PROCESSES                 │
│  (Section C, central)   │  (Section D, 4-card grid)       │
├─────────────────────────┴─────────────────────────────────┤
│  EA DECISION TREE       │  3-TP STAGED CLOSE              │
│  (Section E)            │  (Section F, with chart)        │
├─────────────────────────┴─────────────────────────────────┤
│  WATCHING PATH  │  RECONCILIATION  │  DASHBOARD MOCKUP    │
│  (Section G)    │  (Section H)     │  (Section I)         │
├──────────────────────────────────────────────────────────┤
│  FAILURE MODES (Section J) │ GUARDRAILS (Section K, pills)│
└──────────────────────────────────────────────────────────┘
```

## 7. Tone

Confident, technical, precise. Don't simplify away the SQLite-as-IPC choice — that's the most important architectural decision and the whole infographic should make a reader think *"oh, that's why this works without a queue."*

---

## Tip for using this prompt

- For Midjourney/DALL·E style raster generation, paste sections 1–3 + 6 + 7 (visual brief).
- For a diagram tool (Mermaid/Excalidraw/Figma AI), paste sections 4–5 (structural content).
- For a human designer, hand them the whole thing.
