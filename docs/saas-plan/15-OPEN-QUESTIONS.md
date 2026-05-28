# 15 — Open Questions

Items where the owner's answer materially changes the plan. Grouped by which phase or decision is blocked. Numbered to allow tracking.

For each item in `docs/08-OPEN-QUESTIONS.md`, classification:
- **Resolved here**: addressed by an assumption or decision in this plan.
- **Surfaced**: owner must answer before the listed phase.

---

## Blocking Phase 0 (decisions phase)

**Q-A — Productization direction (08 Q29, Q30, Q35).** Confirm the target subscriber is **trading-curious-to-pro retail**, NOT institutional. Confirm subscribers bring their own MT5 broker account (Path A). Confirm goal is to commercialize this codebase incrementally, NOT hand off to a fresh team.
- *Plan assumes*: yes to all three (logged in [16](16-ASSUMPTIONS-LOG.md)).

**Q-B — Channel relationship (08 Q31).** Are you the operator of the "Forex Engineer" channel, OR is it a third-party whose IP would need a licensing/partnership? This is the difference between launch and an injunction.
- *Plan assumes*: third-party; signal aggregation under fair-use of the public broadcast OR explicit partnership. **Owner MUST confirm and produce a written agreement before Phase 5.** If third-party refuses, pivot product to "build your own channel" tooling.

**Q-C — Geography of first launch (08 Q32).** Confirm UAE/MENA as launch geography. If owner instead wants EU first, the §10 jurisdiction choice flips to Cyprus + full MiFID II investment-advice path → a 3–6 month delay + license capital + significantly different compliance posture.
- *Plan assumes*: MENA-first via ADGM.

**Q-D — Pricing model (08 Q33).** Confirm flat-tier model from §7 of 01. Profit-share or per-trade pricing is on the table but adds metering complexity and IA-framing risk.
- *Plan assumes*: flat-tier, $39/$99/$249.

**Q-E — Team size (08 Q34).** Confirm 2 engineers full-time for 6+ months. If solo founder, plan extends to 10–12 months; if 4 engineers, plan compresses to 4–5 months but coordination overhead grows.
- *Plan assumes*: 2 engineers.

---

## Blocking Phase 1 (foundation)

**Q-F — Existing operator's preferred broker for the cutover demo.** Which broker hosts the production XAUUSD account today (08 Q7)? Affects: hedging-mode tests, commission-multiplier defaults in fan-out lot math, whether the broker's terms allow our EA.
- *Plan assumes*: a standard MT5 broker that does not prohibit third-party EAs. Owner to confirm and provide an account for the demo.

**Q-G — Telegram session continuity.** Has the Telethon session ever been revoked in this account, and what is the recovery path (08 Q27)? Determines whether we need a backup session strategy for Phase 1.
- *Plan assumes*: rare event; admin self-service re-auth panel covers it (R-07 in §14).

**Q-H — Operator availability for cutover.** 4–6 hour cutover window with operator available to enter Telegram 2FA codes / paste new EA bearer / confirm trade. Weekend OK?
- *Plan assumes*: yes; cutover on a Saturday with markets closed.

---

## Blocking Phase 2 (execution bridge)

**Q-I — MQL5 build floor for EA v2.** What is the minimum MT5 build subscribers will be required to run? Older builds may not support TLS in WebRequest correctly.
- *Plan assumes*: MT5 build ≥ 4150 (verify); document in EA-requirements landing page.

**Q-J — Cert pinning posture.** Are you comfortable with cert pinning in EA v2 (more secure; cert rotation requires EA-side input change) vs. pin-the-CA (slightly less secure; cert rotation transparent)?
- *Plan assumes*: pin-the-cert with a documented rotation runbook + an emergency `ea_cert_pin_bypass` system_config (per RB-07 in §11).

**Q-K — Symbol scope at launch.** Confirm XAUUSD-only for v1. Adding FX majors changes the channel-profile assumption, prompt scope, and orchestrator state-summary block.
- *Plan assumes*: XAUUSD only.

**Q-L — Compound action behavior under multi-subscriber fan-out.** When the LLM emits `[CLOSE_FULL, OPEN]` for a "flip" message, do all eligible subscribers execute both, in order, with the OPEN respecting their personal "position already open" guard? (Hint from current code: yes, per-subscriber state machine.)
- *Plan assumes*: yes — fan-out preserves order; per-subscriber state guards run identically.

---

## Blocking Phase 3 (subscriber app)

**Q-M — Free trial duration + payment-at-trial-start.** Plan assumes 14 days, card required, no charge until trial end. Owner could prefer 7 days OR no-card trial OR a $1 verification charge.
- *Plan assumes*: 14d, card required, no charge.

**Q-N — Plan-tier feature set.** The Starter/Pro/Elite split is opinionated. Owner may want a different feature-tier mapping (e.g., move "Telegram alerts" to Starter as a default).
- *Plan assumes*: as written in §7 of `07-BILLING-AND-IDENTITY.md`.

**Q-O — Default risk profile for new subscribers.** Plan assumes EA defaults (LotsPer100Balance=0.01, ChasePriceEnabled=true, EnableInstantOpen=false). Should a new subscriber's profile match the operator's current production tunables, or be more conservative?
- *Plan assumes*: conservative defaults (0.005 LotsPer100Balance for new subs; opt-in to the higher default after a documented "you understand the risk" gate).

**Q-P — Webhook delivery security.** Should webhook secrets be 1-per-subscriber or 1-per-URL?
- *Plan assumes*: 1-per-URL — each webhook URL has its own HMAC secret, rotatable.

---

## Blocking Phase 4 (admin/ops)

**Q-Q — On-call.** Will the owner participate in the primary on-call rotation, or is a contract L2 hired before public launch?
- *Plan assumes*: owner is primary on-call until subscriber count > 50, then hire L2.

**Q-R — Acceptable subscriber-facing transparency level.** Surface "lot size computed from your balance" to the subscriber on every skipped action with reason `lot_size_zero`? Some operators consider lot math proprietary.
- *Plan assumes*: full transparency to the subscriber on their own actions.

---

## Blocking Phase 5 (compliance + launch)

**Q-S — Counsel choice and timing.** Has a fintech-specialist counsel been engaged in ADGM? If not, week-0 is the latest start. Counsel sets the policy-doc timeline, which is the gating item for Phase 5.
- *Plan assumes*: counsel engaged at week 0.

**Q-T — KYC budget.** Plan assumes Persona; at 100 subs the bill is ~$50/mo. Owner OK with that? Sumsub is cheaper at higher volume but uglier UX.
- *Plan assumes*: Persona for v1; revisit at 1k+ subs.

**Q-U — Refund policy specifics.** 7-day no-questions refund within first cycle. Pro-rated mid-cycle refunds explicitly NOT offered. Acceptable, or do we offer pro-rated?
- *Plan assumes*: no pro-rated; cancel-at-period-end only.

**Q-V — Marketing/advertising review.** Will all marketing copy go through legal review (slows launch; required in some regs) or just compliance-trained marketer?
- *Plan assumes*: legal review for the first 6 months; transition to compliance-trained marketer once a brief is mature.

---

## Carrying forward from docs/08-OPEN-QUESTIONS.md (with disposition)

| 08 Q# | Disposition |
|---|---|
| Q1 — Image/OCR signals | Resolved: out of scope v1. Listener drops images as today. Flag in roadmap. |
| Q2 — Edit events on Telegram | **Surfaced** — has this been observed? If yes, listener needs an `events.MessageEdited` handler. |
| Q3 — Other channels (SMC) | Resolved: only Forex Engineer at launch. SMC is a future provider (post-launch). |
| Q4 — Multi-instrument single message | Resolved: single-symbol invariant preserved; LLM emits symbol-mismatch ALERT for non-XAUUSD content. |
| Q5 — Code signing | Resolved: re-issue + EV cert via CI for the EA bundle download artifact. |
| Q6 — `ea/compile_ea.bat` | Resolved: replace with GitHub Actions Windows runner that invokes `metaeditor64.exe`. |
| Q7 — Broker in production | Surfaced (Q-F). |
| Q8 — `MarketClosedHoliday` | Resolved: EA's existing broker-reject path covers it; no pre-check. Reject reason surfaces in audit. |
| Q9 — `MaxLotsPerSignal=100` | Resolved: lot math moves server-side; cap surfaces in `risk_profiles.max_lots_per_signal`. |
| Q10 — Backup hygiene | Resolved: RDS automated PITR replaces operator-initiated backup. |
| Q11 — DPAPI machine migration | Resolved: KMS replaces DPAPI; not applicable post-cutover. |
| Q12 — `_cleanup_message_if_orphan` | Resolved: keep no-op; tombstone behavior preserved in Postgres. |
| Q13 — Broker server time | Resolved: every server-side timestamp is `now() at time zone 'utc'`; EA-side broker time only used in EA's local reconciliation scan. |
| Q14 — Daily LLM spend | **Surfaced** — owner share actual figure to size budget alerts. |
| Q15 — Prompt-injection attacks observed | **Surfaced** — drives whether we add regression tests early. |
| Q16 — `evaluator_version='v2'` rollout | Resolved: v2 is default in new schema. |
| Q17 — `EnableScoreTiedSizing` | Resolved: keep off; behind a feature flag with owner-only access in v1. |
| Q18 — Trigger matcher cold-start latency | Resolved: warmup hook on signal-svc container startup pre-loads embeddings. |
| Q19 — Multi-channel routing status | Resolved: discarded; replaced by per-subscriber subscriptions to providers. |
| Q20 — v2 routing in prod | Resolved: discarded. |
| Q21 — `bot_outbox` legacy/v2 | Resolved: discarded. |
| Q22 — GUI installer audience | Resolved: discarded. |
| Q23 — v1→v2 migration | Resolved: a different migration (SQLite→Postgres) replaces it. |
| Q24 — Test failure rate | **Surfaced** — owner share current pass rate before Phase 1 starts. |
| Q25 — Stack switcher state | Resolved: discarded. |
| Q26 — Uptime target | **Surfaced** — plan assumes 99.5% (~3.5h/mo) for v1. Confirm. |
| Q27 — Session revoke history | Surfaced (Q-G). |
| Q28 — On-call rotation | Surfaced (Q-Q). |
| Q29–Q35 | Surfaced (Q-A through Q-E). |

---

## New questions raised by this plan (not in 08)

- **Q-X1**: Does the operator want to incorporate a HOLDING entity (BVI) above the OpCo (ADGM)? Affects tax + future fundraising. Plan assumes yes.
- **Q-X2**: Is there budget for the AWS+vendor floor (~$500/mo) before the first paying subscriber? If not, defer Phase 1 ECS provisioning and run signal-svc/listener on Fly.io for the dev/staging period to keep cost <$100/mo.
- **Q-X3**: Are you comfortable with the channel-profile JSON being editable through admin UI by support-role accounts? Plan currently gates editing to admin-role only; publication requires explicit "publish + bump version" — could be loosened.
- **Q-X4**: Will the EA v2 be open-sourced or closed-source? Affects: subscriber trust narrative ("I can audit the EA"), copy protection risk, ability to fork. Plan assumes closed-source binary distribution with a signed `.ex5`.
- **Q-X5**: Currency handling — subscriber balance can be in EUR, GBP, JPY, etc. (broker's account currency). The fan-out worker's max-daily-loss and min-balance are stored in cents-USD. Convert via what FX source? Plan assumes a daily FX-rate snapshot from a free source (e.g., ECB reference rates) cached in `system_config`.
- **Q-X6**: Operator's preference on dark/light theme at launch — plan ships dark-only; light theme post-launch.
- **Q-X7**: Marketing channel mix — Telegram ads, Google, Twitter, content. Plan defers this fully to the owner.
