# 00 — Executive Summary

## What we're building

Evolve **CopyTrades** from a single-operator Telegram→MT5 signal bridge for XAUUSD into a **multi-tenant subscription SaaS** that serves paying retail subscribers in MENA. Subscribers install our MT5 EA on their own machine (or our managed-VPS bundle), link their broker, and the cloud platform fans out each parsed Telegram signal to all eligible subscribers — with per-subscriber lot sizing, risk gates, and audit trail. 10–50 paying subscribers within 6 months of launch; architecture designed for 500–2000 without a redesign.

The IP — the ~11 KB interpreter system prompt, the 16 typed action validators, the staged-management policy in the MQL5 EA, the orchestrator's stage cascade — is preserved verbatim. The transport, the Windows-bound storage and secret stack, and the operator-only desktop UI are rewritten.

## The architectural pre-decisions

| # | Decision | Choice | Rationale (one line) |
|---|---|---|---|
| 1 | Execution model | **Path A: subscriber-installs-EA + cloud API** (+ managed-VPS option) | Keep the EA IP; cleanest "publisher-of-signals" regulatory framing; per-subscriber failure isolation |
| 2 | Hosting | **AWS** eu-west-1 (or me-south-1) | Compliance posture, vendor maturity, all-in-one |
| 3 | Data store | **RDS Postgres 16 Multi-AZ** | ACID + RLS + PITR; Neon for dev branches |
| 4 | Business stack | **Next.js 15 + tRPC + Drizzle + shadcn/ui** | Owner is JS native; one repo end-to-end types |
| 5 | Signal pipeline language | **Keep Python** (FastAPI container) | The prompt + orchestrator are the moat |
| 6 | Queue | **pg-boss**; no queue on EA hot path | Transactional with action lifecycle |
| 7 | Billing | **Paddle (merchant-of-record)** for v1; Stripe later | Tax/VAT eaten by Paddle for ~5% |
| 8 | Auth | **Clerk** | Two engineers ≠ an auth team |
| 9 | Secrets | **AWS KMS envelope encryption** in Postgres + Secrets Manager for app secrets | Replace DPAPI; per-tenant DEKs |
| 10 | Jurisdiction | **ADGM (UAE)** | MENA buyer + fintech-friendly + "signal publisher" framing |

Day-1 geo-blocks: US, Canada, UK, EEA, Switzerland, Australia, New Zealand, China, Iran, NK, Syria, Cuba, OFAC SDN, Russia. MENA core; LatAm + SE Asia + sub-Saharan Africa per-country review.

## Pricing

| Tier | Price | Headline feature |
|---|---|---|
| Starter | $39/mo | 1 broker · $5k acct · email |
| Pro | $99/mo | 3 brokers · $50k acct · Telegram alerts · priority |
| Elite | $249/mo | 10 brokers · unlimited acct · webhook · phone support |
| Managed VPS add-on | $19/mo per connection | We host MT5 + EA in your name |

14-day trial, card required, no charge until trial end.

## Phased roadmap (2 engineers, week 0 = today)

| Phase | Weeks | Scope | Exit |
|---|---|---|---|
| **0** Decisions | 0–2 | Owner sign-off; ADGM filing; counsel engaged; CI/CD skeleton | All 10 pre-decisions ratified |
| **1** Foundation | 2–8 | Postgres schema + Clerk + signal-svc cloud-ported + Python↔Node contract | New user signup → orchestrator emits actions visible in admin |
| **2** Execution bridge | 8–14 | EA v2 + fan-out worker + operator cutover | Operator's own broker executes a Telegram signal end-to-end via the cloud |
| **3** Subscriber MVP | 14–18 | Subscriber web app + billing + notifications | Non-engineer subscriber onboards, pays, trades |
| **4** Admin + Ops | 18–22 | Admin UI + runbooks + observability + audit | All P1 incidents have a tested runbook |
| **5** Compliance + Soft launch | 22–26 | Policy docs + KYC + 10 friends-and-family | First paying external subs; no P1 in launch week |
| **6** Public launch | 26–32 | Acquisition + scale to 50 | 50 paid subs · churn < 10%/mo |

Total to soft launch: ~6 months. To 50 paying subs: ~8 months.

## Top 5 risks (full register in §14)

1. **ADGM license takes longer than 6 months** — Plan B BVI in parallel; legal opinion on serving MENA via BVI; decision gate at week 8.
2. **Telegram revokes the listener's user-account session** — Admin self-service re-auth panel; runbook; quarterly drill.
3. **Founder is sole holder of the Telegram session** — Encrypted backup of session blob with two key custodians; documented recovery procedure.
4. **EA bearer token leaked by a subscriber** — Token shown once; per-EA bcrypt'd; one-click rotate; anomaly detection auto-revokes.
5. **EA v2 regression in the staged-management ladder (the 2026-05-27 incident class)** — All `test_management_replay.py` fixtures must pass against live LLM before EA v2 ships; demo-account dry-run ≥ 7 days.

## Top 5 open questions for the owner (full list in §15)

1. **Channel relationship (15 Q-B)** — Are you the operator of the "Forex Engineer" channel, or is it a third-party whose IP needs licensing? Without a written agreement this is a blocker for Phase 5.
2. **Productization direction (15 Q-A)** — Retail vs institutional; Path A vs B; incremental-commercialization vs handoff. Plan assumes retail / Path A / incremental.
3. **Launch geography (15 Q-C)** — MENA-first via ADGM is the plan. If EU-first, the timeline + jurisdiction + capital requirements all flip.
4. **Existing broker for cutover demo (15 Q-F)** — Which production broker? Demo account available?
5. **Counsel engaged by week 0 (15 Q-S)** — Phase 5 is gated on policy docs; counsel kickoff is the critical-path item from day one.

## Cost snapshot (verify all figures before commit)

| Scale | AWS+vendor monthly |
|---|---|
| 0 subs | ~$490 |
| 50 subs | ~$750 |
| 500 subs | ~$1,540 |
| 2,000 subs | ~$3,100+ |

At 100 paid × $99 = $9,900 MRR · margin healthy after Paddle fees. At 500 paid × $99 = $49.5k MRR · margin excellent.

## What this plan does NOT cover (deferred)

Mobile native apps · marketplace of signal providers · second asset class · centralized broker (MetaApi) · social/leaderboard features · institutional / B2B onboarding · advanced backtester · light theme · BYO LLM keys.

## How to read the rest

1. **§01 — Architectural Decisions**: every irreversible choice and its rationale.
2. **§02 — Preserve and Discard**: file-by-file catalog of what survives.
3. **§03 — Data Model**: full Postgres DDL.
4. **§04 — Execution Layer**: EA v2 design and the EA API surface.
5. **§05 — Signal Fan-out**: how one signal becomes N subscriber actions.
6. **§06 — Web App**: routes, components, tRPC API.
7. **§07 — Billing and Identity**: Paddle, Clerk, plan tiers, KYC.
8. **§08 — Communications**: email, Telegram, webhook, in-app.
9. **§09 — Compliance and Legal**: jurisdiction, geo-blocks, policies, GDPR.
10. **§10 — Infrastructure**: AWS topology, CI/CD, cost.
11. **§11 — Observability and Support**: audit trail, impersonation, runbooks.
12. **§12 — Migration and Sunset**: operator cutover from SQLite to cloud.
13. **§13 — Phasing and Milestones**: ticket-level breakdown per phase.
14. **§14 — Risk Register**: every material risk with mitigation.
15. **§15 — Open Questions**: what the owner must answer and when.
16. **§16 — Assumptions Log**: every defensible default and what changes if wrong.

The 17 docs are internally consistent. If you find a contradiction, trust §01 over the others — every decision flows from there.
