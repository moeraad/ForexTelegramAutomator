# SaaS Discussion — CopyTrades Web Platform

Recreating CopyTrades as an online SaaS service. Decisions are appended here as we agree on them, point by point.

---

## Source feature list (from the desktop app)

1. Telegram channel watcher (Telethon)
2. Two-stage AI pipeline (triage → interpreter)
3. 12 action types (OPEN + management + legacy)
4. Hard invariants (single symbol XAUUSD, single open position, fully automated)
5. SQLite as sole coordination medium across 4 processes
6. Action lifecycle state machine (pending → sent → claimed → executed/failed/rejected/watching)
7. FastAPI HTTP bridge for the EA (port 8765)
8. MT5 Expert Advisor (staged closes, ATR trailing, chase-price, synthetic watch, reconciliation)
9. Telegram control bot (owner DMs, /halt, /cancel, promoter + sweepers)
10. Position-state context for the AI (volumes, partial counts, SL moves, last-closed, market heartbeat)

---

## Platform vision (context for all points)

- Operator (us) runs the Telegram listener accounts; we negotiate with signal-channel owners to add our reader account to their channels.
- **Provider role:** signal-channel owners get an account with a dashboard to manage their channel, see subscribers, and promote a "CopyTrades join link" to their followers.
- **Subscriber role:** end users register on the website, browse/subscribe to channels (via provider link or directly from a public catalog), get a dashboard, and connect their MT5 either via (a) self-hosted EA on their own PC pointing at our API, or (b) a paid managed VPS we provision for them.
- One operator-side Telegram identity ingests many channels in parallel; interpreted actions are routed to each subscriber's EA endpoint.

---

## Agreements

### Point 1 — Signal source ingestion (Telegram listener)

**Decision:** Operator-owned Telethon account(s) join provider channels. No per-user Telegram credentials.

**Architecture:**
- Start with a **single Telethon user account** for MVP (<50 channels).
- **Design around an account-pool abstraction from day one** so adding a 2nd/3rd account is config, not a refactor.
- One listener process multiplexes N Telethon clients over one event loop; each client filters on the chat IDs assigned to it.
- DB model: `signal_sources` table maps `provider_id → telethon_account_id → tg_chat_id`. Listener spins up one Telethon client per `telethon_accounts` row.

**Why not single-account forever:**
- Hard cap: 500 channels/supergroups per Telegram account.
- Single point of failure — one ban/lock takes every provider offline simultaneously.
- Rapid channel joining (>~20/day) trips Telegram's anti-spam heuristics; a pool lets us rotate which account joins new channels.
- Session file is high-value; compromise = read access to every provider's channel.

**Operational notes:**
- Phone numbers tied to real SIMs we control; losing a SIM = losing memberships on that account.
- Session files stored encrypted server-side.
- Inbound message throughput is not the bottleneck — downstream AI pipeline is.

### Point 2 — Two-stage AI pipeline (triage → interpreter)

**Inference billing model:** Platform-paid with per-tier subscription quotas.
- Triage calls (Haiku/nano) are folded into base cost — **not** metered against subscriber quota.
- Only interpreter calls count against the subscriber's monthly quota.

**Inference fan-out model:** Interpret once per channel message, fan out the resulting action to all N subscribers.
- Schema split: one canonical `signal_actions` row (channel-level, the AI output) + N `subscriber_executions` rows (per-subscriber lifecycle, filters, EA routing). Do not collapse these into one table.
- Per-subscriber personalization (filters, risk settings, lot sizing, kill switch, EA endpoint) is deterministic post-inference logic, not extra AI calls.

**Prompt architecture:** Layered — scaffold + profile + per-channel overlay. Not literally "one prompt."
- **Universal scaffold** (platform-owned): hard invariants, action schema, output JSON contract, state-context interpretation rules.
- **Language/asset-class profile** (platform-maintained, ~3–5 to start): e.g. `arabic-gold`, `english-forex`, `english-crypto`. Carries idempotency phrasing rules, shorthand decoding rules, commentary filters, cultural/linguistic specifics.
- **Per-channel overlay** (provider-supplied or AI-extracted): vocab table + 5–15 worked examples specific to that channel's style.
- **Vocab/example extraction is its own product feature** — provider onboarding tool that ingests historical messages and proposes a vocab profile + worked examples for review.

**Symbol & position model:** Multi-symbol, with a tightened invariant.
- Platform invariant: **one open position per (subscriber, symbol)**. Not single-position globally, not unrestricted.
- Keeps management actions unambiguous (implicit target = the singleton for that symbol) without forcing AI to disambiguate.
- `state_summary` block grows to "all open positions grouped by symbol"; accept the prompt-length cost.
- EA dispatch needs symbol+side disambiguation; no more `FindSingletonOpenTicket()` without a symbol arg.
- Lift the one-per-symbol restriction later only if real channel behavior demands it.

**Cost controls (multi-layered):**
1. **Per-channel daily budget** — when hit, escalate down the ladder below.
2. **Per-subscriber monthly cap** — quota tied to subscription tier.
3. **Platform-level circuit breaker** — global hourly spend ceiling; halts all interpretation and pages ops. Protects against runaway loops, prompt-injection attacks, billing surprises.

**Budget-exhaustion escalation ladder** (per-channel, in order):
1. **Downgrade model** (Sonnet → Haiku) — saves cost, accepts accuracy hit.
2. **Triage-only mode** — detect signals, emit `ALERT`-style notifications instead of actions. Subscribers see "signal detected, budget exhausted" rather than silence.
3. **Hard stop** — no more interpretation that day.

Provider and affected subscribers are notified at each escalation step.

### Point 3 — Action types

**Vocabulary scope:** Universal action schema, versioned. Providers cannot define custom action types — that would break the EA contract.

**MVP action set: 11 universal types.**

Core trade lifecycle (carried over, no `mt5_ticket` on management types — EA infers target via `(subscriber, symbol)` singleton):
1. `OPEN`
2. `MOVE_SL_BE`
3. `MOVE_SL`
4. `CLOSE_PARTIAL`
5. `CLOSE_FULL`
6. `REOPEN_LAST`
7. `REINFORCE`
8. `TIGHTEN_SL`
9. `ALERT` (also doubles as escape hatch for messages that don't fit a structured type — AI emits structured "manual review needed" with reasoning)

Pending-order types (new, included from day one for forex/multi-asset channels):
10. `PENDING_ORDER` — explicit limit/stop order placement (distinct from EA's chase/synthetic-watch logic)
11. `CANCEL_PENDING` — kill a pending order

**Dropped legacy types:** `MODIFY`, `CLOSE`, `CLOSE_ALL` — superseded by the management types; the ticket-vs-no-ticket inconsistency they introduced is gone in the greenfield rewrite.

**Deferred (add when a real channel needs them):**
- `MODIFY_PENDING` — change a pending order's trigger price
- `HEDGE_OPEN` — opposite-side open without closing existing (requires broker hedging mode)
- `TRAIL_SL` — explicit trailing-stop instruction (trailing is currently EA-side automatic for staged closes)

**Subscriber-side action filters (declarative, no user code):**
- Scope: per-(channel, symbol).
- Fixed toggle set: enable/disable each action type, lot multiplier, max lots cap, SL/TP override behavior.
- Filters can drop an action entirely or modify it (e.g., cap lots) — applied at fan-out time, after inference, before EA dispatch.
- No scriptable filters — too much support burden and a security risk.

**Schema versioning:** Action schema carries a `version` field. EA and API negotiate on connect; old EA versions get a deprecation warning and are pinned to the schema version they understand until upgraded.

### Point 4 — Hard invariants (SaaS edition)

**Symbol/position model** (settled in point 2):
- Multi-symbol supported.
- **One open position per (subscriber, symbol)** — management actions infer target via this singleton.

**Execution mode** (per-subscriber choice):
- **Default: fully automated.** Auto-promote every action after the configured delay (matches desktop-app behavior, matches channel intent).
- **Opt-in: per-action-type approval.** Advanced setting. Subscriber can require manual approval for high-risk types (OPEN, REINFORCE, PENDING_ORDER) while leaving low-risk management (MOVE_SL_BE, CLOSE_PARTIAL, etc.) on auto.
- Approval UX: dashboard inline button + Telegram bot inline keyboard. Action sits in `pending_approval` state with a configurable expiry (default 5 min for OPEN, longer for non-time-critical types). On expiry → `rejected` with reason `approval_timeout`.

**Tenant isolation (hard rule, shapes the entire schema):**
- No subscriber can see another subscriber's positions, executions, settings, EA endpoint, broker, or PnL.
- Providers see channel-level metrics (signal counts, AI accuracy, subscriber count) but **never** subscriber-level data — not PnL, not lot sizes, not broker identity, not EA URLs.
- Operator (platform admin) has read access for support; all access logged and auditable.

**Idempotency across fan-out:**
- One `signal_actions` row fans out to N `subscriber_executions` rows in a single atomic transaction.
- Listener/orchestrator restarts mid-fan-out must not produce duplicate executions. Use a `fan_out_complete` flag on `signal_actions` and on-restart reconcile: any row with `fan_out_complete=false` re-runs fan-out, which is itself idempotent (unique `(signal_action_id, subscriber_id)` constraint on `subscriber_executions`).

**EA endpoint is per-subscriber, opaque to provider:**
- Provider never sees subscriber EA URLs, broker account numbers, or which VPS a subscriber uses.
- EA authenticates to the API with a per-subscriber token, not a shared one.

**No retroactive execution on subscribe:**
- Subscribing at 10:30 does NOT replay an OPEN that fired at 10:25.
- Only signals emitted strictly after the subscriber's `subscribed_at` timestamp are routed to them.
- This is enforced server-side, not by the EA.

**Unsubscribe behavior (subscriber chooses):**
- **"Manage to close"** — open positions from that channel continue to receive management actions until they close naturally. New OPENs are not routed.
- **"Stop managing immediately"** — subscriber takes over manually; no further actions routed for any position from that channel.
- Default: prompt the subscriber on unsubscribe; do not pick silently.

**Provider mutation rules:**
- Provider can **pause** their channel (no new signals route to subscribers; existing positions keep being managed).
- Provider **cannot** recall, edit, or retroactively modify an already-emitted action. They can issue a follow-up action (e.g., immediate CLOSE_FULL) but the original is committed and audit-logged.

**Regulatory note (out of scope for engineering, but flagged):**
- Offering signal-copy-as-a-service may touch financial-advisor / money-management licensing in some jurisdictions. ToS language and execution-mode framing ("user-initiated, we never act as their agent") needs legal review before launch. Not a technical invariant — but it may shape one.

### Point 5 — Data layer & coordination

**Primary database:** PostgreSQL, managed (Neon or Supabase for fast start; migrate to RDS / Cloud SQL when scale demands).
- SQLite is off the table — multi-tenant concurrent writes from web, listeners, and many EA endpoints make it a non-starter.
- JSONB columns for action payloads (variable schema across action types).

**Tenant isolation pattern:** Single DB, shared tables, `tenant_id` columns + Postgres Row-Level Security policies.
- Pragmatic for tens of thousands of tenants.
- RLS gives defense-in-depth against bugs that forget a `WHERE tenant_id = ?`.
- Every table that holds tenant-scoped data carries `subscriber_id` and/or `provider_id`.
- Cross-tenant tables (channels, providers as entities) are gated by separate policies.

**Hot-path coordination:** Hybrid DB-as-queue + WebSocket push.
- Keep the action lifecycle state machine (`pending → sent → claimed → executed|failed|rejected|watching|pending_approval`) in Postgres — it's durable state and the contract is well-trodden.
- **Drop the polling model.** EAs connect via WebSocket; server pushes "new action available" notifications.
- EA still POSTs `claim`/`result` over REST — no need to make the WebSocket bidirectional for state mutations. Keeps the EA's HTTP code path largely unchanged.
- **Fallback:** if WebSocket drops, EA reverts to polling at a slower interval (e.g., 5s) until reconnect.

**Redis (managed — Upstash / ElastiCache):** required from day one.
- Web session storage (auth tokens).
- Per-channel and per-subscriber rate limits.
- AI quota counters (per-channel daily budget, per-subscriber monthly cap — burn down in Redis, periodic flush to Postgres for audit).
- WebSocket presence ("is this subscriber's EA online right now?").
- Idempotency keys for fan-out (prevent duplicate `subscriber_executions` on listener restart).

**EA-side reconciliation logic:** unchanged in shape.
- `GET /positions?status=open` + broker-side close mirroring carry over directly.
- EA doesn't know or care that the backend is Postgres instead of SQLite — just HTTP.

**Region strategy:** Single region for MVP (pick based on provider/subscriber geography — EU or US-East).
- Latency to subscriber VPSes matters for chase-price entries; 50ms vs 200ms is a real difference.
- Schema designed to be region-shard-friendly later (no global sequences; UUIDs or snowflake IDs over autoincrement).
- Multi-region added only when international scale demands it — do not prematurely build.

**Migration path from desktop schema:**
- 1:1 mapping where possible (`actions` → `subscriber_executions`, `positions` → `subscriber_positions`).
- New top-level entities: `providers`, `channels` (signal sources), `subscribers`, `subscriptions` (subscriber↔channel link), `signal_actions` (channel-level, pre-fan-out).
- Existing CHECK constraints on lifecycle states carry forward.

### Point 6 — Action lifecycle state machine

**Two-tier structure (matches the schema split from point 2):**

**`signal_actions`** (channel-level, 1 row per AI inference output):
- States: `interpreted → fanned_out → archived`
- Record of what the AI said. Fan-out is the only operation that mutates it.

**`subscriber_executions`** (per-subscriber, N rows per signal_action):
- Each row's lifecycle is independent — one subscriber's `executed` is another's `failed`.
- Fan-out creates rows in `pending` (auto subscribers) or `pending_approval` (opt-in approval subscribers).

**State diagram for `subscriber_executions`:**

```
                              ┌─ skipped_filter
                              ├─ skipped_kill_switch
pending ──┬──► sent ──► claimed ──┬─► executed
          │                       ├─► failed
          │                       ├─► rejected
          │                       └─► watching ──► executed | rejected (sweeper)
          │
          ├──► pending_approval ──┬─► sent (approved) ──► (as above)
          │                       └─► expired_approval
          │
          └──► expired_offline   (post-sent, EA didn't claim in time, OPEN-class only)
```

**Terminal states:** `executed`, `failed`, `rejected`, `skipped_filter`, `skipped_kill_switch`, `expired_approval`, `expired_offline`.

**New states vs desktop app:**
- `pending_approval` — opt-in approval gate (from point 4).
- `skipped_filter` — subscriber's declarative filter dropped the action (per-channel, per-symbol toggle).
- `skipped_kill_switch` — subscriber's personal halt is active.
- `expired_approval` — approval gate timed out (distinct from generic `rejected` for analytics).
- `expired_offline` — EA was disconnected past the per-action-type window.

**Quota-burned signals:** do NOT create a `subscriber_executions` row. Refuse upstream during fan-out. Signals can't be retroactively delivered when quota refills next month.

**EA-offline expiry policy (per-action-type):**
- `OPEN`, `PENDING_ORDER` — **2 min** expiry (time-sensitive entries).
- `MOVE_SL`, `MOVE_SL_BE`, `TIGHTEN_SL`, `CLOSE_PARTIAL`, `REINFORCE` — **30 min** expiry.
- `CLOSE_FULL`, `CANCEL_PENDING` — **never expire** (always want them to land on EA reconnect).
- `REOPEN_LAST` — **5 min** expiry (entry-like, but slightly more tolerant).
- `ALERT` — informational, no expiry concept.
- Values are platform-tunable; not subscriber-configurable in MVP.

**Watching state (carries over from desktop):**
- EA-managed synthetic limit while waiting for price to enter the entry zone.
- Server changes nothing about EA-side watching behavior.
- Watch sweeper is per-execution, not per-signal-action — each subscriber's watch expires independently.
- Sweeper flips expired watches to `rejected` with reason `watch_expired`.

**Audit trail (required from day one):**
- Append-only `execution_events` table.
- Every state transition writes a row: `{execution_id, from_state, to_state, reason, actor (subscriber|system|EA|admin), timestamp, payload_snapshot}`.
- Never updated, queryable per-tenant via RLS.
- Same pattern for `signal_actions` transitions (signal_events table) for provider-side analytics.

**Lifecycle contract enforcement:**
- Postgres CHECK constraint on `subscriber_executions.state`.
- Allowed transitions enforced via trigger (Postgres) or application-layer state machine library — pick one source of truth, don't split.
- Recommendation: application-layer enforcement (Python state-machine lib), CHECK only validates the enum value. Triggers are harder to audit and migrate.
