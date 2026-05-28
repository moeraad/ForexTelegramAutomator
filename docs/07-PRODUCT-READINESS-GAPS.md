# 07 — Product Readiness Gaps

**Summary.** To go from "tool the owner uses" to "SaaS that paying subscribers depend on", essentially every cross-cutting concern is missing or rudimentary. The trading core itself is mature; what is absent is multi-tenancy, account & billing, customer-facing UI, fan-out execution to N subscriber broker accounts, per-subscriber risk control, compliance, and cloud/operations. This file is a brutally honest inventory — not a solution proposal.

Severity legend:
- **BLOCKER** — cannot launch a paid product without it.
- **MAJOR** — first wave of subscriber pain will hit here.
- **MINOR** — code health / scaling concern.

---

## Multi-tenancy

**BLOCKER — there is no concept of multiple tenants.**

Evidence:
- `_owner_only(user_id)` in `src/bot.py:23` is a hardcoded single-user check.
- `TG_BOT_OWNER_USER_ID` is a single int in `settings`.
- `src/api.py` auth is a single static `EA_SHARED_TOKEN` shared between the EA and the API. No notion of "which subscriber is this token for".
- `process_message` writes to a single DB; the "stack" concept (one DB per channel) is operator-facing parallel deployment, not subscriber-facing multi-tenancy.
- v2 config (`src/config_v2.py`) introduces Account / Profile / Channel / Destination / Bot / Route / BotBinding entities — but these are all **operator-side configuration entities**, not subscriber records. There is no `User` or `Subscriber` entity, no per-user DB partition, no row-level security in any table.
- Every SQL query reads/writes across the whole DB; no `WHERE subscriber_id=?` predicate exists anywhere.

To support N subscribers, the schema needs a `subscribers` table, a `subscriber_id` column on every operational table (`actions`, `positions`, `messages`), and every query refactored. The v2 routing layer is partial work in this direction but stopped at the operator level.

## User accounts, authentication, subscription, billing

**BLOCKER — none of these exist.**

- No `users` / `subscribers` / `accounts` (in the SaaS sense) table.
- No signup / login flow. The only "auth" is `_owner_only` Telegram-id check + a static shared API token.
- No password handling. No `passlib`, `bcrypt`, `argon2`, `oauth`, `jwt`, `authlib`, `fastapi-users` in `pyproject.toml`.
- No subscription model, no plan/tier, no Stripe/Paddle/Lemon Squeezy/Chargebee integration. No invoices.
- No "subscription expires" check anywhere. No grace period logic.
- No KYC, no terms-of-service capture, no GDPR consent record, no Right-to-be-Forgotten endpoint.
- Email is not used anywhere in the codebase. There is no SMTP integration, no SES, no SendGrid, no Postmark.
- Phone capture exists only as the operator's Telethon login phone (PII, plaintext in `stacks_config.json`).

## Per-subscriber broker account linking (1 signal → N brokers)

**BLOCKER — the system has exactly one MT5 terminal in mind, owned by the operator.**

- EA is a chart-attached MQL5 binary running on the operator's machine.
- API speaks to `127.0.0.1:8765` (the EA's host). There is no notion of "fan out this OPEN to 200 subscriber MT5 terminals".
- v2 Routes can target multiple `Destination`s (operator's own MT5 endpoints) but a Destination is `(MT5 terminal + DB + API process)` — multiplying across 200 subscribers means 200 MT5 terminals running on the operator's infra, 200 DBs, 200 API processes, 200 EAs. The architecture doesn't scale this way.
- There is no MetaApi / cTrader Open API / broker-side webhook integration. The system has no way to OrderSend to a subscriber's broker account without that subscriber installing the EA on their own MT5 terminal.
- Per-subscriber broker credential storage: nothing exists. Subscribers cannot "link" their MT5 account through a subscriber UI today.
- The Magic Number per-instance can keep multiple EAs distinct on one account, but it's still one account.

The architecture would need to flip from "EA pulls work" to either "central service pushes to MetaApi per subscriber" or "subscriber-installed EA polls a multi-tenant API that knows the subscriber from a per-subscriber bearer token". Either is a complete redesign.

## Per-subscriber risk management

**BLOCKER — risk knobs are EA-side and per-EA-attach.**

- `MaxLotsPerSignal`, `MaxSlLossPercent`, `LotsPer100Balance`, `ChasePriceEnabled`, `EnableInstantOpen`, `EnableReinforce`, `EnableAiPartialAndBe` — every one of these is an EA input set on chart attach. They are global to the EA instance.
- No "subscriber risk profile" entity. No max-drawdown-per-subscriber. No instrument blacklist per user.
- Route-level rules exist (`Route.max_lots`, `Route.min_account_balance`, `Route.skip_if_drawdown_pct`, `Route.allowed_action_types`, `Route.time_of_day_filter`) — these are routed via `payload_extras` to the EA at execution time (`src/orchestrator.py:326`). This is a partial scaffolding but applies to operator routes, not subscriber identities.
- Cost guard (`src/cost_guard.py`) caps LLM spend globally; no concept of per-subscriber LLM budget.
- Equity-proportional sizing: the EA computes lots from `ACCOUNT_BALANCE` of the MT5 terminal it runs in. Per-subscriber proportional sizing would require knowing each subscriber's balance, which the central system has no way to learn under the current model.

## Subscriber-facing UI

**BLOCKER — there is no end-user UI.** The GUI in `src/gui/` is an operator workbench (~50 modules of services-bar, journal, replay, profile editor, NSSM control). Specifically:

- No web app. PySide6 desktop only.
- No mobile app.
- No "view my recent trades" customer page.
- No "pause my signals" / "resume my signals" customer toggle.
- No "set my max lot" customer control.
- No "see my P&L" customer dashboard.
- The Telegram bot's commands (`/halt`, `/cancel`, `/positions`) are owner-only.

## Signal provider management

**MINOR (today) → MAJOR (at scale)** — partial scaffolding exists, not finished.

- Today: each "stack" is one channel. Adding a second channel means another stack (another DB, another set of services). The v2 multi-channel work (`src/config_v2.py`, `docs/plans/2026-05-23-multi-channel-routing.md`) introduces a single API/listener/bot serving multiple channels — but Step 12 (multi-channel destinations) is **deferred** in the code.
- Per-provider performance tracking: closed positions carry `realized_pnl`; with `source_channel_id` tagging in place (Steps 2, 18) per-channel P&L is queryable, but there is no GUI view for "Channel X this week" beyond what an operator can hand-grep.
- Enable/disable per channel: `Channel.enabled` and `Channel.halted` fields exist in v2 config, and `process_message(halted=True)` short-circuits.
- No marketplace, no rating, no subscriber-side "browse signal providers" flow.

## Compliance gaps the code does not address

- **No risk disclosure surface** anywhere in code. No "trading involves risk of total loss" landing page. No suitability questionnaire.
- **No KYC**. No identity verification flow. No sanctions screening.
- **No terms-of-service capture** with versioned consent.
- **No regional gating**. Many jurisdictions (US, certain EU states) regulate copy-trading as investment advice / discretionary management — the code has no jurisdiction awareness.
- **No record-keeping for regulator audit**. While the DB does keep every action, there is no documented retention policy and no tamper-evident audit log; modifying `settings` is silently allowed.
- **GDPR**: PII (phone numbers) is stored plaintext in `stacks_config.json` (`src/config_v2.py:Account` docstring acknowledges this — DPAPI does not apply to that file).
- **No marketing-consent flow** for the bot's DM channel. Anyone the operator adds as a subscriber implicitly consents to receive trade notifications.
- **No advice-vs-execution disclaimer** baked into copy.

## Per-subscriber observability and customer support

**BLOCKER.**

- No per-account audit trail beyond the global `actions` / `positions` tables.
- No CS tooling: no admin panel for "look up this customer's recent trades", no impersonation, no force-close-position-for-subscriber.
- No "incidents that affected my account" view.
- No notification of platform-wide outages to subscribers.
- The owner's Telegram-DM stream IS the customer support channel.

## Latency and scale — at what subscriber count does this break

The current architecture's bottlenecks (estimated, not measured):

| Subscribers | What breaks first |
|---|---|
| 1 (today) | Nothing |
| 10 (operator-managed, all on operator's machine) | Multi-stack NSSM works but every stack runs its own LLM call, multiplying cost; SQLite WAL on one DB per stack is fine |
| ~50 | Operator's machine can't host 50 MT5 terminals; LLM cost guard caps trip; bot's polling getUpdates rate-limits |
| ~200 | Need a real distributed system: central LLM service + per-subscriber broker bridge + push notifications + horizontal scale. This is a redesign, not a refactor. |
| 1000+ | Telegram Bot API rate-limits make per-subscriber DM-on-every-trade impossible. MTProto user-account sessions are not provisioning-friendly at this scale. |

**The Python LLM pipeline is single-threaded per process.** A single Sonnet+thinking call can block 2–8 seconds. There is no concurrent message-handling: a slow LLM call holds the entire listener (BackgroundTask in API runs in starlette's threadpool, so it's actually thread-pooled, but the orchestrator opens its own SQLite connection and does blocking IO).

**SQLite as the only data store** caps writes to ~1 writer at a time (WAL allows readers to not block, but the orchestrator writes during the LLM call). Multi-tenancy demands switching to PostgreSQL/MySQL or partitioning to per-subscriber SQLite files.

## Reliability — what happens to 200 subscribers mid-trade if the single server dies

Today (1 subscriber): NSSM restarts services; EA reconciles; recovery DMs prompt the operator about parked actions. State survives.

200-subscriber hypothetical: 200 subscribers all see "no signal received for 10 minutes" with no platform-wide incident communication. If the server hosts 200 MT5 terminals, all 200 reconcile simultaneously — depending on broker rate limits, mass-reconciliation could itself trip protections.

**There is no high availability, no failover, no warm standby, no cross-region replication.**

## Disaster recovery, backups, secret rotation

(See `docs/06-OPERATIONAL-POSTURE.md`. Recapping for the gap perspective):

- **BLOCKER**: DPAPI ciphertext is machine-bound. Migration to a new server invalidates every encrypted secret. There is no key-export tool.
- **BLOCKER**: No scheduled backups. Manual `backup_io.py` flow only.
- **MAJOR**: No secret rotation runbook. Rotating the EA shared token requires the operator to (a) update the DB setting, (b) walk to each MT5 chart, (c) change the EA input, (d) restart the EA.
- **MAJOR**: SQLite as the master store has no built-in remote replication. Litestream is not used.

## Pricing and metering infrastructure

**BLOCKER — entirely absent.**

- No metering of "trades executed for subscriber X this month".
- No tier-based feature gating (e.g., "free tier: 5 trades/day; pro: unlimited").
- No usage-based billing hook. No webhook endpoint for billing-system events.
- No "cancel subscription" flow.
- No proration logic, no trial period, no coupon system.
- `logs/ai_calls.jsonl` is the closest thing to a usage stream and it's per-stack global LLM cost, not per-subscriber.

## Communication / customer messaging

**MAJOR.**

- The only outbound channel is Telegram DM via the bot. The bot is one Telegram bot per stack (per v2 Bot entity).
- Sending the same trade to N subscribers means either (a) N Telegram bots (Telegram quota: ~30 messages/sec per bot to different chats, less to one chat), or (b) one bot DMing all subscribers (the operator's subscriber list lives where? — does not exist).
- No email transport, no push notifications, no SMS, no in-app inbox.
- No locale support beyond what the Telegram client does.

## Inferred top concerns ordered by what a strategist asks first

1. **There is no subscriber concept anywhere.** Building one means schema, auth, API, GUI, and operational concepts all need to be added.
2. **The execution model assumes the EA runs on the broker holder's machine.** Productization needs to choose: subscriber-installs-EA (low-trust, support burden) OR centralized MetaApi (vendor relationship, latency, KYC weight). Both are absent.
3. **The system is tuned to one channel.** The IP that ships first (the Forex Engineer interpreter prompt) is per-channel, per-language. Scaling to "10 signal providers" multiplies prompt-engineering work; the trigger-matcher + unmatched_messages workflow is designed for one operator curating one channel's vocabulary, not a marketplace.
4. **No safety net against the operator going offline.** Without an L2 support / runbook / paging story, paying subscribers will demand a refund the first time the operator's laptop reboots during the New York open.
