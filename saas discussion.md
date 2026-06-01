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

**Inference billing model:** AI cost is **per-channel, not per-subscriber** (corrected — see Point 11).
- Because inference runs **once per channel message** and fans out (point 2 fan-out model), a subscriber generates **zero** marginal AI cost. AI cost scales with `channels × message volume × model`, never with subscriber count.
- Therefore there is **no per-subscriber AI quota.** AI inference is metered **per channel** as a variable cost and recovered from the **provider** (deducted from their revenue-share payout). See Point 11.
- Triage calls (Haiku/nano) are cheap and fold into the channel's metered cost; the expensive interpreter is the bulk of it.

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

**Cost controls (CORRECTED — quality is never degraded):**
- **No quality-degradation ladder.** The earlier "per-channel daily budget → downgrade → triage-only → hard stop" ladder is **removed.** The best interpreter model runs on **every** signal, always. Interpretation quality is the product's core value and is never traded for cost.
- **Metered variable AI cost per channel** → recovered from the **provider** (deducted from payout). The provider controls channel chattiness, so they bear the variable cost. Aligns incentives: clean/dense channels = higher payout; the Channel Learning Loop lowers cost over time = higher payout (provider incentive to adopt it). See Point 11.
- **Platform-level circuit breaker — anomaly protection ONLY.** Global hourly spend ceiling that trips on **runaway loops / prompt-injection attacks / billing anomalies**, NOT on legitimate high load. It never degrades a normally-busy channel; it only halts genuinely abnormal spend and pages ops.
- **Per-subscriber AI quota: removed** (subscribers generate no marginal AI cost under fan-out — see corrected billing model above).

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

### Point 7 — HTTP/API bridge

**Service topology:** Monolith FastAPI for MVP, with route prefixes (`/v1/ea/*`, `/v1/api/*`, `/v1/admin/*`) and middleware designed so the EA API can be split into its own service later without a rewrite.
- EA endpoints are stateless (except for the DB) so they're independently scalable when the split happens.
- First split point will be EA-API vs everything-else — design for it now, execute later.

**Three caller classes, three auth models:**
- **EA → API:** per-subscriber long-lived API token (`Authorization: Bearer <token>`), generated when the subscriber connects their EA, revocable from the dashboard. Server-side revocable token table — NOT a JWT.
- **Web user → API:** hosted identity provider (Supabase Auth if using Supabase Postgres, else Clerk). Short-lived JWT + refresh. Don't roll your own user auth.
- **Admin → API:** separate path, SSO (Google Workspace), MFA mandatory, all access audit-logged.

**Rate limiting:** Redis-backed, middleware-applied, per caller class.
- EA API: per-token; heartbeat (15s) baked into the budget; claim/result/position-update bursts allowed.
- Web API: per-user, lower ceilings.
- Public/unauthenticated (catalog browsing pre-signup): per-IP, lowest ceilings.

**WebSocket layer (EA liveness — from point 5):**
- Plain WebSocket + JSON, not SocketIO.
- Auth on handshake (token), validated once, connection bound to subscriber.
- Server → EA messages are a **doorbell only**: `{type: "action_available", execution_id}`. EA fetches details via REST `GET /v1/ea/executions/{id}`. REST stays the source of truth.
- Server pings every 30s; missed pong = disconnect → falls back to slow polling until reconnect.
- Multi-pod: Redis pub/sub broadcasts `action_available` to whichever pod owns the EA's connection.

**EA base URL:** universal (`https://api.copytrades.io`), token-authenticated. No per-subscriber URLs.

**Versioning:**
- REST: `/v1/*`; breaking changes bump major, old version supported ≥6 months.
- WebSocket: `version` in handshake; server negotiates or rejects.
- Action schema version (point 3) evolves independently of API version.

**Idempotency:** `Idempotency-Key` header (UUID per logical op) on every EA write endpoint (result, position update, heartbeat). Cached in Redis 24h; retried POST with same key returns cached response, never double-executes. Non-negotiable for financial-state-mutating endpoints.

**Observability (day one):**
- Structured JSON logging to stdout (platform aggregates).
- OpenTelemetry distributed tracing across `signal_action → fan_out → subscriber_execution → EA claim → broker fill`.
- Metrics: per-endpoint latency/error-rate, live WebSocket connection count, fan-out lag (signal→EA-notify time).

### Point 8 — Trade execution (EA + MetaApi managed)

**Two execution tiers (hybrid), mapped to pricing:**

| Tier | Execution path | User effort | Execution COGS to us |
|---|---|---|---|
| **Self-hosted** | Ship precompiled `.ex5` EA; user installs on their PC/VPS | Install + one-time WebRequest whitelist | ~$0 |
| **Managed** (upsell) | MetaApi cloud; server-side execution engine drives it | Enter MT5 login + password + broker server, done | ~$11–14 / account / month (see pricing) |

**Unified executor architecture (write-once, not twice):**
- **One broker-primitive interface:** `open_market`, `open_pending`, `modify_sl`, `modify_tp`, `close_partial`, `close_full`, `cancel_pending`, `query_positions`, `query_symbol_spec`.
- **Two adapters:** `MetaApiAdapter` (calls MetaApi REST), `EAAdapter` (sends the same primitive as a command to the self-hosted EA over WebSocket; EA executes locally and reports back).
- **All signal-driven actions** (OPEN, every management type, pending orders) are decided upstream by the AI → emitted as ONE primitive → carried out by whichever adapter is attached. **Written once.** Matches the existing "`api.py` is dumb" philosophy.
- **Autonomous price-reactive policy** (chase-entry, staged-close-on-TP-hit, trailing SL) is the ONLY genuinely duplicated logic — because it fires without a new signal and must react to live ticks.
  - Expressed as a **shared declarative plan schema** (the existing `g_plans[]` / `TradePlan` model already works this way — plan is data, loop interprets it). The schema is the shared contract.
  - **Two small interpreters:** MQL5 `ManagePlans()` for the EA path; a Python interpreter for the MetaApi path (runs on our server against MetaApi's streamed quotes). Bounded duplication (~few hundred lines), stable once tuned.

**Where the "brain" lives, and failure modes:**
- **Self-hosted EA:** keeps its full local autonomous brain (plans cached in MT5 `GlobalVariables`, reacts to local ticks). Only needs our server to *receive new signals*. **Survives a network partition** — keeps trailing / taking staged profit locally while offline.
- **MetaApi managed:** the autonomous brain runs in **our server-side Python**, fed by MetaApi's price stream. Manages trades identically when healthy. ⚠️ If our server / MetaApi connection drops, the **soft** logic (trailing, partial-at-TP) pauses until reconnect.
- **Safety rule for BOTH paths:** always push **hard SL and final TP as native broker orders**. Worst case during any outage, the position rides to a real broker-side SL/TP and is **never naked** — only the soft optimization layer is lost.

**Broker/symbol heterogeneity:**
- EA path: EA **auto-discovers** the broker's symbol naming (`XAUUSD` vs `GOLD` vs `XAUUSD.m`), min/step lots, stop levels, filling mode on attach; reports a capability manifest to the server.
- MetaApi path: same discovery via MetaApi's symbol-spec API.
- Subscriber can **override** a symbol mapping in the dashboard if auto-discovery guesses wrong.
- Fan-out translates the canonical signal symbol → the subscriber's broker symbol and validates lots before dispatch.

**Lot sizing:** Offer **fixed-lot** and **risk-percent** (compute lots from SL distance + account balance) from the start. Server-computed where possible; executor validates against broker min/step.

**EA versioning:** version-gate on connect, support N-1, nudge upgrades in dashboard, **never hard-break a working EA** without a long deprecation window (MQL5 has no auto-update).

**EA-local hard caps:** subscriber-set backstops the server cannot override ("never more than X lots", "never more than Y open trades") — protects against a server bug / compromise fanning a bad action to everyone.

**Reconciliation:** EA path keeps `ReconcileClosedPositions` (broker = source of truth). MetaApi path gets continuous pushed position/account state (reconciliation is easier — no polled scan needed).

**Managed-tier sequencing note:** the MetaApi/Python execution engine is *easier to build and test* than the EA (no MetaEditor, no Windows). Consider **building managed-first** and adding the self-hosted EA afterward for cost-sensitive users.

#### Verified MetaApi pricing (live-checked 2026-05-30, https://metaapi.cloud/#pricing)

Pay-as-you-go, **billed per trading account per hour of deployment** (~730 hrs/month for 24/7). Prices exclude VAT/GST.

**Use the G2 tier** — it is both the recommended high-reliability infrastructure AND cheaper than G1 (G1's CopyFactory is ~$115/account/mo; avoid).

G2 ("Cloud offering g2") rates:
- Deployed (active) account hosting: **$0.012 / account / hour** → ~$8.76/mo at 24/7
- Account deployment: $0.072 / deployment (occasional, negligible)
- Undeployed (inactive) hosting: $0.00105 / account / hour
- Adding a trading account: $2.10 / unique account / month
- **MetaApi trading API itself: Free** (you pay for hosting the deployed account, not per API call)
- CopyFactory API (optional — we build our own fan-out, so likely skip): $0.001575 / account / hour
- MetaStats API (optional analytics): $0.001575 / account / hour
- Risk Management API (optional): $0.00315 / account / hour
- Trading-account replica discount: 50% (read-only replicas)

**Estimated all-in COGS per managed subscriber (G2, 24/7, our own execution engine, no CopyFactory):**
- Hosting: ~$8.76 + add-account $2.10 = **~$10.86 / account / month**
- With CopyFactory if used: ~$12 / month
- With MetaStats too: ~$13–14 / month

**Business subscription:** for >250 accounts, custom infrastructure, volume discounts, SLA — negotiate down at scale.

**Pricing implication for the managed tier:**
- COGS floor is **~$11–14/account/month** (much lower than the earlier ~$60 guess — corrected by live data).
- A managed tier at even **$25–40/month** carries healthy margin over MetaApi COGS + AI inference.
- Self-hosted tier stays ~$0 execution cost → anchors the affordable/entry plans; managed is a clean premium upsell. Validates the hybrid tiering.

**Reliability caveat:** MetaApi Trustpilot reviews are mixed (reports of disconnects / missed trades) — reinforces the "always native broker SL/TP" safety rule and the G2 high-reliability choice.

### Point 9 — Control bot, background workers & notifications

**Tear the background machinery out of the bot.** In the desktop app the promoter + sweepers were bolted onto `bot.py` only to avoid a 4th process. That rationale is gone in SaaS.
- **Promoter** (`pending → sent`, fan-out, approval-timeout) → dedicated horizontally-scalable worker loop.
- **Sweepers** (stale claims, stale management, watch expiry, EA-offline expiry) → scheduled jobs (Celery beat / APScheduler / cron-like).
- These are core platform services, NOT "the bot" anymore.

**Control-surface hierarchy:** Web dashboard is the **primary, authoritative** control surface. Telegram is **one opt-in notification + quick-action channel** among several (email, web push, mobile push, SMS for critical). Subscriber chooses channels.
- The "approve this OPEN from your phone" flow (point 4 approval mode) is the prime Telegram quick-action use case.

**Bot identity:** **One platform bot** (e.g. `@CopyTradesBot`), many subscribers.
- Account-linking via signed deep-link: `t.me/CopyTradesBot?start=<link_token>` ties a Telegram `chat_id` to a platform account.
- Notifications routed by looking up the linked `chat_id`.
- Per-provider white-label bots deferred (post-MVP feature).

**Keep the inbound/outbound Telegram split** (carries over from desktop, same rationale):
- **Inbound (listener):** operator's Telethon **user account** reading provider channels (point 1).
- **Outbound (control bot):** platform's **bot account** DMing subscribers.
- Two different identities, two libraries (Telethon vs python-telegram-bot), two processes. Do not merge — keeps user-account rate limits isolated from bot polling.

**Role-specific controls:**
- **Subscriber:** personal kill switch (stops only their executions), cancel in-flight action, approve/reject (approval mode), pause a specific subscription (keep others), status (open positions, today's executions, quota remaining). Dashboard + mirrored Telegram quick-actions.
- **Provider:** pause channel (stops emission to all subscribers), channel stats (signal count, subscriber count, AI accuracy — **no subscriber PII**, per point-4 isolation), manage vocab/example profile (point 2). **Web-first** — providers manage a product, not phone-react to trades.
- **Operator:** platform-wide circuit breaker (point 2), per-channel / per-account halts, Telethon account health (alive / challenged / banned). **Admin console** + critical alerts pushed to ops (PagerDuty / Telegram / Slack).

**Kill-switch semantics (carry over the honesty):**
- Stops new promotion to the subscriber **and** cancels any not-yet-claimed executions → transition to `skipped_kill_switch`.
- **Cannot recall** an execution the EA already claimed/executed. Surface this limitation clearly in the UI.

**Notification service (dedicated, event-driven):**
- Consumes events (`execution.executed`, `approval.required`, `quota.exhausted`, `channel.paused`, …) and dispatches to each subscriber's chosen channels.
- **Not inline in the request path** — queue/worker based.
- Rate-limit-aware (Telegram bot send limits ~30 msg/sec; batch/throttle accordingly).

### Point 10 — Position-state context for the AI

**Central reframe (the key decision):** Because inference is channel-level (point 2) but state was per-user (desktop), they no longer line up. Resolution:
- **The AI interprets the message into an *intent* against a *channel-canonical* position model.**
- **Per-subscriber idempotency moves OUT of the prompt and INTO deterministic code guards** evaluated at fan-out.
- Rejected alternatives: per-subscriber inference (kills the point-2 efficiency win); channel-level with no idempotency (loses AI nuance like "النصف الثاني" = second half must still fire).

**Channel-canonical position model:**
- Maintain a **virtual "channel position"** derived purely from the channel's own signal stream (OPEN → management → CLOSE), independent of any subscriber.
- This is what populates the SYSTEM STATE block in the prompt: "channel opened XAUUSD buy, moved SL to BE, took one partial."
- Cleaner than the desktop app, where the single user's real position doubled as the channel's notion of state. Splitting them is correct.

**Per-subscriber idempotency guards (deterministic, post-inference, per `subscriber_execution`):**
- `CLOSE_PARTIAL` + subscriber `partial_close_count ≥ 1` + not "second half" intent → `skipped_filter` (reason: `already_partialed`).
- `MOVE_SL_BE` + subscriber already at BE → skip.
- `MOVE_SL` within ε of subscriber's current SL → skip.
- `REOPEN_LAST` / management + subscriber has no matching history with this channel → `skipped_filter` (reason: `no_subscriber_history`).
- These are exactly today's prompt idempotency rules, re-expressed as deterministic per-subscriber checks. The AI supplies the semantic hint (reminder vs fresh instruction; explicit "second half"); the code decides skip-or-fire per subscriber.

**Market price — one platform reference price per symbol (not N broker prices):**
- AI shorthand decoding ("ستوبك 56" → 4856) needs only *approximate* mid to disambiguate magnitude — not any subscriber's exact broker price.
- Maintain **one platform-level reference price per symbol** for the prompt.
- Source: a dedicated market-data feed (preferred, robust) or median of recent EA/MetaApi heartbeats per symbol (fallback).
- The subscriber's *exact* broker price is used only at **execution** time by their executor (which has live local price).
- Decouples "AI needs approximate price to read the message" from "executor needs exact price to fill." Kills the N-heartbeats-into-prompt problem.
- Staleness: per-symbol, `>60s` old → `STALE`, prompt told not to guess shorthand (carries over).

**Last-closed / REOPEN_LAST / REINFORCE:**
- **Channel-canonical** last-closed-within-24h + its originating signal drives the *intent*.
- Per-subscriber guard then checks whether *this* subscriber actually has a matching last-closed position to reopen.
- A subscriber who joined 1h ago has no history with the channel → REOPEN_LAST → `skipped_filter` (`no_subscriber_history`).
- This naturally enforces the point-4 invariant "no retroactive execution on subscribe" — new subscribers simply have no per-subscriber history to act on, even when channel canon says "reopen."

**Cost / caching angle (ties to point 2):**
- Channel-canonical state changes only when the channel emits a signal → stable between messages → preserves prompt-cache hit rates on the cached prefix.
- Had per-subscriber state gone into the prompt, caching would be destroyed (different per subscriber). So this decision also protects inference cost.

---

## Status: all 10 source features discussed and locked.

---

# Net-new SaaS-only items

Concerns with no desktop-app equivalent, surfaced during the discussion. Worked through the same way.

**Backlog:** billing/subscriptions (✅ Point 11), managed provisioning flow (✅ Point 12), provider onboarding & revenue share (✅ Point 13), vocab/example extraction onboarding tool (✅ Point 13B), public channel catalog & discovery (✅ Point 14), legal/ToS & regulatory framing (✅ Point 15), multi-tenant Channel Learning Loop (✅ Point 16), admin console (✅ Point 17). **— ALL NET-NEW ITEMS COMPLETE.**

### Point 11 — Billing & subscriptions

> Visual companion: `saas-billing-visual.html` (open in browser).

**Marketplace model:** providers set their own channel price; platform takes a flat commission (~25%, revisit with volume tiers later).

**Hybrid subscription unit — a subscriber pays two things:**
- **A) Platform access fee** (platform-priced) — funds the costs *we* bear: execution tier + dashboard/infra/support. We control this number → margin protected. **Kept by platform.**
- **B) Per-channel subscription** (provider-priced) — one per channel copied. Split **~75% provider / ~25% platform**.

**Platform-fee tiers (structure locked; dollar amounts illustrative). Differentiated by EXECUTION + channel count + features — NOT AI quota:**

| Tier | Execution | Channels | COGS to us | Illustrative price |
|---|---|---|---|---|
| **Free / Trial** | Self-hosted EA | 1 | ~$0 | $0 |
| **Self-hosted** | Self-hosted EA | Multiple | ~$0 (execution) | $15–25/mo |
| **Managed** | MetaApi cloud | Multiple | ~$11–14/acct/mo | $40–60/mo |

Provider channel subscriptions (B) are charged **on top** of the platform tier.

**AI inference cost — metered, variable, provider-borne (quality NEVER degraded):**
- AI is a **per-channel** cost (fan-out → subscribers generate no marginal AI cost). Not a per-subscriber quota.
- Best interpreter model runs on **every** signal — **no degradation ladder** (removed from point 2).
- Metered per channel, **deducted from the provider's payout**: `provider payout = (~75% of channel subs) − (metered AI cost)`.
- **Decision: provider bears it** (chosen over platform-absorbed). Rationale: provider controls channel chattiness; keeps platform's 25% margin clean; cheap triage gate filters noise first so metered cost tracks real signal volume; gives providers a direct incentive to keep channels clean AND to adopt the Channel Learning Loop (CLL lowers interpreter cost → raises their payout).
- **Platform-level circuit breaker retained for ANOMALY protection only** (runaway loop / prompt-injection / billing anomaly) — never trips on legitimate load.

**Payment processor: Stripe + Stripe Connect.**
- Stripe: subscriptions, proration, Stripe Tax, dunning, customer portal.
- **Stripe Connect (Express)** for provider payouts: KYC/AML + automated revenue-share transfers. Platform **never holds funds** → avoids money-transmitter licensing.

**State & lifecycle:**
- **Stripe = billing source of truth, mirrored to Postgres via webhooks** (`customer.subscription.updated`, etc.) for fast per-request access checks. Tier/execution-mode/channel-access derived from the mirror.
- **Cancellation undeploys the MetaApi account immediately** → stops the hourly meter (no stranded monthly cost; MetaApi bills hourly).
- **Failed payment → degrade gracefully** (Stripe dunning): manage-to-close existing positions, never abandon an open trade because a card expired (mirrors point-4 unsubscribe behavior).

**Provider economics & transparency:**
- Provider dashboard: subscriber count, channel MRR, their share, **metered AI cost line**, net payout, payout history.
- **Aggregate only — no subscriber PII** (per point-4 isolation).
- Flat commission for MVP; volume-tiered commission (lower % for high-MRR channels) later to retain top providers.

### Point 12 — Managed provisioning flow (MetaApi credential capture & custody)

> Highest-security-weight item: these credentials carry **trade authority over real money.**

**What's captured:** MT5 `login` + **master/trade password** (read-only investor password can't trade — copy trading requires master) + **broker server name**. No way around capturing trade-authority creds.

**Credential custody — THE load-bearing decision: MetaApi is the custodian, not us.**
- POST `{login, password, server}` to MetaApi **once**; MetaApi stores them and returns `account_id` + access token.
- **We store only `account_id` + token — NEVER the broker password.**
- Consequence: a breach of *our* database leaks **no broker passwords** (they're not there). Credential custody is delegated to a vendor whose business is holding them. Liability surface shrinks dramatically.
- Rejected: self-storing encrypted passwords and re-sending on each deploy (makes us a credential custodian — bigger attack surface + liability).

**Password transit (browser → our API → MetaApi):** server-side **in-memory pass-through only**.
- **Never logged** — explicitly scrub credential payloads from request logs, error traces, APM. ⚠️ The desktop app's "422 handler persists raw body" habit is DANGEROUS here — must exclude credential payloads.
- **Never persisted** — received in-memory, forwarded to MetaApi, discarded. No DB / disk / queue write.
- TLS end-to-end (given, point 7).
- Enforced as a **tested invariant**: a test asserts the password string never appears in logs.

**Encryption of what we DO store:** the MetaApi token *is* trade authority.
- **KMS-backed envelope encryption** (AWS/GCP KMS or Vault) at the application layer — a DB dump is useless without the KMS key. Not just DB-level at-rest.
- Per-tenant/per-record data keys ideally (one leaked key ≠ global compromise).
- The broker password needs no at-rest story because it's never stored (the whole point of the custody decision).

**Provisioning flow (happy path):**
```
1. Subscriber picks Managed, enters {login, master password, broker server}.
2. Browser → our API over TLS (in-memory only).
3. Our API → MetaApi provisioning: create account.
4. MetaApi validates by connecting to the broker → returns account_id + token (or error).
5. Store account_id + KMS-encrypted token. Discard password from memory.
6. MetaApi deploys account (hourly meter starts).
7. Server-side execution engine goes live for this subscriber.
```
- **Validate-then-bill:** managed billing starts ONLY after MetaApi connects successfully. Bad creds fail fast, cost nothing.
- **Reduce broker-server friction:** server-name lookup / fuzzy-match / `.srv` upload — users often don't know their exact server name; onboarding stalls here otherwise.

**Managed-account lifecycle** (parallel to subscription state):
`provisioning → connected → deployed (trading) → undeployed (paused/cancelled) → failed/disconnected`.
- **Broker disconnect** (user changed password, broker maintenance, margin call): surface to subscriber **immediately** + alert (silent disconnect = missed trades = churn). Ties to point-9 notifications.
- **Credential rotation:** "re-enter credentials" flow updates the MetaApi account when a user changes their broker password.

**Consent, disclosure, revocation:**
- **Explicit, versioned, timestamped consent** before capture: "You authorize CopyTrades + MetaApi to place trades on this account. Credentials are stored by MetaApi, not CopyTrades. Revoke anytime." Logged for audit.
- **One-click revoke = undeploy + DELETE the MetaApi account** → purges creds at MetaApi too. Don't just stop trading; actually delete.
- **Broker-ToS attestation** (point 8): some brokers prohibit third-party API/cloud trading; warn + user attests.
- Consent language needs **legal review** (intersects the backlog legal/ToS item).

**Blast-radius containment (managed has NO EA-local backstop):**
- **Server-side per-account hard caps** enforced by the execution engine regardless of signal: max lots, max open trades, max daily loss. The managed analog of point-8's EA-local caps — *more* important because there's no second line of defense on a user machine.
- **Native broker SL/TP always** (point-8 safety rule) — ultimate backstop.

**Compliance posture (not MVP-blocking, but design for it):**
- Minimize-hold (custody decision does this) + encrypt-what-we-must + log-all-access + document-the-data-flow.
- Anticipates SOC 2 (serious customers will ask), GDPR (credentials = personal data). Documenting the data-flow now avoids a rewrite at audit time.

### Point 13 — Provider onboarding & revenue share

**Onboarding gate:** **Invite / application + manual approval** for MVP (evolve to hybrid self-serve + automated checks later).
- The reader-account-must-join step (point 1) is already a manual touchpoint — lean into it as a quality gate.
- Rationale: a scam/money-losing channel is an existential reputational risk; vet before ingesting. (Also: every ingested channel is circuit-breaker risk even though AI cost is provider-borne.)

**Onboarding sequence:**
```
1. Provider applies (channel link, asset class, language, sample messages).
2. Operator vets + approves → provider account created.
3. Operator adds the Telethon reader account to the channel; verify ingestion (messages arriving, chat_id bound).
4. Provider completes profile: name, description, price, asset/language profile pick.
5. Vocab/example extraction (Point 13B) runs on history → review → profile goes live.
6. Stripe Connect Express onboarding (KYC/AML) → payout eligibility.
7. Channel published to catalog (or kept unlisted/invite-only — provider's choice).
```

**Channel-ownership verification:** **post-a-code** — platform generates a one-time code; provider posts it in their channel; the listener confirms it appeared. Proves control. (Reader-account join stays operator-side since it's a Telethon *user* account, not a bot.)

**Revenue-share mechanics (builds on Point 11's ~75/25 + provider-borne metered AI cost):**
- **Payout:** monthly, **minimum threshold ~$50** (sub-threshold rolls over) to avoid micro-transfers.
- **Chargebacks:** a subscriber chargeback claws back the provider's already-paid share — **absorbed by the provider's future payouts** (Connect negative-balance handling). Policy recorded.
- **Tax forms:** Stripe Connect collects W-9/W-8/1099 automatically — don't build this.
- ⚠️ **Geographic payout-eligibility gap:** Connect Express isn't available in all countries; some international providers can't receive payouts. Needs a fallback (manual wire / "not eligible" gating). Flagged early.

**Post-onboarding accountability:**
- **Performance transparency:** per-channel win rate, signals/day, AI accuracy — surfaced to provider + (later) catalog.
- **Probation / suspension:** consistently money-losing or malformed channels can be paused. Manual trigger for MVP; metric-driven later.
- **Churn signal:** high churn on a channel flags a quality problem.

**Hard isolation (recap, point 4):** providers NEVER get subscriber PII, PnL, lot sizes, or broker identity. Aggregate metrics only. Onboarding must never promise otherwise.

### Point 13B — Vocab/example extraction onboarding tool

> The feature that turns "hardcoded Arabic-gold prompt" into "any channel self-configures." Real IP. (Named in point 2; designed here.)

**Output:** the **per-channel overlay** (point 2's bottom prompt layer) — vocab table (channel phrase → action_type) + 5–15 worked examples — plus a **recommendation for which language/asset-class profile** (middle layer) to attach.

**Engine reuse — THE key decision: reuse the Channel Learning Loop engine, don't build a parallel tool.**
- Onboarding extraction = CLL in **"cold-start eager mode"** (run once on history, immediately).
- Continuous CLL = **"warm mode"** (the existing rolling `learning_batch_n` trigger).
- Same machinery: `channel_learner` greedy cosine clustering by category, embeddings, per-cluster LLM synthesis, replay-for-evidence.

**Pipeline:**
```
1. Ingest N historical messages (~500–2000).
2. Triage + cluster to find signal-bearing messages (reuse triage model + channel_learner clustering).
3. Per cluster, interpreter (one-time onboarding cost) proposes: phrase→action_type vocab + representative worked examples.
4. Detect language + asset class → recommend the profile.
5. Present everything for REVIEW.
```

**Human-in-the-loop, propose-only** (mirrors CLL safety contract — never auto-activate):
- Provider edits/approves vocab + examples via a friendly UI ("when the channel says X, we do Y — correct?").
- Provider touches ONLY the channel overlay — never the platform scaffold (top layer).
- **Review ownership:** operator validates extraction during the manual onboarding (already in the loop); provider refines afterward. Quality gate + provider autonomy.

**Cold-start (little/no history):**
- Attach the **language/asset-class profile only** (safe default — reasonable interpretation on its own).
- CLL continuous mode builds the channel overlay as real messages accumulate.
- Extraction is best-effort; profile-only is the floor.

**Per-channel replay gate before activation** (reuse the desktop `test_management_replay.py` safety-net pattern):
- The provider-approved worked examples ARE the test set.
- If prompt + overlay can't reproduce the approved examples, the overlay isn't ready to go live.

### Point 14 — Public channel catalog & discovery

> Subscriber-acquisition surface. The load-bearing tension is **what performance data is safe/honest to show**, not UX.

**Performance display — THE consequential decision: show hypothetical *signal* performance, NEVER real subscriber money.**
- Two possible numbers: (a) the channel's **signal** performance (hypothetical — "if you'd taken every signal at stated entry/SL/TP"), computable from our `signal_actions` history; (b) aggregated **real subscriber** P&L (varies by broker/slippage/lot/join-time, exposes "advertised X got Y" disputes, brushes tenant-isolation + privacy).
- **Decision: (a) only.** Lead with **reliability/consistency metrics — win rate (TP before SL), avg R:R, max drawdown of the signal series, longevity, signals/day, consistency.**
- **No dollar/percentage profit headline.** A return headline is a financial performance claim → legal liability + incentivizes risky channels.
- Every figure labeled: **"based on the channel's published signals — hypothetical, not real returns, past ≠ future."** Non-negotiable.

**"Independently tracked / Verified by CopyTrades" — the core differentiator AND legal shield:**
- Stats are computed by us from signals **our own listener ingested** → providers can't fake them (unlike screenshot-faked Telegram channels).
- **Minimum track record** to list publicly (e.g., 30–90 days of ingested signals) — blocks brand-new channels claiming greatness.
- Market the catalog around "independently tracked," not "high returns."

**Ranking & discovery:**
- **Filters:** asset class, language, signals/day (active vs occasional), track-record length, risk profile.
- **Sort:** track-record length, win rate, popularity, recency.
- **Ranking = transparent multi-factor quality score** (consistency, longevity, drawdown control, **retained-paying** subscriber count). **NEVER a pure-return leaderboard** (reads as a return promise + rewards recklessness).
- **Anti-gaming:** weight subscriber count by *retained paying* (not free-trial); show R:R alongside win rate (defeats tiny-TP-scalp gaming).

**Trust & safety:**
- **Standardized risk label** on every channel page (from signal drawdown/volatility) — subscriber sees "high risk" before subscribing.
- **Reviews/ratings deferred post-MVP** (gameable, moderation-heavy) — launch with objective platform-computed stats only.
- Moderate or disallow unverifiable free-text provider claims.

**Subscribe-from-catalog flow:**
```
Browse → channel page (verified stats + disclaimers + risk label)
      → Subscribe → confirm platform tier (execution) if unset
      → pay channel price + platform fee (Stripe)
      → connect execution (EA token or MetaApi creds) if first subscription
      → signals route only AFTER subscribed_at (point 4: no retroactive execution)
```
- **Free trial: provider opts in** (a channel-level trial costs the provider AI inference per point 11 with no revenue — so it's their choice, not platform-forced).

**Listing visibility (provider-controlled):** **public** (catalog-listed) / **unlisted** (link-only) / **invite-only**. Matches both acquisition paths (provider promotes own link OR catalog discovery). Public listing requires min track record + approved profile + verified stats; unlisted/invite can go live sooner.

**Legal framing baked into the UI (pre-figures the legal item):**
- Persistent disclaimers on every channel page + at subscribe-time: "CopyTrades tracks and relays signals; not investment advice; trade at your own risk; past signal performance is hypothetical and not indicative of future results."
- **Geographic gating** — some jurisdictions prohibit this outright; may need to geo-block listings/subscriptions in certain countries. Flagged for the legal item.
- Disclaimers + risk label + "tracked, not advice" are **hard UI requirements**, not afterthoughts.

### Point 15 — Legal / ToS & regulatory framing

> ⚠️ **NOT legal advice — engineering/product framing only.** This is the one item that genuinely requires a qualified fintech lawyer in EACH launch market. "Structure the problem + defensive posture," not "you're now compliant."

**Core question — what are you, legally?** Everything flows from this:
- **(a) Pure technology / relay tool** — "we ingest public signals and relay them; the user decides and authorizes; we never advise, never manage funds." **← target posture (lowest burden).**
- **(b) Investment adviser** — if seen as *recommending* trades (regulated).
- **(c) Asset / money manager** — if exercising *discretionary* authority over client accounts (heaviest regulation: portfolio licensing, custody rules).
- **Biggest risk:** the fully-automated managed tier (points 8/12) — server executes on a credential — **drifts toward (c)**. Auto-execution on held/relayed credentials looks like discretionary trading authority. The EU has specifically scrutinized copy trading under MiFID II.

**Defend posture (a) by design (product choices with legal weight — most reinforce locked decisions):**
- **User-initiated, user-authorized execution** — explicit opt-in to auto-copy; halt/cancel anytime (point 9); user sets own risk caps (points 8/12). "You configured an automation tool," not "we manage your account."
- **No platform discretion** — we never decide *what* to trade; we mechanically relay a provider's signal. The **AI is a translation layer, not a recommendation engine** — document this.
- **No fund custody** (Stripe only, point 11) + **no password custody** (MetaApi holds creds, not us, point 12). Custody is the bright line into the worst regimes — stay clear.
- **Provider-is-the-source framing** (point 14) — we relay *their* calls; distances us from being the adviser.
- Caveat: even with all this, **some regulators treat auto-execution copy-trading as regulated regardless of framing.** Validate per market.

**Consolidated flags (rolled up from points 4, 8, 12, 13, 14):**
- **Geo-gating (decision):** launch in a **deliberately limited set of counsel-cleared jurisdictions; geo-block the rest**; expand market-by-market. Do NOT launch global and hope.
- **Credential consent** (point 12): legally-reviewed, versioned, logged authorization language.
- **Payout / KYC-AML** (point 13): Connect handles much; AML may still attach to us depending on classification.
- **Disclaimer regime** (point 14): "hypothetical, not advice, past ≠ future."
- **Provider agreement:** warrants channel ownership, signal legality (no pump-and-dump / insider), indemnifies us, independent contractor (not partner/employee).

**Document set required before public launch:**
- **ToS** (subscribers) — tech tool, own-risk, not advice, liability cap, arbitration, jurisdiction limits.
- **Provider Agreement** — ownership/legality warranty, revenue-share, independence, indemnification, termination.
- **Privacy Policy** — GDPR/CCPA; subprocessors (MetaApi, Stripe, Anthropic/OpenAI, Telegram). Good story: we do NOT hold broker passwords (point 12).
- **Risk Disclosure** — standalone, prominent, acknowledged at signup (high-risk, leverage, total-loss possible).
- **DPA / subprocessor list** — GDPR.
- **Cookie/consent** — web compliance.

**AI-specific legal surface (often missed):**
- **AI misclassification liability** — AI can misread a message → wrong trade. ToS disclaims: best-effort interpretation, no accuracy warranty, user accepts automation risk. (Desktop CLAUDE.md's "AI accuracy is the only safety net" is now a *contractual* risk.)
- **Right to send channel content to AI vendors** (Anthropic/OpenAI) — granted in provider agreement, disclosed in privacy policy.
- **Right to use channel messages for the Channel Learning Loop** — covered in provider agreement.

**Liability & structure:**
- Limitation of liability (cap to fees paid; exclude consequential/trading losses), arbitration + class-action waiver.
- Professional/tech **E&O insurance** (may be hard/expensive for trading-adjacent fintech — budget for it).
- Operate through a **properly-formed entity**; don't personally hold the risk (counsel + accountant).

**MVP posture (pragmatic):**
- Pick **1–2 launch jurisdictions**, geo-block the rest.
- **Engage a fintech lawyer BEFORE taking real money** — non-deferrable; regulators can shut down + claw back.
- Build to support posture (a) regardless (the design choices cost little, good practice anyway).
- All six documents drafted/reviewed before public launch; in-app disclaimers (points 12/14) are their UI surface.
- Document the "no funds / no passwords held" architecture — doubles as compliance artifact (point 12 SOC 2) + regulatory-posture argument.

### Point 16 — Multi-tenant Channel Learning Loop

> Evolution of the existing CLL + Point 13B extraction engine, reasoned across hundreds of channels/providers.

**Isolation boundary — THE consequential decision: strictly per-channel.**
- Each channel's CLL learns ONLY from its own corpus, writes ONLY to its own overlay. Total isolation for MVP.
- Rejected for now: free cross-pollination (one channel's quirk poisons others; uses Provider A's data to improve Provider B — tenant-isolation + IP problem).
- **Future, guard-railed path: operator-only, anonymized profile-level promotion** — a genuinely general improvement (e.g., better arabic-gold shorthand decoding) can be proposed for the *shared profile* layer that helps all channels of that class. Operator-reviewed, anonymized, never automatic, never injects one provider's specific examples into another's overlay.

**Review ownership (mirrors Point 13B):**
- **Channel-overlay suggestions → provider reviews** (friendly dashboard UI; the desktop CLL's one-tap-safe vs requires-explicit-confirm distinction carries over — provably-safe = easy accept, risky = confirm with evidence shown).
- **Operator has oversight** (can see/override provider acceptances) **and owns all profile-level proposals.**

**Cost-incentive made explicit (Point 11 synergy):**
- CLL lowers interpreter cost over time; AI cost is **provider-borne** (Point 11). So accepting good suggestions **directly lowers the provider's metered AI cost → raises their payout.**
- Surface the projected saving in the dashboard: "accepting this is projected to cut your channel's AI cost ~X%." Turns maintenance into a money motivator.

**Trigger model at scale:**
- **Per-channel scheduled job** in the point-9 worker/scheduler tier (CLL is no longer in the bot).
- **Two triggers, one engine:** eager cold-start (= onboarding extraction, Point 13B) + rolling (`learning_batch_n` accumulation).
- **Queue-based, rate-limited, low-priority** background work — never competes with the live trade path. The desktop "swallow all errors for live-path safety" contract is even more critical now.

**Tuning:** platform-default knobs (operator-set) + **operator-only per-channel overrides** (e.g., larger batch for high-volume channels). NOT exposed to providers (too fiddly).

**Drift detection (the desktop v2, now higher-value):**
- Tie to the **point-14 per-channel AI-accuracy metric** — when a channel's interpretation accuracy degrades (style change: new admin/format), surface "your channel's style may have changed, review updated suggestions" to the provider.
- Reuses metrics already computed; optional shadow re-evaluation (current overlay vs candidate) later.

**Data governance (ties to point 15):**
- Right to learn from channel messages covered by the **provider agreement**.
- **Strict per-channel data boundary** (decision above) = not using Provider A's data to improve Provider B.
- Corpus `learning_samples` keyed by `source_channel_id` (desktop design) — carries over with tenant scoping.

**Carries over unchanged (multi-tenant-scoped):**
- **Propose-only** (nothing auto-activates).
- **`false_suppression_count` gating** (a suppression rule that would have hidden real signals is never one-tap-safe).
- **Accept is the only overlay mutation**, atomic.
- **Live-path error isolation** (CLL failure can never touch trading).
- **Structural change:** `profile.json` (one file, one user) → **per-channel overlay rows in Postgres** (point 5), same field routing (`noise_patterns` / `triage_keep_triggers` / `triggers[]`).

### Point 17 — Admin console

> Operational capstone — mostly a consumer/view layer over locked points. Highest-privilege surface (sees all tenants, can halt trading + move money).

**Build approach: hybrid.**
- **Low-code** (Retool / Forest Admin / Supabase table editor) for the ~80% inspect-and-CRUD surface.
- **Purpose-built, audit-logged action endpoints** for high-risk operations — NOT raw table edits.

**Access model (the part with real weight):**
- **SSO + mandatory MFA** (point 7).
- **RBAC, four roles:** `support` (read + limited subscriber assist), `ops` (account health, halts, channel approval), `finance` (billing/payout/chargebacks), `superadmin` (all, incl. circuit breaker).
- **Every admin action audit-logged** (point 4): who / what / when / before-after. Non-negotiable.
- **Break-glass for tenant-data access:** explicit, time-boxed, reason-logged (ideally consented) — NOT ambient "admins see everything always."

**Functional surfaces (windows onto locked points):**
| Surface | From | Key powers |
|---|---|---|
| Telethon account health | 1 | pool status (alive/challenged/banned), channels per account, re-assign on failure, add account |
| Channel approval queue | 13 | vet/approve/reject, run ownership-code verification, trigger extraction |
| Provider/channel mgmt | 13/14 | suspend/probation, edit listing status, view (not edit) overlays |
| CLL oversight | 16 | review profile-level proposals, override provider acceptances, tune per-channel knobs |
| Lifecycle & audit inspection | 6 | query `execution_events`/`signal_events` per tenant, trace signal→fan-out→fill |
| Billing & payouts | 11/13 | subscription state, chargeback handling, payout oversight, comp/refund (finance) |
| MetaApi fleet health | 12 | connection status, disconnect alerts, redeploy, revoke (delete) account |
| Kill switches | 2/9 | global circuit breaker, per-channel halt, per-account halt |

**Dangerous-button inventory** (confirmation + role-gate + audit; **two-person approval for global ones**):
- Trip/reset global circuit breaker (halts ALL interpretation — point 2).
- Halt a channel (stops signals to all its subscribers — point 9).
- Issue refund / adjust payout (moves money — point 11).
- Revoke/delete a managed account (purges MetaApi creds, stops a subscriber trading — point 12).
- Re-assign Telethon channels (disrupts ingestion for many providers — point 1).

**Observability & incident response:**
- Surface point-7 OTel metrics for humans: fan-out lag, AI spend rate vs circuit-breaker ceiling, EA/MetaApi connection counts, error rates.
- **Alert inbox:** disconnected managed accounts (12), challenged Telethon accounts (1), circuit-breaker proximity, payment failures.
- Incident actions wired to existing kill switches + point-9 notification service. View+action layer — **no new infra.**

**Must NOT:**
- Show broker passwords (not stored — point 12; literally can't).
- Edit immutable audit logs (point 6 — read-only always).
- Silent tenant-data access (break-glass only).

---

## Status: ALL features complete — 10 source (points 1–10) + 7 net-new (points 11–17, incl. 13B) discussed and locked.

### Point 18 — MVP scope cut

> Visual companion: `saas-mvp-visual.html` (open in browser).

**Thesis:** the thinnest end-to-end loop where a real subscriber can pay, connect a broker account with zero install, and have a curated channel's signals auto-traded with safe management — everything operator-assisted, **one execution path.**

**Decisive cut — MetaApi-managed ONLY for v1; defer the self-hosted EA to v1.1.**
- Zero-install onboarding (matches the original vision); server-side Python (testable, no MetaEditor); per-account COGS trivial at beta scale (~$11 × 20 ≈ $220/mo).
- Cascades into deleting from MVP: EA multi-tenant rework, EA token auth, **the entire WebSocket doorbell layer** (only existed for polling EAs — managed execution drives MetaApi server-side), EA versioning, install guide, EA-local caps (replaced by server-side caps, point 12).

**IN (v1):**
| Area | Scope | Pt |
|---|---|---|
| Listener | **Single** Telethon account, multi-channel (keep pool abstraction, don't build it) | 1 |
| AI pipeline | Triage + interpret, once-per-channel, layered prompt with the **1–2 profiles actually launched** + **hand-authored overlays** | 2 |
| Action types | Core 9 (OPEN, MOVE_SL_BE, MOVE_SL, CLOSE_PARTIAL, CLOSE_FULL, REOPEN_LAST, REINFORCE, TIGHTEN_SL, ALERT) | 3 |
| Symbol model | Multi-symbol arch + one-position-per-(subscriber, symbol) | 2 |
| Execution | **MetaApi managed only**; server-side engine + plan interpreter; **native SL/TP always + staged partial closes** | 8 |
| Data | Postgres + RLS, Redis. **No WebSocket layer** | 5 |
| State machine | Two-tier, core states (+ `skipped_filter`/`skipped_kill_switch`) | 6 |
| API/auth | Monolith FastAPI; hosted identity (Supabase/Clerk); **no EA endpoints** | 7 |
| Provisioning | MetaApi credential capture, **custody-by-MetaApi**, consent, server-side hard caps | 12 |
| Billing | Stripe **subscriptions** (platform fee + channel sub) | 11 |
| Catalog | **Minimal**: list channels + subscribe; respects no-retroactive-execution | 14 |
| Onboarding | Operator-assisted, invite-only; minimal provider dashboard (stats) | 13 |
| Subscriber dashboard | Connect account, positions, **personal kill switch**, subscriptions, status | 9 |
| Notifications | Email + Telegram critical only | 9 |
| Admin | **Low-code** (Retool/Supabase), operator-only, basic kill switches | 17 |
| Legal | ToS + Risk + Privacy + disclaimers + geo-gating + **lawyer before real money — NON-NEGOTIABLE, v1-blocking** | 15 |

**OUT (deferred):**
| Deferred | To | Why safe |
|---|---|---|
| Self-hosted EA path (+ token auth, WebSocket, versioning, install guide) | v1.1 | Managed covers the core loop |
| **Chase-price entry + ATR trailing** | **v1.1** | MVP ships native SL/TP + staged closes (safety-critical core); chase/ATR = edge, deferred to reduce launch risk |
| Telethon account pool | >~50 channels / first ban | Single account fine at beta scale; abstraction kept |
| Vocab/example auto-extraction tool | v2 | Operator hand-authors overlays for launch channels |
| Channel Learning Loop (all) | v2 | Pure cost-optimization; not in core loop |
| Per-action approval mode | v1.x | MVP = auto-execute + kill switch |
| Stripe Connect automated payouts | v1.x | Manual payouts to few launch providers |
| Catalog discovery (ranking, quality score, risk labels, reviews, filters) | v1.x–v2 | Early subs arrive via provider links |
| Multi-channel notifications (web/mobile push, SMS) | v2 | Email + Telegram covers critical |
| Full RBAC + break-glass | v1.x | Operator-only console at beta |
| PENDING_ORDER / CANCEL_PENDING | when a launch channel needs them | Only if first channels place explicit pendings |
| Drift detection, white-label bots, multi-region | v2+ | Enhancements |

**Build order (critical path):**
1. Foundations — Postgres+RLS, hosted auth, core entities (providers, channels, subscribers, subscriptions, signal_actions, subscriber_executions, overlays).
2. Listener + AI pipeline — heaviest reuse of desktop code (orchestrator, validators, ai, triage, state_summary → channel-canonical).
3. Managed execution engine — MetaApi adapter + plan interpreter (native SL/TP + staged closes).
4. Provisioning flow — credential capture + MetaApi account lifecycle.
5. Billing — Stripe subscriptions + access gating.
6. Subscriber dashboard + minimal catalog.
7. Operator onboarding + low-code admin.
8. Legal docs + disclaimers + geo-gating.
9. Closed beta (demo accounts) → paid launch (after legal clears).

**Locked call:** chase-price + ATR trailing deferred to **v1.1** (native SL/TP + staged partial closes ship in v1).

### Point 19 — Data model / schema (build-order phase 1)

> Artifact: `saas-schema.sql` (Postgres DDL for MVP). Translates points 1–18 into tables.

**Foundational conventions:**
- **Postgres `timestamptz` everywhere** (UTC) — NOT ISO-8601 text (the desktop used text only because SQLite). Migration nuance noted.
- **UUID PKs** (prefer `uuidv7` on PG18+ for index locality / shard-friendliness, point 5; `new_id()` wrapper so defaults don't change later).
- **State = text + CHECK** (validates enum value only); **legal transitions enforced in the app state-machine layer, not triggers** (point 6 — one source of truth).
- **JSONB** for variable payloads (action payload, overlay content, plan, capability manifest).

**Table groups:**
- **Identity/tenancy:** `subscribers`, `providers`, `admin_users` (RBAC roles). Auth in hosted IdP; we mirror `idp_user_id` + routing/billing fields.
- **Ingestion:** `telethon_accounts` (pool-ready, encrypted session), `profiles` (shared middle prompt layer), `channels` (provider→channel, listing/price/verify), `channel_overlays` (bottom prompt layer, replaces desktop `profile.json`; one active per channel).
- **Subscriptions/wiring:** `subscriptions` (`subscribed_at` enforces no-retroactive, `on_cancel` behavior), `subscription_filters` (per channel+symbol declarative filters), `execution_accounts` (**path enum metaapi|ea**; v1 metaapi only; **stores account_id + KMS-encrypted token, NEVER broker password** — point 12; server-side hard caps).
- **Signal flow (two-tier):** `signal_actions` (channel-level, `fan_out_complete` for restart-safe fan-out) → `subscriber_executions` (per-subscriber lifecycle; **UNIQUE(signal_action_id, subscriber_id)** = fan-out idempotency, point 4) → `positions` (carries `original_volume`/`partial_close_count`/`sl_moved_at`; **partial UNIQUE index = one open position per (subscriber, symbol)**, point 2) → `trade_plans` (shared plan schema, UNIQUE per position = desktop dedupe rule).
- **Market:** `market_reference` (one price per symbol, point 10).
- **Cost/billing:** `ai_usage_ledger` (per-call, channel-keyed — feeds provider-borne cost, point 11), `provider_payouts` (monthly rollup: gross − ai_cost − chargebacks = net, point 13).
- **Audit/consent (append-only):** `execution_events`, `signal_events`, `admin_audit_log`, `admin_access_grants` (break-glass), `consent_log` (versioned trade-authority consent, point 12).
- **Deferred v2 stubs:** `learning_samples`, `learning_suggestions` (CLL — ship empty, point 16).

**Key enforced invariants (at the DB layer):**
- `uq_one_open_per_symbol` — partial unique index on `positions(subscriber_id, symbol) WHERE status='open'` (point 2).
- `UNIQUE(signal_action_id, subscriber_id)` on executions — no double fan-out (point 4).
- `uq_overlay_active` — one active overlay per channel.
- `trade_plans UNIQUE(position_id)` — the desktop `RegisterPlan` dedupe rule.
- **RLS** enabled with tenant-isolation policies keyed on `current_setting('app.current_subscriber')` (illustrated on executions/positions/subscriptions/execution_accounts; replicate per tenant table, point 5).

### Point 20 — Consolidation: PRD + system diagram

Standalone hand-off artifacts produced from points 1–19:
- **`PRD.md`** — self-contained product requirements doc (exec summary, roles, architecture, data flow, component inventory, domain model, invariants, economics, MVP+roadmap, tech stack, build order, legal posture, risks, open questions, artifact index). Citations back to source points; doesn't require reading this log.
- **`saas-architecture-diagram.html`** — layered system visual: signal flow top→bottom (External → Ingestion → Intelligence → Fan-out → Execution → Broker) with surfaces + cross-cutting services alongside, v1/v1.1/v2 badges.

**Full artifact set:** `saas discussion.md` (decision log), `PRD.md`, `saas-architecture-diagram.html`, `saas-schema.sql`, `saas-billing-visual.html`, `saas-mvp-visual.html`.

**Design phase complete.** Remaining work is implementation (build-order phase 1 onward) — and, before real money, the non-deferrable legal engagement (point 15).

---

# Technology stack

Deliberated one decision at a time (same format as points 1–18). Choices already implied/locked by earlier points are noted, not re-litigated: Postgres (pt 5), Redis (pt 5), Stripe + Connect (pt 11), MetaApi (pt 12), Telethon + python-telegram-bot (desktop), Anthropic/OpenAI via `llm_provider` (desktop).

### Tech 1 — Backend language & framework

**Decision: Python + FastAPI (async). Execution/fan-out layer kept cleanly separable for a possible future Go service.**

**Merit-based rationale (holds even greenfield, ignoring existing code):**
- The app is two workloads: an **AI/interpretation brain** (Python's home turf — LLM SDKs, embeddings + clustering for the CLL, structured-output validation, eval tooling) and a **high-concurrency I/O platform** (fan-out, many broker streams, web API — Go/Node territory).
- The AI brain is the **core IP, hardest to get right, and where misclassification loses money** — and where Python's ecosystem lead is largest. Optimize the stack around getting it right.
- Python async concurrency ceiling is real but far beyond MVP scale (comfortably hundreds–low-thousands of concurrent accounts).
- **Node/TS** = best single-language option *only if* AI work were lighter / team JS-first (full-stack type sharing is its real edge); loses on AI/ML ecosystem for this app.
- **Go** = best concurrency, wrong for the AI core + verbose for CRUD/dashboards; right as a *targeted execution service*, not the whole app.
- **Optimal large-scale shape = polyglot** (Python brain + Go/Node execution behind the API boundary) — deferred to v2+; two runtimes on day one is overhead an MVP shouldn't pay.
- Existing desktop `src/` (orchestrator, validators, ai, triage, state_summary, llm_provider) ports nearly mechanically → turns a correct choice into a no-brainer.

**Sub-decisions locked:** async throughout (`asyncpg`/SQLAlchemy 2.0 async, `httpx`); Pydantic for validation (already used); execution/fan-out layer behind a clean internal boundary so it can become a separate Go service later without rewriting the brain.

### Tech 2 — Hosting / cloud platform

**Decision: DigitalOcean** — App Platform (web API + workers + persistent listener/execution processes) + Managed Postgres + Managed Valkey (Redis). External KMS for token encryption. Droplets/DOKS reserved for scale.

**Why DO over PaaS (Fly/Railway) and hyperscaler (AWS/GCP) for MVP:**
- **Grow-into-it within one provider** — App Platform → Droplets (VMs) → DOKS (k8s) without re-platforming. Softer trajectory than Fly/Railway (which eventually jump to AWS); pushes the painful migration far out, maybe never.
- **Managed Postgres + Managed Valkey** in one console (covers pt 5's two stores; PgBouncer pooling available).
- **Predictable flat pricing** — easier COGS forecasting than AWS's meter-everything model.
- App Platform handles long-running processes (listener + execution engine are persistent, not request/response).
- Far less ops burden than AWS; SOC2 deferred (pt 12) so AWS's compliance edge isn't MVP-blocking.

**KMS caveat (NOT DO-specific):** DO has no native KMS — but neither do Fly/Railway/Render; only AWS/GCP do. Point 12's hard requirement (envelope-encrypt MetaApi trade-authority tokens) is met the same way on any non-hyperscaler: **self-hosted HashiCorp Vault (a Droplet) or external KMS over API** (standalone AWS KMS / Infisical). Identical effort vs Fly — a task to budget, not a reason to switch hosts.

**AWS re-evaluation trigger:** a partner/customer forcing native-KMS or early SOC2. Migration (containerized FastAPI + managed Postgres) is well-trodden if it comes.

**Cascades set by this choice:** Postgres = DO Managed Postgres; Redis = DO Managed Valkey; secrets/KMS = Vault-on-Droplet or external KMS; compute = App Platform (MVP) → Droplets/DOKS (scale).

### Tech 3 — Authentication / identity

**Decision: Clerk** for web-user (subscribers + providers) + admin auth.

> Note: DO Managed Postgres (Tech 2) ruled out the Supabase bundle (which pairs auth with its own Postgres + RLS-JWT). So this is a pure auth-service choice.

**Why Clerk:**
- **Backend/DB-agnostic** — composes cleanly with DO Postgres; we store `idp_user_id` in `subscribers`/`providers`/`admin_users` (schema already has the columns), Clerk owns credential/session lifecycle.
- **MFA out of the box** — satisfies the mandatory-admin-MFA rule (pts 7/17) with no TOTP flows to build.
- **Organizations/roles** map to provider/subscriber/admin + admin RBAC (pt 17).
- "Don't roll your own auth" (pt 7) is strongest for a **financial product** — a self-hosted auth bug is a breach headline; Clerk's business is getting this right.

**Rejected:** Auth0 (enterprise-grade but heavier/pricier than needed at MVP; revisit only for enterprise SSO demands); self-host Keycloak/Ory/FastAPI-Users (the "rolling your own" burden pt 7 warned against — dangerous for a financial product); Supabase Auth (weak fit detached from Supabase Postgres).

**Tradeoff accepted:** per-MAU cost at scale + external login dependency — acceptable at MVP/early scale; Clerk supports enterprise SSO later if needed.

**Scope note:** EA token auth (v1.1) is a separate, simple revocable-token table — NOT Clerk.

### Tech 4 — Frontend framework

**Decision: Next.js (React)** for the three web surfaces — subscriber dashboard, provider dashboard, public catalog. (Admin is separate low-code, pt 17.)

**Why Next.js:**
- **Serves both surface types in one framework:** SSG/SSR for the SEO-sensitive public catalog (acquisition surface) AND a rich authenticated app for the dashboards — no split stack.
- **Clerk's deepest, best-documented integration is Next.js** (middleware route protection, server-side session helpers) — materially cuts auth wiring after the Tech-3 lock.
- Largest talent pool + ecosystem + component libraries → ship three surfaces fast.
- Hosts cleanly as a container on **DO App Platform** alongside the API (no second vendor), or Vercel.
- Pairs with the Python API over the REST boundary already being built.

**Rejected:** SvelteKit (leaner/faster but smaller ecosystem, less first-class Clerk support); plain React SPA + separate catalog (splits stack, loses catalog SEO); HTMX + FastAPI templates (one-language appeal, but dashboards are app-like — live positions, kill switches, config — where a real SPA framework earns its keep; the one-language win was already banked on the backend in Tech 1).

### Tech 5 — Background workers / task queue

**Decision: Arq** — async-native (asyncio + Redis), built-in cron scheduling.

**Hosts:** promoter (pending→sent, fan-out), sweepers (stale claims, approval/watch/offline expiry), notification dispatcher, CLL (v2). Pulled OUT of the bot into dedicated workers (pt 9).

**Why Arq:**
- **Async-native** — fits the async FastAPI backend (Tech 1) with no impedance; Celery's sync model would fight it.
- **Redis-backed** — reuses DO Managed Valkey (Tech 2); no new infra.
- **Built-in cron** covers the sweepers; queued jobs cover fan-out + notifications.
- From the Pydantic authors — idiomatic in a Pydantic/FastAPI codebase; lighter op/conceptual weight than Celery (matters for small-team MVP).

**Rejected:** Celery (heavyweight, config-heavy, awkward async story); Dramatiq (good but less async-native); Postgres `SKIP LOCKED` queue (genuinely tempting minimal-infra option since lifecycle is already PG state — but notifications + CLL fit a real job queue better, and Arq gives scheduling for free). All beat Celery's weight for this stack.

### Tech 6 — Admin console tooling

**Decision: `sqladmin` (in-app, self-hosted) + the purpose-built audited action endpoints** from pt 17.

> Implements pt-17's hybrid shape: low-code for inspect/CRUD; custom audited endpoints for dangerous operations.

**Why `sqladmin`:**
- **Inside the FastAPI/SQLAlchemy app** — no third vendor, no tenant/financial data leaving the stack; tightens the pt-4/17 audit-and-isolation security story.
- **Zero extra cost**, auto-generates from the SQLAlchemy models already being written — fastest path to operator inspect/edit.
- Dangerous ops (circuit breaker, refunds, revoke managed account — pt 17 inventory) are **not** raw CRUD anyway → purpose-built audited endpoints; the low-code layer only needs safe inspect/edit.
- MVP operator = engineers/founder, not a non-technical ops team.

**Rejected (for MVP):** Retool (most capable; right later IF a non-technical ops team needs to self-assemble views — can be added pointing at the same DB); Forest Admin (auto-CRUD SaaS, external vendor); custom Next.js admin (max control but real build time competing with the product).

**Upgrade path:** add Retool later against the same Postgres if ops scales.

### Tech 7 — Transactional email

**Decision: Resend** (email side of pt-9 notifications; Telegram side = the bot, already locked).

**Why Resend:**
- **React email templates** (`react-email`) — share components/design tokens with the Next.js frontend (Tech 4); tight stack cohesion.
- Excellent deliverability + clean modern REST API → easy from async Python (`httpx`).
- Generous free tier covers MVP/beta at ~$0.
- Sits behind the pt-9 notification service → swappable.

**Rejected:** Postmark (equally-defensible "boring & bulletproof" transactional pedigree — the conservative alt; chosen against only for Resend's React-template stack fit + free tier); AWS SES (cheapest at scale but bare-bones DX, only makes sense deep in AWS — we're on DO); SendGrid/Mailgun (heavier, slipped deliverability reputation).

### Tech 8 — Observability backend

**Decision: Grafana Cloud** (MVP free tier) over OTel → self-hosted Grafana/SigNoz on a Droplet as the cost-control fallback at scale.

> pt 7 set OpenTelemetry as the instrumentation standard; this is where telemetry lands.

**Why Grafana Cloud:**
- **OTel-native + zero lock-in** — consumes OTLP directly; if costs rise, self-host the identical stack (Grafana/Loki/Tempo/Prometheus) or switch to SigNoz without re-instrumenting.
- Free tier covers MVP/beta (metrics + logs + traces) at ~zero ops.
- Surfaces pt-17 admin needs (fan-out lag, AI spend vs circuit-breaker ceiling, connection counts, error rates) + drives the ops alert inbox.

**Rejected:** Datadog (best UX but notorious cost trap — avoid until revenue justifies); SigNoz self-hosted (strong cohesive single-tool alt, fits DO theme — close second; Grafana Cloud chosen only for zero-ops free tier + self-host escape hatch); Axiom (cheap/great DX but lighter on full tracing/APM depth).

### Tech 9 — Secrets & KMS

**Decision (split):**
- **App secrets** (Clerk/Stripe/MetaApi/DB keys): **DO App Platform encrypted env vars** — standard, adequate, zero extra infra for MVP.
- **Application-data encryption** (MetaApi trade-authority tokens + Telethon sessions, pt 12): **AWS KMS standalone, over API** — envelope encryption; master key never leaves KMS, app holds only KMS-encrypted data keys.

**Why:**
- pt-12 tokens carry **trade authority** → deserve a real key-management boundary, not a key in an env var.
- AWS KMS = native-grade key isolation/rotation/audit **without** AWS hosting or Vault's ops burden; cheap (pennies), battle-tested. A thin AWS-KMS API dependency, not AWS hosting.
- DO env secrets already cover app secrets free → only the token-encryption need remains, which KMS solves most robustly.

**Rejected:** HashiCorp Vault (the "correct" enterprise answer but disproportionate ops — HA, seal/unseal, "Vault down = can't decrypt"; defer to scale/SOC2); Infisical (strong unified secrets+encryption, light ops — the alt if one tool preferred; split chosen since DO env vars handle app secrets free); all-DO app-level master key in env var (weak key isolation/rotation — rejected for the trade-authority tokens specifically).

**Tradeoff accepted:** thin AWS-KMS dependency slightly cuts the "all-DO" cleanliness, but it's an API call (not hosting) buying the strongest custody story for the most sensitive data (pt 12).

### Tech 10 — Repo structure & CI/CD

**Decision: Monorepo + GitHub Actions (CI gate) → DO App Platform (CD); Alembic migrations; frontend = 2-app Turborepo.**

**Repo layout:**
```
/ (monorepo)
  /backend            FastAPI app, listener, execution engine, Arq workers, sqladmin
  /frontend           Turborepo (JS workspace)
    apps/
      catalog         public, SSG/SEO (catalog + marketing)
      app             authenticated; subscriber + provider role-gated via Clerk
    packages/
      ui              shared design system
      api-client      typed client for the FastAPI backend
      types           shared types (incl. generated API types)
      config          eslint/ts/tailwind presets
  /db                 schema.sql baseline + Alembic migrations
  /ea                 MQL5 EA (v1.1)
  /infra              DO App Platform spec, CI config
  /.github/workflows
```

**Why:**
- **Monorepo** — schema/API/frontend change together constantly early; atomic cross-cutting PRs are a velocity win; path-filtered CI builds each area on change. EA (v1.1) in its own top-level dir.
- **GitHub Actions** — already on GitHub; deepest ecosystem; existing pytest suite ports straight in; runs test/lint/type-check **gate before** handing CD to DO App Platform (don't auto-deploy a financial app without the gate).
- **Alembic** — SQLAlchemy-native migrations; the hand-written `saas-schema.sql` becomes the initial baseline.

**Frontend = 2-app Turborepo (refinement of Tech 4):**
- Split that carries real value = **public vs authenticated**: `catalog` (SSG/SEO, no auth code, fast acquisition bundle) vs `app` (authenticated shell shared by subscriber + provider, role-gated via Clerk).
- **Rejected 3-app split** (separate subscriber/provider apps) for MVP: they share ~80% (auth, shell, design system, API client); provider surface is minimal in MVP (pt 13/18); 3-way adds Clerk satellite-domain config + 3 deploys/CI lanes for benefit that mostly pays off later.
- Turborepo structure makes promoting `provider` into its own app **later** a clean mechanical extraction. (Admin is NOT a Next app — it's sqladmin, Tech 6.)
- 3-app split reconsidered only if hard subdomain/security boundaries (`providers.copytrades.io`) or a separate provider team are wanted from day one.

### Admin access model (clarifies pt 17 + Tech 6)

Admin capability is **deliberately split across a few purpose-fit surfaces**, not one monolithic page (pt 17 must-nots: never edit billing-truth-in-Stripe, never edit immutable audit logs):

| Surface | Access | Covers |
|---|---|---|
| **sqladmin** (in-app, Tech 6) | `admin.copytrades.io` (or `/_admin`), Clerk **admin role + mandatory MFA**, IP allowlist | Safe CRUD + inspection: subscribers, providers, **approval queue** (channel.status), channels, telethon/execution-account health, **read** positions/executions/signal_actions, **read** audit + `ai_usage_ledger` |
| **Custom audited action endpoints** (Tech 6) | Same admin auth; buttons surfaced in sqladmin/thin admin page | Side-effecting **actions** (not raw CRUD): trip/reset circuit breaker, halt channel, **refund** (calls Stripe), **revoke/delete managed account** (calls MetaApi), re-assign Telethon channels — each confirmation + role-gated + audit-logged; two-person for global ones |
| **Grafana Cloud** (Tech 8) | SSO | Operational health dashboards + alert inbox (fan-out lag, AI spend vs ceiling, connection counts, error rates) |
| **Stripe + Clerk dashboards** | their own admin | **Sources of truth** for billing + identity; complex billing actions done here, webhooks sync back to Postgres mirror |

**Direct answer to "can I manage everything from it":** Yes — essentially every aspect is manageable, but across these 4 surfaces by design, not one pane. Editing mirrored billing rows directly is forbidden (would desync from Stripe); audit logs are read-only. **If a single unified pane is a hard requirement**, that's the case for pulling **Retool** forward (Tech 6 deferred option) — it can stitch Postgres + Stripe API + the custom endpoints into one dashboard. **Decision: keep the split for MVP** (sqladmin + action endpoints + Grafana + Stripe/Clerk dashboards).

---

## Status: Technology stack complete (Tech 1–10). Implementation plan produced.

- **PRD §11 synced** to the locked stack (DigitalOcean, Clerk, Next.js/Turborepo 2-app, Arq, Resend, Grafana Cloud, DO env vars + AWS KMS, sqladmin, monorepo + GitHub Actions + Alembic).
- **`IMPLEMENTATION_PLAN.md`** — phased v1 build plan: 11 phases (0–10) across 4 tracks (Infra/Backend/Frontend/Legal), each with concrete tasks + exit criteria + decision refs; parallelism map; M1–M4 milestone rollup; cross-cutting testing/reuse/scope-guard notes.

**Full artifact set:** `saas discussion.md` · `PRD.md` · `IMPLEMENTATION_PLAN.md` · `saas-schema.sql` · `saas-architecture-diagram.html` · `saas-billing-visual.html` · `saas-mvp-visual.html`.

**Design + planning phase complete. Next is execution (Phase 0).**

---

# Frontend design & libraries

Deliberated before writing frontend code (per web/design-quality rules: pick a specific direction, define palette/type/tokens). Visuals: `saas-design-directions.html` (3-way compare), `saas-design-A-gold.html` (finalist, dual-register).

### FE 1 — Design language

**Decision: "Terminal × Gold" — precise modern fintech, dual-register, one token system.**

- **Direction:** Direction A (Terminal — Linear/Vercel precision applied to trading) with **gold as the brand accent** (not cool blue). Gold is *meaningful*, not decorative — XAUUSD/gold heritage — and escapes the blue-fintech sea.
- **Dual register, one system:** identical tokens; **app = dark** (long sessions, data density, P&L focus), **catalog = light** (trust, SEO, conversion). Gold + semantic colors + type constant across both → reads as one product.
- **Rejected:** B (light approachable — fine but less distinctive, less "serious tool"); pure C (premium serif/gold — borrowed its accent, not its serif; precision > flourish for a data product).

**Token spec (initial):**
- **Palette:** bg `#0A0E14`, panel `#10161F`, panel-2 `#141C27`, line `#1C2734`; text `#EAF1F8`, muted `#7D8A9C`; **gold `#D4AF37`** / gold-hi `#E8C66A` / gold-deep `#B8932C`; **gain `#2EA043`**, **loss `#F85149`**; light surface `#F6F7F9` / `#FFFFFF`.
- **Type:** **Inter** (400–900) for UI; **JetBrains Mono** for every price/lot/%/ID with **tabular numerics** (money never shifts width). No serif.
- **Discipline rules (locked):**
  1. **Gold is an accent, never a fill** — primary CTAs, logo mark, "tracked" mark, single position edge-stripe. No gold gradients/fills everywhere.
  2. **Green/red strictly semantic** for P&L — gold never competes with profit/loss signaling.
  3. **Tabular monospace for all money.**
  4. **One token set, two registers** — dark app / light catalog.

### FE 2 — Styling + component layer

**Decision: Tailwind CSS + shadcn/ui (Radix primitives), tokens as CSS variables, components owned in `packages/ui`.**

**Why:**
- **Own components as source** (shadcn = code copied into the repo, not a themed npm dep) → the only way to hit the distinctive Terminal × Gold identity without fighting a vendor's house style.
- **Tokens-as-CSS-variables** maps the FE-1 palette/type into Tailwind; **dual register** (dark app / light catalog) = a class/`data-theme` swap on the same variables.
- **Accessibility built-in** (Radix: focus/keyboard/ARIA) — matters for the dashboards, tedious to hand-roll.
- First-class with Next.js + RSC (no CSS-in-JS runtime/server-component friction).
- Shared `packages/ui` → catalog + app consume the same components in their registers.

**Rejected:** styled kits (MUI/Mantine/Chakra — opinionated look you'd fight; heavier runtime; anti-template direction rules them out); raw Radix without shadcn (same base, more boilerplate — shadcn is this pre-assembled); CSS-in-JS (RSC friction + runtime cost).

**Note:** shadcn's *default* look is itself template-y — but it's the unstyled starting point; restyled to Terminal × Gold (radii, gold accent, tabular numerics) per FE-1 discipline rules, it won't read as default-shadcn.

### FE 3 — Charts & data visualization

**Decision: split — TradingView Lightweight Charts (price/position) + Recharts (equity + stats), both token-themed.**

**Why the split:**
- **Price charts are a different job.** Position entry/SL/TP as price lines on candlestick/area series is exactly what **Lightweight Charts** is built for; traders recognize/trust the TradingView look. Tiny (~45kb), free, OSS. Forcing a general lib to do this is fighting the tool.
- **Everything else = general charting:** equity/P&L curves (dashboard), win-rate/R:R/drawdown (catalog channel pages) → **Recharts**, declarative, restyled to tokens.
- **Rejected Tremor:** Recharts-plus-KPI-cards with its own visual opinions — would duplicate/fight `packages/ui` (shadcn already covers cards). Use plain Recharts + own card components.
- **Rejected visx:** low-level D3-for-React; overkill unless a bespoke viz Recharts can't express appears (not MVP).

**Consistency rule (locked):** both libraries consume the same token variables (gold accent, semantic gain/loss, mono tick labels, tabular numerics) → data-viz is part of the design system, not bolted on.

**Tradeoff accepted:** two chart libs = slightly more surface, but they cover genuinely different jobs, both light, and one-lib-doing-price-charts-badly is worse for the product's core screen.

### FE 4 — Standard kit (forms, tables, state, data-fetching, icons, motion)

**Decision (locked as a batch):**
| Concern | Choice |
|---|---|
| Forms + validation | **React Hook Form + Zod** (Zod doubles as TS types) |
| Tables | **TanStack Table** (headless → styled via `packages/ui`) |
| Server state / fetching | **RSC for catalog (SSG/ISR) + TanStack Query for the authenticated app** |
| Client state | **Zustand** (minimal UI state; don't duplicate server state) |
| Icons | **Lucide** (pairs with shadcn) |
| Motion | **Framer Motion, used sparingly** (1–2 purposeful moments, per FE-1) |

**Data-fetching nuance (MVP has no WebSocket layer, pt 18):**
- Catalog → **RSC** (SSG/ISR, SEO-fast, no client fetching for listings).
- Authenticated app → **TanStack Query with interval refetch** (positions/P&L poll every few seconds) against REST, which reflects server-side MetaApi stream state. "Live enough" **without** a WebSocket layer — consistent with the MVP cut.
- v1.1: swap TanStack Query polling for a WS subscription (minimal change) if sub-second push is needed.

**Rejected/considered:** SWR (fine, but TanStack Query has richer caching/polling/optimistic-update story for the dashboard); true real-time push now (deferred — polling is adequate for MVP freshness, WS was already cut in pt 18).

---

## Status: Frontend design language + library stack complete (FE 1–4).

Full frontend stack: **Next.js (Turborepo 2-app) · Tailwind + shadcn/ui (Radix) · Terminal × Gold tokens · TradingView Lightweight Charts + Recharts · TanStack Table · RHF + Zod · RSC + TanStack Query (polling) · Zustand · Lucide · Framer Motion.** PRD §11 (frontend rows) updated to reflect FE 1–4.

---

# Pre-implementation cross-cutting concerns

Architecture-shaping concerns surfaced before Phase 0. **Tier 1** (decide now, retrofit-hostile): i18n+RTL, timezone/locale, environments+demo-test, mobile strategy, data-retention/GDPR-deletion, product analytics. **Tier 2** (spec during build): notification taxonomy, failure UX, a11y target, anti-fraud, currency display, email domain auth, support tooling. **Tier 3** (GTM, non-blocking): branding/domain, SEO content, subscriber-KYC confirmation.

### PRE 1 — i18n + RTL

**Decision: build i18n + RTL infrastructure now; ship English (default/fallback) + Arabic at launch.**

**Rationale:** signals are Arabic, audience skews MENA → Arabic is a *launch* locale + differentiator, not someday. RTL + locale routing are retrofit-hostile → must be in from the first component; translation *content* can grow incrementally.

**Scope of translation:**
- ✅ Platform UI chrome (app + catalog) + transactional emails/notifications (per user locale).
- ❌ Provider channel content (provider's language — display, don't translate); AI-interpreted signals (structured data — format, don't translate).
- ⚠️ **Legal docs — translated separately with counsel, never machine-translated** (pt 15).

**Stack:**
- **next-intl** (App-Router-native, RSC support) — over next-i18next/react-intl.
- **Subpath routing** (`/en`, `/ar`), locale detection (Accept-Language + user pref); SEO-friendly for catalog.
- **RTL:** `dir` on `<html>` per locale; **CSS logical properties** + **Tailwind `rtl:`/`ltr:` variants** everywhere (no physical left/right); mirror directional icons only; shadcn/Radix are RTL-aware (FE-2 pays off).
- **Formatting:** native `Intl` (`NumberFormat`/`DateTimeFormat`).

**Fintech-i18n rule (LOCKED — easily missed, costly late):** in Arabic RTL, **the chrome mirrors but the data does NOT** —
- price charts **never mirror** (time axis is universally left→right in trading);
- **prices, lots, P&L stay LTR with Western/Latin numerals** (`4,812.50`, not Eastern-Arabic numerals) — matches trading convention + keeps FE-1 tabular numerics consistent.

**Workflow:** in-repo JSON catalogs (`messages/en.json` + `ar.json`; English = source of truth) → graduate to a TMS (Crowdin/Locize) when more languages are added.

### PRE 2 — Timezone & temporal display

**Storage (carry desktop convention):** all timestamps **UTC, tz-aware** (`timestamptz`; ISO-8601 `+00:00`). Never naive `utcnow()`. Everything sorts/compares in UTC server-side.

**Display:** **user-local timezone**, browser-detected + overridable in settings; timezone label shown where ambiguous (e.g., position open time). Rendered via `Intl.DateTimeFormat` with the user's tz. Fixed market-session view deferred to v1.1.

**Business-logic windows are UTC-anchored (LOCKED — not the viewer's local day):** per-channel **daily AI budget** (pt 11), **`REOPEN_LAST` within-24h** (pt 10), **provider monthly payout period** (pt 13), monthly rollups — all computed in a single fixed reference (UTC) server-side, else "today/this month" would shift per user and be inconsistent. Presentation-only converts to local. (Payout "month" = fixed civil month in the declared platform tz.)

**Relative time:** `Intl.RelativeTimeFormat` (locale-aware, pairs with PRE-1) for recency. Market-price **staleness (`>60s` STALE, pt 10)** stays a server-side UTC computation; UI reflects the flag.

**Money override (PRE-1/FE-1):** dates/times localize via `Intl`; **prices/lots/P&L stay Western-numeral + LTR** regardless of locale.

### PRE 3 — Environments & demo-account test strategy

> Most important "don't skip" pre-Phase-0 item: this system places real trades on real money — it can't be tested the normal way.

**Topology: three environments** — local → **staging** (sandbox/test mode of every external service) → prod.

**Sandbox per external service:**
- **MetaApi → MT5 demo broker accounts** — the **primary safety net**: full fan-out → execution → reconciliation loop runs end-to-end with real broker mechanics + fake money.
- **Stripe → test mode** (test keys/cards/webhooks via Stripe CLI); **Clerk → separate instance**; **AI → real APIs or recorded fixtures**; **Telegram → dedicated test bot + test channel**.

**Test layers (build on desktop suite):**
- Hermetic unit/integration against a **real Postgres** (don't mock the DB — desktop lesson); ported pytest suite.
- **Per-channel replay gate** (pt 13B) as CI gate.
- **Playwright E2E** on staging: signup → connect demo → subscribe → signal → position appears.
- **MetaApi-demo execution integration tests**: dispatch each action type, assert broker state + reconciliation.
- **Fan-out load test** (1 signal → N executions) before scaling subscribers.

**Demo/live guardrail (NON-NEGOTIABLE):** hard demo-vs-live separation at data (`execution_accounts` flag), API, and UI (persistent "DEMO" banner). **Staging is demo-only by config — physically cannot connect a live broker account.** Design so a test *can't* fire real trades.

**Release mgmt:** feature flags from day one (ship deferred/half-built work dark, gate risky execution changes); CI-gated Alembic up/down (Tech 10); staging seed data (demo channels/subscribers); DO rolling deploys (blue-green/canary deferred).

**Conservative go-live (carry desktop ethos):** demo beta → **tiny server-side hard caps** (pt 12) on first live accounts → ramp over time. (Desktop precedent: `MaxLotsPerSignal=0.01`, demo ≥2 weeks first.)

### PRE 4 — Mobile strategy

**Decision: Responsive web + PWA for MVP; native apps deferred to post-MVP (traction-gated).**

**Why:**
- Responsive is mandatory anyway — catalog is a mobile/SEO acquisition surface; dashboard must work on a phone.
- **PWA is a small delta** over responsive and buys the two things that matter for trading: **installable home-screen presence** (retention) + **web push** for time-sensitive alerts (position events, approvals) — complements Telegram quick-actions (pt 9). iOS push requires installed PWA (acceptable MVP constraint).
- **Native (React Native/Expo) is a product, not a feature** — second codebase/skill set, store review, finance-app scrutiny. The mobile moments that matter (check positions, halt, approve) are served by responsive PWA + push + Telegram.

**Design-in-now:** keep the API a clean REST boundary (pt 7) so a future native app is "just another client" — no architectural debt.

**Mobile-urgent reach at launch:** web push (installed PWA) + Telegram (pt 9). Web push integrates as another pt-9 notification channel alongside email + Telegram.

**Rejected:** native in MVP (premature scope); plain responsive without PWA (loses installability + push for marginal savings).

### PRE 5 — Data retention & GDPR/erasure

> Core tension: append-only audit (pt 6) + financial-record retention vs "delete my data." Resolved by anonymization, not row deletion. Must be schema-designed, not bolted on.

**Erasure model: crypto-shred / anonymize PII, retain de-identified financial+audit records keyed to an opaque id.**
- **Deleted/anonymized:** profile PII, email, Clerk link, broker login/server, MetaApi token (revoke + delete at MetaApi, pt 12), consent reduced to "consent existed."
- **Retained (anonymized):** executions, positions, signal linkage, audit events, payouts (Stripe keeps its own per financial law).
- Satisfies GDPR (no longer personal once truly de-identified) AND preserves pt-6 audit/financial integrity. **Never delete audit/financial rows — anonymize the actor link.**

**Retention windows (explicit):**
- Operational data: indefinite while account active.
- Closed financial/audit: **statutory 5–7y** (confirm per jurisdiction with pt-15 lawyer).
- Raw Telegram text + learning corpus: short TTL (~90–180d) unless needed for CLL (v2).
- Logs/observability (Grafana): 30–90d.
- `ai_usage_ledger`: until billing-reconciled (payouts), then aggregated.

**Data export:** self-serve subscriber export (account + trade history CSV/JSON) — also a product feature (traders want history for taxes).

**Schema implications (now):** PII isolated from financial rows behind an opaque FK so it can be anonymized without cascade; **soft-delete + anonymize** pattern (`deleted_at`/`anonymized_at` + nulled PII), not hard delete, for financially-linked entities; audit stays append-only; **erasure runbook fans out to processors** (Clerk/Stripe/MetaApi/Resend) — doubles as GDPR/SOC2 artifact (pt 12, pt 15 DPA). The "no broker passwords held" design (pt 12) shrinks the erasure surface.

### PRE 6 — Product analytics

> Distinct from Grafana (Tech 8 = system health). This = product/business funnel + activation + retention. Can't backfill un-fired events → instrument day one.

**Decision: PostHog (self-hosted or EU cloud) for product analytics + feature flags; GA4 on the public catalog only.**

**Why:**
- **One tool, two locked needs:** product analytics **and** the **feature flags PRE-3 requires** → fewer moving parts.
- **Privacy/GDPR fit (PRE-5):** open-source + EU-host/self-host keeps behavioral data controllable; better for a finance product than shipping all behavior to a third party.
- **Funnels + session replay** answer the core question (where does catalog→first-trade leak?) — GA4 can't inside an authenticated app.
- **GA4 catalog-only** — genuinely good + free for public marketing/SEO traffic; stays out of the authenticated app where PostHog owns the funnel.

**Core funnel to instrument:** catalog_viewed → channel_viewed → signup_completed → broker_connected → subscription_started → **first_trade_executed** (activation) → + kill_switch_used, churn.

**Rules (locked):** define a **canonical event taxonomy up front** (consistent, analyzable from day one); **never send financial PII or secrets into analytics events** (PRE-5/pt-12 discipline).

**Rejected:** Amplitude/Mixpanel (slicker but extra vendor holding behavioral data + cost; PostHog fits stage + privacy); GA4-only (weak for authenticated-app product funnels); roll-your-own (premature).

---

## Status: Tier-1 pre-implementation concerns complete (PRE 1–6).

Locked: i18n+RTL · timezone/temporal · environments+demo-test · mobile (PWA) · data-retention/GDPR · product analytics. **Tier 2** (notification taxonomy, failure UX, a11y target, anti-fraud, currency display, email domain auth, support tooling) = spec during relevant build phase. **Tier 3** (branding/domain, SEO content, subscriber-KYC confirmation) = parallel GTM. PRD/IMPLEMENTATION_PLAN to absorb PRE 1–6 (i18n/RTL into Phase 0/7; demo-test into Phase 0/3; retention into Phase 1; analytics into Phase 0).

### Brand name — **Signari** (working brand)

**Decision:** product brand = **Signari** (replaces the "CopyTrades" placeholder).

**Why:** signal-rooted (the literal core — interpreting/relaying signals), legal-posture-safe (no profit/wealth/advice connotation, pt 15), fits Terminal × Gold (precise, premium), internationally clean incl. Arabic phonetics ("sig-NAH-ree").

**Scan result (2026-05-31, web-search proxy — NOT legal clearance):** no brand collision found in any sector; **`signari.com` available to purchase (~$7,950, Spaceship)**. Rejected alternatives on collision grounds: Aurum (severe in-niche — Aurum Markets MT4/MT5 broker, AURUM Foundation AI+gold), Aurevo (aurevo.ai = AI trading), Relayn/Relayo (active cos), Cue (generic), Decoda (no `.com`, crowded tech namespace).

**Caveats (pt 15 legal track):**
1. Proper trademark knockout (USPTO/EUIPO + launch jurisdiction) via counsel **before** buying domain / printing brand.
2. **Do not buy the domain yet** — repo doesn't need it; purchase after TM clears. Fold into legal engagement.

**Repo/identity:** build under `signari` now; domain + TM clearance ride the legal track.
