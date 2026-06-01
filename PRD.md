# CopyTrades SaaS — Product Requirements Document

**Status:** Design complete (v1 scope locked) · **Date:** 2026-05-31
**Source of decisions:** `saas discussion.md` (points 1–19)
**Companion artifacts:** `saas-architecture-diagram.html` · `saas-schema.sql` · `saas-billing-visual.html` · `saas-mvp-visual.html`

> This PRD is a standalone hand-off. It does not require reading the discussion log. Where a decision needs its full rationale, it cites the source point (e.g. *(pt 8)*).

---

## 1. Executive summary

CopyTrades is a **Telegram → AI → MT5 copy-trading SaaS**. Operator-run Telegram accounts read trading-signal channels; a two-stage AI pipeline interprets each message **once per channel** into structured trade actions; those actions fan out to every subscriber of that channel and execute on each subscriber's MetaTrader 5 account — **with zero install** via cloud execution (MetaApi).

It is a **two-sided marketplace**: signal-channel **providers** list channels and earn a revenue share; **subscribers** discover and subscribe to channels and have signals auto-traded on their broker account.

The product evolves an existing single-user desktop app (Telegram→AI→MT5 bridge for one Arabic gold channel) into a multi-tenant web platform.

---

## 2. Problem & vision

**Problem.** Retail traders follow signal channels on Telegram but can't act on every message instantly, correctly, and 24/7. Signals are natural-language, multilingual, and noisy. Manual copying is slow, error-prone, and impossible while asleep.

**Vision.** A subscriber connects their broker account once (three fields, no software install), subscribes to one or more vetted channels, and every signal is interpreted by AI and executed on their account automatically, with disciplined trade management — while the platform never holds their funds and never sees their broker password.

**Operator model.** The operator (platform) negotiates with channel owners to add a reader account to their channel. Providers get a dashboard and a shareable join link; subscribers join via that link or discover channels in a public catalog.

---

## 3. Roles

| Role | Who | Core surface |
|---|---|---|
| **Subscriber** | End-user trader | Web dashboard: connect account, subscribe to channels, positions, kill switch |
| **Provider** | Signal-channel owner | Web dashboard: channel settings, price, aggregate stats, payouts |
| **Operator/Admin** | Platform staff | Admin console: account health, channel approval, kill switches, billing |

---

## 4. System architecture

> See `saas-architecture-diagram.html` for the visual.

Five layers + cross-cutting services:

1. **Ingestion** — operator-owned Telethon account(s) (single account for MVP, pool-ready) read many provider channels over one event loop *(pt 1)*.
2. **Intelligence** — two-stage AI: cheap triage (`keep|ignore`) → interpreter (structured actions). Layered prompt: universal scaffold + language/asset profile + per-channel overlay. Runs **once per channel message** *(pt 2)*.
3. **Coordination** — Postgres is durable state; the action lifecycle is a two-tier state machine. Redis handles rate limits, presence, idempotency, cost counters *(pts 5, 6)*.
4. **Fan-out** — one channel-level `signal_action` → N per-subscriber `subscriber_executions`, after deterministic per-subscriber filters and idempotency guards *(pts 2, 4, 10)*.
5. **Execution** — a server-side engine drives the broker through a **broker-primitive interface** with pluggable adapters. v1 ships the **MetaApi (managed cloud)** adapter only; the self-hosted MT5 EA adapter is v1.1. A shared **plan schema** carries autonomous management (staged closes, trailing) *(pts 8, 12, 18)*.

**Cross-cutting:** hosted identity (auth), Stripe + Connect (billing/payouts), KMS (encryption), background workers (promoter, sweepers, CLL), notification service, append-only audit, observability (OTel).

---

## 5. End-to-end data flow

```
Provider's Telegram channel
  → Listener (Telethon)                                    [pt 1]
  → AI triage (keep|ignore)                                [pt 2]
  → AI interpreter → structured action(s)                  [pt 2]
  → signal_actions row (channel-level, 1)                  [pt 6]
  → FAN-OUT: for each active subscriber of the channel:
        apply declarative filters                          [pt 3]
        apply per-subscriber idempotency guards            [pt 10]
        translate canonical symbol → broker symbol         [pt 8]
        resolve lots (fixed / risk-%)                       [pt 8]
     → subscriber_executions row (per-subscriber, N)       [pt 6]
  → Execution engine claims & executes via adapter:
        MetaApi (v1)  /  self-hosted EA (v1.1)             [pt 8/18]
        push native broker SL/TP (always)                  [pt 8 safety]
        register trade_plan (staged closes; chase/trail v1.1)
  → result POSTed back → execution state terminal          [pt 6]
  → positions row maintained; reconciliation continuous    [pt 8]
  → notifications to subscriber (email/Telegram)           [pt 9]
```

Management actions (MOVE_SL_BE, CLOSE_PARTIAL, …) carry **no ticket** — the engine resolves the target via the **one-open-position-per-(subscriber, symbol)** invariant *(pt 2)*.

---

## 6. Component inventory

| Component | Responsibility | MVP |
|---|---|---|
| **Listener** | Telethon client(s); ingest channel messages | ✅ single account |
| **AI pipeline** | Triage + interpreter; layered prompt; channel-canonical state | ✅ |
| **Fan-out service** | Filters, idempotency guards, symbol/lot resolution, execution row creation | ✅ |
| **Execution engine** | Broker-primitive interface; MetaApi adapter; plan interpreter | ✅ MetaApi only |
| **Provisioning service** | MetaApi account create/deploy/revoke; credential pass-through | ✅ |
| **API (FastAPI monolith)** | `/v1/api` (web), `/v1/admin`; `/v1/ea` deferred | ✅ |
| **Promoter + sweepers (workers)** | Promote pending→sent, expire stale/approval/watch | ✅ |
| **Billing service** | Stripe subscriptions; access gating; payout rollup | ✅ (manual payouts) |
| **Notification service** | Email + Telegram (critical) | ✅ minimal |
| **Subscriber dashboard** | Connect account, positions, kill switch, subs | ✅ |
| **Provider dashboard** | Channel settings, aggregate stats, payouts | ✅ minimal |
| **Public catalog** | List channels + subscribe | ✅ minimal |
| **Admin console** | Low-code; operator controls; audit | ✅ |
| **Telegram control bot** | Notifications + quick-actions | ✅ basic |
| **Channel Learning Loop** | Per-channel self-distillation | ❌ v2 |
| **Vocab/extraction tool** | Auto-generate channel overlays | ❌ v2 |

---

## 7. Domain model

> Full DDL: `saas-schema.sql`. Highlights:

- **Two-tier flow:** `signal_actions` (channel-level, 1) → `subscriber_executions` (per-subscriber, N) → `positions` → `trade_plans`.
- **Identity/tenancy:** `subscribers`, `providers`, `admin_users`; auth in hosted IdP, mirrored here.
- **Ingestion:** `telethon_accounts`, `profiles` (shared prompt layer), `channels`, `channel_overlays` (per-channel prompt layer, replaces desktop `profile.json`).
- **Wiring:** `subscriptions` (`subscribed_at`, `on_cancel`), `subscription_filters`, `execution_accounts` (**stores MetaApi token, never broker password**).
- **Money:** `ai_usage_ledger` (provider-borne metered AI cost), `provider_payouts`.
- **Audit (append-only):** `execution_events`, `signal_events`, `admin_audit_log`, `admin_access_grants`, `consent_log`.

---

## 8. Core invariants & safety rules

1. **One open position per (subscriber, symbol)** — DB-enforced; management actions infer the target *(pt 2)*.
2. **Interpret once per channel, fan out** — AI cost scales with channels, not subscribers *(pt 2)*.
3. **Fan-out idempotency** — `UNIQUE(signal_action_id, subscriber_id)`; restart-safe via `fan_out_complete` *(pt 4)*.
4. **No retroactive execution on subscribe** — only signals after `subscribed_at` route *(pt 4)*.
5. **Tenant isolation** — RLS; providers never see subscriber PII/PnL/broker *(pts 4, 5)*.
6. **MetaApi custodies the broker password; we store only the token** — a DB breach leaks no broker passwords *(pt 12)*.
7. **Always push native broker SL/TP** — a position is never naked during an outage; only soft optimization (trailing/partials) pauses *(pt 8)*.
8. **Server-side hard caps** (max lots / open trades / daily loss) — managed-tier backstop with no EA-local fallback *(pt 12)*.
9. **Interpretation quality is never degraded for cost** — best model always; AI cost is metered & passed to the provider *(pt 11)*.
10. **Circuit breaker is anomaly-only** — trips on runaway/injection/billing anomalies, never legitimate load *(pt 11)*.
11. **Propose-only learning** — CLL never auto-activates; accept is the only overlay mutation *(pt 16)*.
12. **Audit is append-only & immutable** *(pt 6)*.

---

## 9. Pricing & economics

> Visual: `saas-billing-visual.html`.

- **Marketplace, ~75/25 split.** Provider sets channel price; platform takes ~25% commission *(pt 11)*.
- **Hybrid subscription:** platform access fee (funds platform COGS — execution tier, infra) **+** per-channel provider-priced subscription.
- **Tiers by execution, not AI quota:** Free (EA, 1 channel) / Self-hosted (EA, ~$15–25) / Managed (MetaApi, ~$40–60). *(MVP ships Managed first.)*
- **AI cost: metered, variable, provider-borne** — `provider payout = ~75% of channel subs − metered AI cost`. Full quality always; CLL lowers cost → raises payout *(pts 11, 16)*.
- **Verified MetaApi COGS (live-checked):** ~$11–14 / managed account / month on G2 *(pt 8)*.
- **Processor:** Stripe + Stripe Connect (Express). Platform never holds funds *(pt 11)*.

---

## 10. MVP scope & roadmap

> Visual: `saas-mvp-visual.html`.

**v1 (ships):** single Telethon account · AI pipeline with 1–2 launched profiles + hand-authored overlays · core 9 action types · multi-symbol · **MetaApi managed execution only** (native SL/TP + staged closes) · Postgres+RLS/Redis (no WebSocket) · two-tier state machine · monolith API + hosted auth · MetaApi provisioning · Stripe subscriptions · minimal catalog + dashboards · low-code admin · **legal docs/disclaimers/geo-gating (non-negotiable)**.

**v1.1:** self-hosted MT5 EA path (+ token auth, WebSocket doorbell) · **chase-price + ATR trailing** · per-action approval mode · Stripe Connect automated payouts.

**v2:** Channel Learning Loop · vocab/extraction tool · full catalog discovery (ranking, quality score, risk labels, reviews) · multi-channel notifications · Telethon pool · full RBAC/break-glass · drift detection · white-label bots · multi-region.

---

## 11. Tech stack

> Locked via deliberated decisions Tech 1–10 (see `saas discussion.md` → Technology stack).

| Concern | Choice | Notes |
|---|---|---|
| **Backend** | **Python + FastAPI** (async) | Reuses desktop orchestrator/validators/ai/triage; execution layer kept separable for a future Go service |
| **Hosting** | **DigitalOcean** | App Platform (web + workers + persistent processes); grow-into-it → Droplets/DOKS at scale |
| **Database** | **DO Managed Postgres** + RLS | PgBouncer pooling; `timestamptz`; UUID PKs |
| **Cache/queue broker** | **DO Managed Valkey** (Redis) | rate limits, presence, idempotency, cost counters, Arq broker |
| **Auth** | **Clerk** | web users (subscriber+provider) + admin; MFA built-in; EA token auth = separate table (v1.1) |
| **Frontend** | **Next.js (React)** in a **2-app Turborepo** | `catalog` (public/SSG/SEO) + `app` (authenticated, subscriber+provider role-gated); shared `ui`/`api-client`/`types` packages |
| **Design language** | **"Terminal × Gold"** (FE 1) | Precise modern fintech; dual register (dark app / light catalog), one token set; gold brand accent (XAUUSD heritage), semantic green/red P&L, Inter + JetBrains Mono tabular numerics |
| **Styling / components** | **Tailwind + shadcn/ui (Radix)** (FE 2) | Tokens as CSS variables; components owned in `packages/ui`; restyled to Terminal × Gold |
| **Charts** | **TradingView Lightweight Charts** (price/position) + **Recharts** (equity/stats) (FE 3) | Both token-themed; data-viz is part of the design system |
| **FE libraries** | RHF + Zod · TanStack Table · **RSC + TanStack Query (polling)** · Zustand · Lucide · Framer Motion (FE 4) | App polls for live-ish positions (no WebSocket in MVP, pt 18); WS swap = v1.1 |
| **Workers / scheduling** | **Arq** | async-native, Redis-backed, built-in cron; runs promoter + sweepers + notifications (+ CLL v2) |
| **Execution** | **MetaApi cloud (G2)** | managed v1; MQL5 EA adapter = v1.1; broker-primitive interface + shared plan schema |
| **Payments** | **Stripe + Stripe Connect (Express)** | subscriptions + provider payouts; platform never holds funds |
| **Email** | **Resend** | `react-email` templates; behind the pt-9 notification service (swappable) |
| **Observability** | **Grafana Cloud** (OTel) | free tier → self-host Grafana/SigNoz at scale |
| **App secrets** | **DO App Platform encrypted env vars** | Clerk/Stripe/MetaApi/DB keys |
| **Data encryption (KMS)** | **AWS KMS** (standalone, over API) | envelope-encrypt MetaApi tokens + Telethon sessions (pt 12); Vault deferred |
| **Admin** | **sqladmin** (in-app) + **custom audited action endpoints** | + Grafana (metrics) + Stripe/Clerk dashboards (their truths); Retool deferred unless a single unified pane is needed |
| **Repo / CI-CD** | **Monorepo + GitHub Actions → DO App Platform** | path-filtered CI gate (test/lint/type-check) before CD; **Alembic** migrations (baseline = `saas-schema.sql`) |
| **AI providers** | **Anthropic / OpenAI** via `llm_provider` abstraction | carried from desktop |
| **Telegram** | **Telethon** (listener) + **python-telegram-bot** (control bot) | two identities, two processes (carried from desktop) |

---

## 12. Build order (critical path)

1. Foundations — Postgres+RLS, hosted auth, core entities.
2. Listener + AI pipeline (max reuse of desktop code).
3. Managed execution engine — MetaApi adapter + plan interpreter.
4. Provisioning flow — credential capture + MetaApi lifecycle.
5. Billing — Stripe subscriptions + access gating.
6. Subscriber dashboard + minimal catalog.
7. Operator onboarding + low-code admin.
8. **Legal docs + disclaimers + geo-gating (gate).**
9. Closed beta (demo accounts) → paid launch (after legal clears).

---

## 13. Legal & regulatory posture

> ⚠️ Framing only — **requires a qualified fintech lawyer per launch market** *(pt 15)*.

- **Target posture:** pure technology/relay tool — **not** investment adviser, **not** money manager.
- **Defended by design:** user-initiated/authorized execution; AI translates (does not advise); no fund custody (Stripe); no password custody (MetaApi); provider is the signal source; disclaimers everywhere.
- **Biggest risk:** auto-execution on held credentials drifting toward "discretionary management." Mitigate via framing + per-market counsel.
- **Hard gates:** geo-block to 1–2 counsel-cleared jurisdictions at launch; engage a fintech lawyer **before taking real money**; ship ToS / Risk Disclosure / Privacy / Provider Agreement / DPA / consent flows.
- **AI-specific:** disclaim misclassification liability; secure rights to send channel content to AI vendors and to use it for the CLL.

---

## 14. Key risks

| Risk | Mitigation |
|---|---|
| Single Telethon account banned → all channels dark | Pool-ready design; move to pool early *(pt 1)* |
| AI misinterprets a signal → wrong trade | Per-channel replay gate; native SL/TP; hard caps; ToS disclaimer |
| MetaApi outage → managed trades pause | Native broker SL/TP always; status alerts; G2 tier *(pt 8/12)* |
| Regulatory action in a jurisdiction | Geo-gating; tech-tool posture; counsel before launch *(pt 15)* |
| Chatty channel erodes margin | Provider-borne metered AI cost *(pt 11)* |
| Credential breach | MetaApi custody (we never store passwords); KMS for tokens *(pt 12)* |
| Provider lists a scam channel | Invite-only + manual approval; suspension; verified stats *(pt 13/14)* |

---

## 15. Open questions (for next phase)

- Exact launch jurisdiction(s) and the resulting geo-gating list (legal-dependent).
- Final tier dollar amounts (structure locked, numbers illustrative).
- Which language/asset profiles to build for launch (driven by the first signed channels).
- Whether any launch channel needs `PENDING_ORDER`/`CANCEL_PENDING` in v1.
- MetaApi volume/business-tier pricing negotiation at scale.

---

## Appendix — artifact index

| File | Contents |
|---|---|
| `saas discussion.md` | Full decision log, points 1–19 (rationale + rejected alternatives) |
| `PRD.md` | This document |
| `saas-architecture-diagram.html` | System architecture visual |
| `saas-schema.sql` | Postgres DDL (MVP) |
| `saas-billing-visual.html` | Billing/money-flow visual |
| `saas-mvp-visual.html` | MVP scope + build-order visual |
