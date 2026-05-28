# 14 — Risk Register

Probability × Impact = priority. P × I are 1–5. Top entries reviewed monthly; full review quarterly.

Categories: TECH (technical), VEND (vendor), REG (regulatory), FIN (financial), OPS (operational), SEC (security).

| ID | Cat | Risk | P | I | P×I | Mitigation | Owner | Trigger |
|---|---|---|---|---|---|---|---|---|
| R-01 | REG | ADGM license takes > 6 months; can't legally take payments | 4 | 5 | 20 | Open BVI as Plan-B jurisdiction in parallel during Phase 0; legal opinion on serving MENA via BVI | Owner | License timeline review at week 8 |
| R-02 | REG | A market we serve reclassifies copy-trading as discretionary IA mid-flight | 3 | 5 | 15 | Geo-block on signup; per-country counsel review for amber list; framing as "signal publisher" in all copy | Owner+Counsel | Quarterly counsel scan |
| R-03 | TECH | EA v2 transport rewrite introduces a regression in `ManagePlans` staged-close ladder | 3 | 5 | 15 | Cutover gate: all `tests/test_management_replay.py` fixtures pass against live Anthropic in CI before EA v2 ships; demo-account dry-run for ≥7 days | Eng lead | First demo failure |
| R-04 | TECH | Postgres RLS misconfig leaks subscriber A's positions to subscriber B | 2 | 5 | 10 | RLS policies tested per-table in CI (every table with `subscriber_id` has a "user A cannot read user B's row" test); pgaudit on all bypasses | Eng lead | New table merged without RLS test |
| R-05 | SEC | EA bearer token leaked in subscriber logs or screenshots | 4 | 4 | 16 | Show token ONCE; per-EA bcrypt hash only; one-click rotate from UI; per-token-hash anomaly detection (geo or rate spike) → auto-revoke + alert subscriber | Eng lead | First leaked token report |
| R-06 | VEND | Anthropic outage > 4h | 3 | 4 | 12 | OpenAI failover wired + tested; degraded-interpreter banner; subscribers see ALERT-only mode | Eng lead | First Anthropic incident |
| R-07 | VEND | Telegram revokes the listener's user-account session | 4 | 5 | 20 | Admin self-service re-auth panel; documented runbook; reduce reconnect attempts to stay below abuse threshold; consider applying for an MTProto-server allowlist if Telegram offers one | Owner | First revoke event |
| R-08 | TECH | Cost-guard fails; LLM bill spikes | 2 | 4 | 8 | Hard daily budget in Anthropic + OpenAI dashboards; cost guard tested via fixture; CloudWatch billing alarm | Eng lead | First budget breach |
| R-09 | TECH | Idempotency-key collisions in EA POSTs | 1 | 4 | 4 | Idempotency keys include attempt_n + bearer_hash; window 24h; tests for replay | Eng lead | Duplicate position in audit |
| R-10 | OPS | Founder is sole holder of Telegram session; founder unavailable | 4 | 5 | 20 | Operator-side session blob backed up encrypted to KMS-protected S3 with two key custodians; documented session-recovery procedure | Owner | Quarterly drill |
| R-11 | OPS | One person on call; no escalation | 3 | 4 | 12 | Phase-4 hire of an L2 ops contractor; PagerDuty escalation policy enforces secondary | Owner | First P1 missed-SLO |
| R-12 | FIN | Paddle holds revenue during reserve/risk review | 3 | 4 | 12 | Cash reserve of 3 months opex held outside Paddle; second processor (Stripe) account opened but inactive | Owner | First reserve notice |
| R-13 | REG | KYC vendor (Persona) flags a high-net-worth subscriber as high-risk, blocking | 3 | 3 | 9 | Manual review queue with admin override + counsel sign-off; bypass requires CTO + counsel co-sign | Owner+Counsel | First override request |
| R-14 | TECH | Fan-out worker stalls; subscriber actions don't materialize | 3 | 5 | 15 | Health-check: alert when `signal_actions.fanout_completed_at IS NULL` count > 5 for > 60s; ECS auto-scale; manual replay tool | Eng lead | First alert |
| R-15 | SEC | Subscriber's bearer token logged in error reports | 3 | 4 | 12 | Sentry beforeSend strips bearer; Datadog grok pipeline strips bearer; explicit unit tests | Eng lead | Audit finds a leak |
| R-16 | TECH | MetaTrader build change breaks WebRequest TLS | 2 | 5 | 10 | EA-version compatibility matrix in CI (verify against MT5 builds N, N-1); EA dashboard shows MT5 build for support | Eng lead | MT5 release breaks polling |
| R-17 | OPS | Subscriber MT5 silently down; subscriber unaware | 4 | 3 | 12 | `quarantine_dead_connections` worker; email+Telegram at 5m and 30m; admin dashboard EA fleet status | Ops | First "I didn't know my EA was off" ticket |
| R-18 | REG | EEA visitor signs up undetected via VPN; we hold EU data without basis | 3 | 4 | 12 | IP-intelligence vendor (IPinfo) + KYC corroboration + ToS clause "you confirm not EEA resident" | Owner+Counsel | First VPN-flagged subscriber |
| R-19 | FIN | LLM cost grows non-linearly with subscribers (cache miss patterns) | 3 | 3 | 9 | Per-provider budget; prompt-caching telemetry; weekly cost review; price tiers built to absorb $X per sub | Eng lead | Daily LLM cost / sub > $1 |
| R-20 | TECH | Postgres single-region; AWS region outage = full downtime | 1 | 5 | 5 | Multi-AZ at launch; cross-region read replica + Route53 failover at 500 subs | Eng lead | First incident-impact assessment |
| R-21 | SEC | Operator's admin laptop compromised → admin session hijacked | 2 | 5 | 10 | MFA required for admin; admin-IP allowlist optional; hardware key (YubiKey) for owner; session timeout 8h | Owner | First suspicious admin login |
| R-22 | OPS | Channel goes silent (Forex Engineer stops posting) | 2 | 5 | 10 | Single-provider risk is concentrated; product roadmap targets 2nd provider in year-2; provider relationship contract | Owner | Channel-silent alert > 5d |
| R-23 | REG | Channel posts a "guaranteed return" claim we then redistribute | 2 | 4 | 8 | Commentary filter strips marketing claims; legal review of profile prompt; disclaimer footer on every notification | Eng+Counsel | First flagged forwarded claim |
| R-24 | TECH | Telethon backfill races with live messages, producing duplicate processing | 2 | 3 | 6 | UNIQUE(provider_id, tg_chat_id, tg_message_id) constraint; idempotent backfill walker | Eng | First duplicate `messages` insert |
| R-25 | TECH | KMS CMK accidentally deleted | 1 | 5 | 5 | KMS deletion 30-day grace; admin action requires "I am deleting a CMK" plain-text confirmation; CloudTrail alarm on `kms:ScheduleKeyDeletion` | Eng lead | First deletion event |
| R-26 | VEND | Clerk pricing change | 2 | 2 | 4 | Quarterly vendor-pricing review; Lucia fallback engineering doc kept current | Eng lead | Pricing-change announcement |
| R-27 | OPS | Mass-cancellation by subscribers after a bad-signal day | 3 | 3 | 9 | Risk disclosure prominent; daily loss caps enforced; transparent "why skipped" tooling reduces "missed-trade" anger | Owner | Cancel rate > 5%/wk |
| R-28 | SEC | A subscriber's broker credentials are exfil'd from a compromised cloud (Path B if we add MetaApi later) | 2 | 5 | 10 | Path A at launch eliminates this; Path B introduction triggers a re-risk-assessment | Eng lead | If/when Path C is added |
| R-29 | TECH | Schema migration drops/renames a column with subscriber data | 2 | 5 | 10 | Expand-contract policy; CI blocks destructive DDL without an explicit `--allow-destructive` flag | Eng lead | First blocked migration |
| R-30 | FIN | Refund spike (>10% of MRR in a month) | 2 | 4 | 8 | 7-day refund policy bounded; refund reasons categorized; iterate product to reduce primary cause | Owner | Monthly refund review |
| R-31 | OPS | Sole engineer with EA / MQL5 expertise leaves | 2 | 5 | 10 | Documentation; pair-programming; EA in a separately-maintained repo with code-tour | Owner | Quarterly bus-factor review |
| R-32 | REG | UAE-specific advertising rules on financial products (truthful, "past performance" disclosure mandatory) violated unwittingly | 3 | 3 | 9 | Marketing copy reviewed by counsel; ad creatives pre-approved | Owner+Counsel | First UAE-regulator letter |

---

## Top 5 (by P×I)

1. **R-01 ADGM license timeline** — 20
2. **R-07 Telegram session revoke** — 20
3. **R-10 Founder is sole session-holder** — 20
4. **R-05 EA bearer token leaked** — 16
5. **R-03 EA v2 regression in ManagePlans** — 15 (tied with R-02, R-14)

These get explicit mitigation milestones in Phase 0–2 and are reviewed monthly.

---

## Risks explicitly outside this register

- General market risk (XAUUSD goes bid-down for 6 months) — that's the customer's risk, not ours.
- Channel signal quality risk — the customer subscribed knowing it's a signal service.
- Cryptocurrency / DeFi exposure — none in v1.
- Multi-asset risk — XAUUSD only in v1.
