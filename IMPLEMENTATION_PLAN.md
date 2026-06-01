# CopyTrades SaaS — Phased Implementation Plan (v1 / MVP)

**Status:** Ready to execute · **Date:** 2026-05-31
**Inputs:** `PRD.md`, `saas discussion.md` (points 1–20 + Tech 1–10), `saas-schema.sql`
**Scope:** MVP only — **MetaApi-managed execution, operator-assisted onboarding** (pt 18). Self-hosted EA, chase/ATR, CLL, full catalog discovery, Connect auto-payouts = v1.1/v2.

> **How to read:** Phases are the critical path. Within phases, tasks are grouped by **track** (🟦 Infra · 🟩 Backend · 🟪 Frontend · 🟥 Legal/Ops) so parallel work is visible. Each phase has an **Exit criteria** (definition of done) — don't advance until it's met. Refs cite decision points (pt N) and tech choices (Tech N).

---

## Tracks & parallelism (read first)

Four tracks run partly in parallel after Phase 0:
- **🟦 Infra** — DO, CI/CD, KMS, observability.
- **🟩 Backend** — FastAPI, AI pipeline, execution, fan-out, billing, workers.
- **🟪 Frontend** — Turborepo, catalog, authenticated app.
- **🟥 Legal/Ops** — the long-lead legal engagement (start at Phase 0, gates paid launch at Phase 9).

**Critical path:** Phase 0 → 1 → 2 → 3 → 4 → 5 → (6 ∥ 7) → 8 → 9 → 10.
Frontend (🟪) scaffolds in parallel from Phase 0 and integrates against backend endpoints as they land. **Legal (🟥) starts on day one** — lawyer lead times are long and it's the hard gate before real money.

---

## Phase 0 — Project setup & scaffolding
**Goal:** an empty-but-deployable skeleton with CI/CD green to production.

- 🟦 Create **monorepo** (Tech 10): `/backend`, `/frontend` (Turborepo), `/db`, `/infra`, `/.github/workflows`.
- 🟦 **DigitalOcean** project: App Platform app spec, **Managed Postgres**, **Managed Valkey** (Tech 2).
- 🟦 **GitHub Actions** CI gate: lint (ruff), type-check (mypy), test (pytest), frontend build — then CD to App Platform (Tech 10).
- 🟦 **AWS KMS** key for data encryption; **DO env-var** secrets wiring (Tech 9).
- 🟦 **Grafana Cloud** project + OTel SDK wired into a hello-world FastAPI (Tech 8).
- 🟦 **Three environments** — local → **staging (sandbox/test-mode of every external service)** → prod; **feature-flag system** (PostHog flags) wired from day one (PRE 3/6).
- 🟦 **PostHog** project + **canonical event taxonomy** defined (`catalog_viewed`…`first_trade_executed`); GA4 on catalog only; **no PII/secrets in events** (PRE 6).
- 🟩 FastAPI app skeleton (async), health endpoint, `/v1` router prefixes (`api`/`admin`; `ea` stub) (pt 7).
- 🟪 Turborepo: `apps/catalog`, `apps/app`, `packages/{ui,api-client,types,config,i18n}` (Tech 10); blank deploys.
- 🟪 **next-intl + RTL scaffolding from the first commit** — subpath locale routing (`/en`,`/ar`), `dir` switching, **CSS logical properties + Tailwind `rtl:` variants**, Terminal × Gold tokens made direction-agnostic (PRE 1). Retrofit-hostile → do it now, not later.
- 🟪 **Clerk** project; wire sign-in to `apps/app`; admin role defined (Tech 3, pt 17).
- 🟥 **Engage fintech lawyer**; scope ToS / Risk / Privacy / Provider Agreement / DPA; decide launch jurisdiction(s) (pt 15).

**Exit criteria:** push to `main` → CI passes → both Next apps + FastAPI deploy to DO; Clerk login works; a trace appears in Grafana; **staging is demo-only by config**; an `/ar` route renders RTL; a PostHog event fires; lawyer engaged.

---

## Phase 1 — Data foundations
**Goal:** the full schema live, with tenancy and migrations.

- 🟩 Port `saas-schema.sql` → **Alembic** baseline migration (Tech 10).
- 🟩 SQLAlchemy 2.0 async models for all entities (subscribers, providers, channels, profiles, channel_overlays, subscriptions, subscription_filters, execution_accounts, signal_actions, subscriber_executions, positions, trade_plans, market_reference, ai_usage_ledger, provider_payouts, audit tables).
- 🟩 **RLS policies** + the `app.current_subscriber`/`app.current_provider` session-setting pattern (pt 5).
- 🟩 Enforce DB invariants: one-open-position-per-(subscriber,symbol), fan-out uniqueness, one-active-overlay, plan-per-position (pt 2/4).
- 🟩 Sync Clerk users ↔ `subscribers`/`providers`/`admin_users` via `idp_user_id` (webhook on Clerk user events).
- 🟩 **Retention/erasure schema (PRE 5):** PII isolated from financial rows behind an opaque FK; **soft-delete + anonymize** pattern (`deleted_at`/`anonymized_at` + nullable PII) for financially-linked entities; audit tables stay append-only; demo/live flag on `execution_accounts` (PRE 3).
- 🟩 All timestamp columns `timestamptz` (UTC); confirm no naive datetimes (PRE 2).
- 🟦 Backup/restore verified on Managed Postgres.

**Exit criteria:** migrations apply cleanly up/down; RLS blocks cross-tenant reads in a test; a Clerk signup creates a `subscribers` row; an anonymize-on-erasure test nulls PII while retaining the (now de-identified) financial rows.

---

## Phase 2 — AI pipeline (highest desktop reuse)
**Goal:** a channel message → structured `signal_actions` row, end to end.

- 🟩 Port desktop `orchestrator`, `validators`, `ai`, `ai_triage`, `state_summary`, `llm_provider`, `fingerprint`, `signal_memory` into `/backend` (pt 2; biggest accelerator).
- 🟩 **Listener** service (Telethon, single account, multi-channel) → writes raw messages keyed by channel (pt 1).
- 🟩 Layered prompt assembly: **scaffold + profile + channel overlay** from `profiles` + `channel_overlays` (pt 2).
- 🟩 **Channel-canonical** position-state rendering (replaces desktop per-user state) (pt 10).
- 🟩 `market_reference` per-symbol price (one reference, not per-broker) + staleness (pt 10).
- 🟩 Triage → interpret → write `signal_actions` (+ `signal_events`).
- 🟩 Port the **replay test suite** (`test_management_replay`-style) as the per-channel prompt-drift gate (pt 13B).

**Exit criteria:** a seeded test channel's messages produce correct `signal_actions` on the replay fixtures; cost logged to `ai_usage_ledger` (pt 11).

---

## Phase 3 — Managed execution engine
**Goal:** an action → a real (demo-account) trade via MetaApi.

- 🟩 **Broker-primitive interface** (`open_market/open_pending/modify_sl/modify_tp/close_partial/close_full/cancel_pending/query_positions/query_symbol_spec`) (pt 8).
- 🟩 **MetaApi adapter** implementing the interface (Tech: MetaApi G2).
- 🟩 **Plan interpreter** (Python): native SL/TP always + **staged partial closes**; chase/ATR **stubbed for v1.1** (pt 8/18).
- 🟩 Symbol/lot resolution: canonical→broker symbol, fixed + risk-% lots, validate vs broker min/step (pt 8).
- 🟩 `positions` + `trade_plans` maintenance; continuous reconciliation from MetaApi stream (pt 8).
- 🟩 **Server-side hard caps** (max lots / open trades / daily loss) (pt 12).

**Exit criteria:** dispatching each of the 9 action types against a MetaApi **demo** account produces correct broker state; staged closes fire; native SL/TP always present; reconciliation converges.

---

## Phase 4 — Provisioning (security-critical)
**Goal:** a subscriber connects a broker account with 3 fields, zero install.

- 🟩 Credential capture endpoint: **in-memory pass-through** to MetaApi; **never logged, never persisted** — enforced by a test asserting the password never hits logs (pt 12).
- 🟩 Store `metaapi_account_id` + **KMS-envelope-encrypted token** in `execution_accounts`; **no broker password column** (pt 12, Tech 9).
- 🟩 Managed-account lifecycle: `provisioning→connected→deployed→…→revoked`; **validate-then-bill** (pt 12).
- 🟩 **Versioned consent** capture → `consent_log` (pt 12); broker-ToS attestation (pt 8).
- 🟩 **One-click revoke** = undeploy + delete MetaApi account (purge creds) (pt 12).
- 🟩 Broker-server lookup/fuzzy-match to cut onboarding friction (pt 12).

**Exit criteria:** connect a demo account end-to-end; security test confirms no password in logs/DB; revoke deletes the MetaApi account.

---

## Phase 5 — Fan-out & lifecycle workers
**Goal:** one `signal_action` → N personalized `subscriber_executions`, executed.

- 🟩 **Fan-out service**: for each active subscription → apply declarative filters (pt 3), per-subscriber idempotency guards (pt 10), symbol/lot resolution → create `subscriber_executions` (unique per signal+subscriber, restart-safe via `fan_out_complete`) (pt 4).
- 🟩 **Two-tier state machine** transitions enforced in app layer (pt 6); write `execution_events`.
- 🟩 **Arq workers** (Tech 5): promoter (pending→sent), sweepers (stale claim / approval / watch / offline expiry).
- 🟩 Respect **no-retroactive-execution** (`subscribed_at`) and per-subscriber kill switch → `skipped_*` states (pt 4/9).
- 🟩 **Anomaly-only circuit breaker** (global hourly spend ceiling) (pt 11).
- 🟩 **UTC-anchored business windows** (PRE 2): daily AI budget, `REOPEN_LAST` 24h, monthly payout period computed in fixed UTC reference, never viewer-local.

**Exit criteria:** a single test signal fans out to multiple demo subscribers with different filters/lots; idempotency guards skip correctly; restart mid-fan-out produces no duplicates.

---

## Phase 6 — Billing  *(parallel with Phase 7)*
**Goal:** subscribers pay; access is gated; providers' economics tracked.

- 🟩 **Stripe** subscriptions: platform access fee + per-channel provider-priced subscription (pt 11).
- 🟩 **Webhook sync** Stripe→Postgres mirror (`subscribers.platform_tier/tier_status`, `subscriptions`) (pt 11).
- 🟩 **Access gating** middleware (tier → execution path/features); subscription status → fan-out eligibility.
- 🟩 **Provider payout rollup** job: gross − metered AI cost (`ai_usage_ledger`) − chargebacks → `provider_payouts`; **manual payout** for MVP (Connect auto = v1.1) (pt 11/13).
- 🟩 Failed-payment → **manage-to-close** degradation (pt 4/11).

**Exit criteria:** test-mode checkout creates a subscription; webhook updates the mirror; an unpaid subscriber is correctly gated; a payout rollup computes net correctly.

---

## Phase 7 — Frontend  *(parallel with Phase 6)*
**Goal:** the three surfaces, integrated against the API.

- 🟪 `packages/api-client` + `packages/types` (generate types from FastAPI OpenAPI).
- 🟪 `apps/app` (authenticated, Clerk): **subscriber** — connect account (Phase 4 flow), positions, **personal kill switch**, manage subscriptions, status (pt 9).
- 🟪 `apps/app`: **provider** (role-gated) — minimal: channel settings, price, aggregate stats, payout view (no subscriber PII) (pt 13).
- 🟪 `apps/catalog` (public, SSG): list channels + channel page (verified/hypothetical stats + disclaimers + risk label) + subscribe flow (pt 14).
- 🟪 Subscribe flow honors no-retroactive-execution + tier selection (pt 4/14).
- 🟪 **Components RTL-aware + localized** (en/ar) per PRE 1; **dates/times in user-local tz, money stays LTR + Western numerals** (PRE 1/2); charts never mirror.
- 🟪 **PWA** (installable, offline shell) + **web push** subscription as a pt-9 notification channel (PRE 4); live-ish data via TanStack Query polling (FE 4).

**Exit criteria:** a user signs up, subscribes to a channel from the catalog, connects a demo account, and sees a live position appear after a test signal; the app renders correctly in `/ar` RTL with money still LTR; installs as a PWA and receives a web-push test.

---

## Phase 8 — Admin, notifications, observability
**Goal:** operate the platform and see what it's doing.

- 🟩 **sqladmin** mounted at `admin.*` (Clerk admin role + MFA + IP allowlist) — CRUD/inspect users, channels, approval queue, read executions/positions/audit (Tech 6, pt 17).
- 🟩 **Custom audited action endpoints**: circuit breaker, channel halt, refund, revoke managed account, Telethon re-assign — confirm + role-gate + `admin_audit_log` (pt 17).
- 🟩 **Notification service** (Arq): events → **Resend** email + **Telegram bot** (critical) + **web push** (PRE 4); locale-aware templates (PRE 1); account-linking deep-link (pt 9, Tech 7).
- 🟦 Grafana dashboards + **alert inbox**: fan-out lag, AI spend vs ceiling, MetaApi/Telethon health, error rates (pt 17, Tech 8).

**Exit criteria:** operator can approve a channel, trip/reset the breaker (audited), and revoke an account from admin; subscriber gets an email + Telegram on execution; key metrics + a disconnect alert show in Grafana.

---

## Phase 9 — Legal gate 🟥  *(hard gate — blocks paid launch)*
**Goal:** legally cleared to take real money in the launch jurisdiction(s).

- 🟥 Finalize ToS, Risk Disclosure, Privacy Policy, Provider Agreement, DPA/subprocessor list (pt 15).
- 🟪 In-app disclaimers (catalog pages, subscribe, execution-config) + consent flows wired to `consent_log` (pt 12/14).
- 🟩 **Geo-gating** — block signups/subscriptions outside counsel-cleared jurisdictions (pt 15).
- 🟩 **Erasure runbook + self-serve data export** (PRE 5): anonymize-PII flow fanning out to Clerk/Stripe/MetaApi/Resend; CSV/JSON account+trade-history export.
- 🟥 Lawyer sign-off on the "tech-tool, not adviser" posture for launch market(s); confirm subscriber-KYC not required (no fund custody) (Tier 3).
- 🟥 Confirm **retention windows** with counsel (statutory financial-record period) (PRE 5).

**Exit criteria:** counsel sign-off; all documents live and acknowledged at signup; geo-gating verified; an end-to-end erasure request anonymizes PII across all processors; data export works.

---

## Phase 10 — Beta → launch
**Goal:** prove it on demo, then take real money.

- **Closed beta on DEMO accounts** — friends/family + first 1–2 hand-onboarded channels; validate the full loop, watch Grafana, tune prompts/overlays.
- Harden: incident runbook, on-call alerting, backup/restore drill.
- **Paid launch** — only after Phase 9 sign-off; start `MaxLots`-equivalent caps low (mirrors desktop's conservative go-live).

**Exit criteria:** demo beta runs clean for an agreed period; legal cleared; first paying subscriber trades on a real account within hard caps.

---

## Milestone rollup

| Milestone | Phases | Outcome |
|---|---|---|
| **M1 — Signal flows end-to-end (internal)** | 0–3 | A channel message trades on a demo account |
| **M2 — Multi-tenant + paid-ready** | 4–6 | Subscribers connect, fan-out works, billing gates access |
| **M3 — Product surfaces** | 7–8 | Dashboards, catalog, admin, notifications, dashboards live |
| **M4 — Launch** | 9–10 | Legal cleared, demo beta, paid launch |

## Cross-cutting (every phase)

- **Testing:** port the desktop hermetic suite; **per-channel replay gate** before any overlay goes live (pt 13B); integration tests against a real Postgres (don't mock the DB — desktop lesson); security test for credential non-persistence (Phase 4).
- **Reuse leverage:** Phase 2 is mostly a *port*, not a rewrite — the riskiest code already exists and is replay-tested.
- **Deferred, do-not-build-now (guard against scope creep):** self-hosted EA + WebSocket layer, chase/ATR, CLL + extraction tool, full catalog discovery, Connect auto-payouts, per-action approval, Telethon pool, full RBAC/break-glass (all v1.1/v2 per pt 18).
- **Standing risk:** single Telethon account — monitor for bans; pool is the first v1.1 reliability add (pt 1).
- **Pre-implementation concerns folded in (PRE 1–6):** i18n/RTL → Phase 0 scaffold + Phase 7 components; timezone/UTC-windows → Phase 1 + 5 + 7; environments/demo-test/feature-flags → Phase 0 + 3; mobile PWA/web-push → Phase 7 + 8; data-retention/erasure → Phase 1 schema + Phase 9 runbook; product analytics → Phase 0. **Tier 2** (notification taxonomy, failure UX, a11y target, anti-fraud, currency display, email DKIM/SPF/DMARC, support tooling) spec'd in their build phase; **Tier 3** (branding/domain, SEO content, subscriber-KYC confirm) parallel GTM.
