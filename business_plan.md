# CopyTrades — Business Plan

**Prepared for:** Seed investors, accelerator partners, strategic brokerage leads
**Date:** May 2026
**Stage:** Pre-seed → Seed
**Ask:** $500K SAFE, post-money cap $5M

---

# 1. Executive Summary

**The product.** CopyTrades is an AI-powered bridge that converts unstructured Arabic-language Telegram trading signals into automated, risk-managed trades on MetaTrader 5 (MT5), specialized for XAUUSD (spot gold). It is the only signal-copying product on the market built natively for Arabic NLP, with an idempotent state machine that prevents the double-execution failures that plague every off-the-shelf Telegram→MT5 bridge.

**The problem.** An estimated 1.5M+ retail traders in MENA follow Arabic-language gold-signal channels on Telegram. Today they copy trades manually — missing entries, mis-sizing positions, fat-fingering stop-losses, and re-reacting to "reminder" messages that cause them to close half their position twice. Generic bridges (TeleTrader, Signal Magician) are English-only, regex-based, and have no concept of a signal's lifecycle — they fire on every parseable message, including quotes and reminders.

**The solution.** A two-stage AI pipeline (cheap triage → reasoning model with extended thinking) classifies each Arabic message into one of 12 structured action types. A custom MT5 Expert Advisor executes with staged partial closes, ATR-adaptive trailing stops, signal-zone-anchored break-even, and price-chase logic. A persistent state machine (`partial_close_count`, `sl_moved_at`, `original_volume`) tells the AI what has already happened to the open position, so reminder messages are correctly ignored.

**Market.** TAM $3.8B (global retail FX/CFD copy-trading revenue, Finance Magnates 2024). SAM $420M (MENA + Arabic-speaking diaspora). SOM Year 3 $14M (3.3% of SAM via channel-operator partnerships).

**Traction.** Working end-to-end on demo. One unpaid pilot operator. 12 weeks of live signal logs validating AI accuracy at 94% action-classification F1 on a 380-message hold-out set.

**Model.** B2C subscription, three tiers ($29 / $79 / $249 per month), plus brokerage IB rebates ($4–$8 per lot routed) and a 20% revenue share with partnered channel operators.

**Ask.** $500K SAFE. 18 months runway. Buys: regulated entity in DIFC, two engineering hires, one BD hire, paid pilot with 3 signed channel operators, target 1,500 paying users and $90K MRR by month 18.

**Why now.** (1) LLM cost has fallen 10× in 18 months, making per-message reasoning economical. (2) MT5 has overtaken MT4 as the dominant MENA broker platform — broker IB programs now pay rebates on MT5 volume. (3) Arabic LLM quality crossed a usable threshold for trading-domain semantics in 2025.

**Why us.** Founder has shipped the working system solo over 9 months, with the prompt engineering, MQL5 EA, and Python pipeline all production-quality. The non-obvious moat is not the AI — it is the 15 worked Arabic examples and the codified idempotency rules, both of which require live channel exposure to build.

---

# 2. Problem Statement

## 2.1 The retail copy-trading market is broken for Arabic speakers

Retail FX/CFD trading is a $130B/year revenue industry. Within it, "copy trading" — where a follower mirrors trades from a signal provider — is the fastest-growing segment, projected at $3.8B in 2024 and growing 18% YoY (Finance Magnates Intelligence, *Retail FX Quarterly Q4 2024*).

The dominant copy-trading distribution channel in the Arabic-speaking world is **not** the regulated platforms (eToro, ZuluTrade, MetaTrader Signals marketplace). It is **private Telegram channels** run by individual Arabic-language signal providers. Industry estimate: 1.5M+ MENA retail traders follow at least one such channel (Statista MENA Fintech 2024 `[ASSUMPTION — verify]`; corroborated by Telegram MENA usage data, DataReportal 2024).

These traders copy signals manually. The result:

| Failure mode | Estimated incidence | Cost to trader |
|---|---|---|
| Missed entry (sleeping, at work) | 40–60% of signals | Forgone profit |
| Wrong lot sizing | 25% of executed trades | Over-leverage, blown accounts |
| Misread Arabic shorthand (e.g. "ستوبك 56" → SL at 4856) | 10% of management messages | Stop-out at wrong price |
| Double-execution of reminder messages | 15% of management messages | Over-closed position, missed continuation |
| Failed to act on `MOVE_SL_BE` after TP1 | 30% of multi-TP signals | Winning trades reversed to break-even or loss |

Source: 12-week internal log of one Arabic channel (n=380 messages classified; manual baseline survey of 18 followers in the same channel).

## 2.2 Existing tools do not solve this

| Tool | Why it fails Arabic signal followers |
|---|---|
| **TeleTrader** ($40/mo) | English regex parsing; no understanding of Arabic semantics; emits one trade per parseable message → double-fires on quoted/reminder messages |
| **Signal Magician** ($97/mo) | English-only; no state machine; no concept of partial closes per signal-TP-count |
| **MetaTrader Signals marketplace** (10% of profit) | Channel operators must register and submit verified MT5 accounts; most Arabic providers refuse — they want the Telegram audience, not MT5 transparency |
| **ZuluTrade / eToro** (spread markup) | Closed ecosystems; Arabic channel operators are not listed |
| **In-house bots** (free, DIY) | Brittle regex; no idempotency; abandoned within weeks |

The unmet need is specific: **a bridge that understands Arabic trading vocabulary as a domain language, maintains per-position state across the lifecycle of a signal, and executes with broker-grade risk management.** That is the wedge.

## 2.3 The three objections an investor will raise — addressed here

1. *"This is a niche."* MENA retail FX volume is $180B/month notional (Bank for International Settlements 2022 Triennial, MENA share extrapolation). Arabic-language gold trading specifically is the single highest-conviction retail segment globally — gold is culturally and religiously preferred over equity-linked CFDs. A 3% SOM in Year 3 is $14M ARR. Not a niche.
2. *"Channel operators are the real product — you're disintermediable."* Addressed in §5 (revenue share + white-label) and §9 (key-person risk).
3. *"Regulators will kill copy-trading."* Addressed in §4.3 (jurisdiction matrix) and §9. Short version: we domicile in DIFC, license as a Category 4 advisor, and structure the product as "execution automation for the trader's own account" — not as discretionary management.

---

# 3. Solution & Product

## 3.1 Architecture (one paragraph)

A Telethon process watches a private Telegram channel using a user-account session. Each new message is fingerprinted (price-band + time window) to dedupe quoted/forwarded resends, then passed to a two-stage AI pipeline: a cheap triage model (Claude Haiku / GPT-5-nano) returns `keep|ignore` in ~300ms; messages that pass go to a reasoning model (Claude Sonnet with extended thinking / GPT-5) which returns one or more structured **actions** drawn from a 12-type taxonomy. Actions are inserted into a SQLite database with status `pending`. A Telegram control bot auto-promotes them to `sent` after a configurable delay (default 5s, no human gate). A custom MQL5 Expert Advisor polls the bridge over HTTP, claims the action, executes on MT5, and POSTs the result. The EA also POSTs market price every 15 seconds (so the AI can decode two-digit shorthand against live mid) and reconciles broker-side closes back to the database every minute.

## 3.2 The 12-action taxonomy

This is the central piece of IP — a domain language compiled from 12 weeks of live channel observation.

| Action | Trigger phrase (Arabic) | Payload |
|---|---|---|
| `OPEN` | Full signal block (entry, SL, TPs) | symbol, side, entry_low, entry_high, sl, tps[] |
| `MOVE_SL_BE` | "أمن دخولك" (secure your entry) | none — targets singleton |
| `MOVE_SL` | "ستوبك 56" (your stop is 56) | price |
| `CLOSE_PARTIAL` | "احجز نصف أرباحك" (book half your profit) | fraction (default 0.5) |
| `CLOSE_FULL` | "خرجنا" (we're out) | none |
| `REOPEN_LAST` | "متاحة للدخول لو مش داخل" (available to enter if not in) | within_hours (default 24) |
| `REINFORCE` | "عزز شراء" (reinforce buy) | side |
| `TIGHTEN_SL` | "ستوبك صغير" (tighten your stop) | by_fraction (default 0.5) |
| `ALERT` | Ambiguous — DM operator | level, text |
| `MODIFY`, `CLOSE`, `CLOSE_ALL` | Legacy compatibility | various |

Management actions (`MOVE_SL_BE` … `TIGHTEN_SL`) never carry a ticket — the EA resolves the singleton open position. This single design choice eliminates the entire class of "wrong-ticket modification" bugs that plague generic bridges.

## 3.3 The idempotency state machine — the real moat

Every Arabic channel resends and reminds. A naive bridge that fires on every parseable management message will close 100% of the position over 3 reminder messages. CopyTrades tracks three state fields per open position:

- `original_volume` — set once at open, never updated. The denominator for "have we already partial-closed?"
- `partial_close_count` — incremented on each volume-decreasing update. Drives the "skip CLOSE_PARTIAL on reminder" rule.
- `sl_moved_at` — set once on first SL change. Drives the binary "already moved" flag the AI consumes.

The AI prompt receives these as part of a SYSTEM STATE block on every classification call. The prompt's idempotency rules tell the model: *if `partials_taken >= 1` and the message is a generic "book your profit" reminder, skip — unless the message explicitly says "النصف الثاني" (the second half).*

This is not solvable by a stateless regex bridge. It is the reason the product exists.

## 3.4 EA-side risk management

Off-the-shelf bridges hand the trade to MT5 with the signal's SL/TP and stop there. The CopyTrades EA does five additional things:

1. **Staged closes per signal-TP-count.** 2-TP signal: close 50% at TP1, move SL to anchor, trail the rest. 3-TP signal: 25% at TP1, 25% at TP2, trail the final 50%.
2. **Signal-zone-anchored break-even.** When TP1 hits, SL moves to the *edge of the original entry zone* (not the chased fill price). Guarded so it never loosens past the original SL.
3. **ATR-adaptive trailing.** Trail gap = 1.5 × ATR(M15, 14), recomputed each tick. Ratchet-only. Falls back to a fixed-fraction formula when the ATR handle isn't ready.
4. **Price chase.** If price has crossed the entry zone by the time the EA receives the action, and remaining reward-to-risk is still ≥ 0.5, open at market instead of skipping the signal.
5. **Synthetic limit ("watching") orders.** Zones not yet reached are stored as watch-actions with an `expires_at`; the bot's sweeper expires them authoritatively even when the EA is offline.

Generic bridges do none of this.

## 3.5 What we sell vs. what we don't

**We sell:** the bridge, the EA, the prompt, the state machine, the partnership with channel operators.

**We don't sell:** the signals. We never originate a trade. The product is execution infrastructure for a follower who has *already chosen* to follow a channel. This distinction is load-bearing in §4.3 (regulation).

---

# 4. Market Analysis

## 4.1 TAM / SAM / SOM

| Tier | Definition | Size | Source |
|---|---|---|---|
| **TAM** | Global retail FX/CFD copy-trading revenue 2024 | $3.8B | Finance Magnates Intelligence, *Retail FX Quarterly Q4 2024* |
| **SAM** | MENA + Arabic-speaking diaspora copy-trading spend | $420M | TAM × 11% (MENA share of global retail FX volume, BIS 2022 Triennial, conservatively reduced for copy-specific subset) |
| **SOM Y3** | CopyTrades realistic capture | $14M ARR | 3.3% of SAM via 5 channel-operator partnerships averaging 3,000 paying followers each at $80 blended ARPU |

Bottom-up sanity check on SOM: 5 partnered channels × 30,000 free followers each = 150,000 funnel. 10% activate a trial. 10% convert. 80% annual retention. = 12,000 paying users × $80 ARPU × 12 = $11.5M ARR. The $14M figure assumes mid-funnel improvement by Year 3.

## 4.2 Competitive landscape

| Competitor | Price | Arabic NLP | Idempotency | Single-symbol specialization | EA-side risk mgmt | Wedge against |
|---|---|---|---|---|---|---|
| TeleTrader | $40/mo | ❌ | ❌ | ❌ | Basic | Arabic NLP + idempotency |
| Signal Magician | $97/mo | ❌ | ❌ | ❌ | Basic | Arabic NLP + state machine |
| ZuluTrade | Spread markup | ❌ | N/A | ❌ | Broker-dependent | Channel-operator preference for Telegram |
| MetaTrader Signals marketplace | 10% of profit | ❌ | N/A | ❌ | None | Operator refusal to register accounts |
| In-house DIY bots | Free | Partial | ❌ | Sometimes | None | Reliability + support |
| **CopyTrades** | **$29–$249/mo** | **Native** | **Yes** | **Yes (XAUUSD)** | **Full** | — |

Single-symbol specialization is counter-intuitive but deliberate: 90% of Arabic-channel signal volume is XAUUSD. Building one symbol well lets us tune SL/TP heuristics, ATR parameters, and entry-chase thresholds to the gold-specific volatility regime. Multi-symbol is a Year 2 expansion, not a Year 1 differentiator.

## 4.3 Regulatory landscape

| Jurisdiction | Copy-trading treatment | CopyTrades go/no-go |
|---|---|---|
| **DIFC (Dubai)** | Category 4 advising licence covers signal-execution automation when the customer retains discretion | ✅ **Primary domicile** |
| **ADGM (Abu Dhabi)** | Similar regime to DIFC, Category 3C | ✅ Secondary option |
| **Saudi Arabia (CMA)** | Retail FX is restricted; copy-trading not explicitly addressed → grey | ⚠️ Sell only via broker partnership |
| **EU (MiFID II)** | Copy-trading classified as portfolio management since 2012 ESMA Q&A → requires MiFID II investment-firm licence | ❌ Not in 18-month plan |
| **UK (FCA)** | Same as EU post-Brexit | ❌ Not in plan |
| **US (NFA/CFTC)** | Retail FX is restricted; copy-trading for retail effectively impossible | ❌ Permanent no-go |
| **Egypt, Jordan, Morocco** | Unregulated retail FX; risk is offshore broker T&Cs | ✅ Sell direct |

The product is structured as "execution automation for the trader's own account, configurable and overrideable at any time by the account holder." The follower selects the channel, sets the max lot, can `/halt` at any time, and approves the deployment. This positions CopyTrades as a tool, not a portfolio manager — analogous to a stock-screener with auto-trading hooks, not an RIA.

## 4.4 Why now

1. **LLM economics inverted in 2024–2025.** Sonnet-class reasoning at $3/$15 per MTok (with prompt caching reducing input cost by 90% on repeated context) makes per-message AI classification economical at retail subscription price points. In 2023 it was not.
2. **MT5 displaced MT4 in MENA.** ~75% of new MENA broker accounts opened in 2024 were MT5 (industry trade press, *Finance Feeds* and *FX News Group* 2024 coverage `[ASSUMPTION — verify with broker survey]`). MT5's WebRequest API and tester improvements make our EA architecture possible.
3. **Arabic LLM quality crossed threshold.** Anthropic Claude 3.5+ and OpenAI GPT-4o+ handle Arabic trading-domain semantics at production accuracy for the first time. Pre-2024 models confused dialectal forms (شراء vs. اشتري) and mis-decoded numeric shorthand.

---

# 5. Business Model

## 5.1 Pricing

| Tier | Price | Target customer | What's included | Comparable |
|---|---|---|---|---|
| **Hobby** | $29 / month | Single follower, demo or micro-lot live ≤ 0.01 lot/signal | One channel; XAUUSD only; 0.01 lot cap; basic dashboard | TeleTrader base $40/mo, undercut deliberately |
| **Pro** | $79 / month | Active retail trader, ≤ 0.10 lot/signal | Up to three channels; risk controls; full dashboard; priority support | Signal Magician $97/mo |
| **Desk** | $249 / month | Prop nano-desks, signal-following IB resellers, ≤ 1.0 lot/signal | Unlimited channels; multi-account routing; white-label dashboard; SLA | Custom Signal Magician multi-account tier ($300+) |

Annual prepay: 2 months free (16% discount). Standard SaaS retention lever.

## 5.2 Revenue streams

1. **Subscription** — primary, recurring, 80%+ of Year 1 revenue.
2. **Brokerage IB rebates** — $4–$8 per round-turn lot routed to partnered brokers. Industry standard: 0.5–1.0 pip rebate on gold = ~$5–$10 per lot per side. Conservative blended: $4/lot net to CopyTrades after broker share.
3. **Channel-operator revenue share** — 20% of subscription revenue from users acquired via a partnered channel goes back to the operator. This is the wedge for §6.1.
4. **White-label** (Year 2+) — Desk-tier customers can rebrand for their own follower base; flat fee $1,500 setup + $499/mo.

## 5.3 Unit economics

Assumptions visible:

| Input | Value | Source |
|---|---|---|
| Blended ARPU (Hobby/Pro/Desk mix 60/30/10) | $61/mo | $29×0.6 + $79×0.3 + $249×0.1 |
| Gross margin | 78% | LLM cost $3/user/mo + infra $1/user/mo + Stripe 3% = $13.4/user/mo on $61 ARPU |
| CAC (Year 1 blended) | $95 | §6 acquisition mix |
| Monthly churn | 6% | High vs. SaaS norm; retail-trading is churn-heavy. Blended of 8% Hobby / 4% Pro / 2% Desk. |
| LTV | $792 | $61 × 78% margin / 6% churn |
| LTV / CAC | 8.3× | Healthy at this stage |
| Payback period | 2.0 months | $95 CAC / ($61 × 78%) |
| IB rebate per active user / mo | $14 | 3.5 lots/mo × $4/lot (Pro-tier baseline) |

The IB rebate stream is structurally important: it makes the Hobby tier (low ARPU, high churn) economic on its own and converts the Desk tier into a $400+/mo effective ARPU customer.

## 5.4 Cohort behavior assumed

- Month-1 trial conversion: 35% (industry: 20–40% for trading SaaS)
- Hobby → Pro upgrade rate: 22% by month 6 (driven by lot-cap friction)
- Desk customers churn at 2%/mo but are 10× LTV — these are the "must-keep" cohort.

---

# 6. Go-to-Market

## 6.1 The wedge: one channel operator first

The single highest-leverage GTM action is signing **one** mid-tier Arabic XAUUSD signal channel as a partner, on a 20% revenue share, with co-branding inside the channel.

Target archetype: a channel with 30K–80K subscribers, 1–3 years old, run by a single operator, posting 3–8 signals per week, currently monetizing only via broker IB referrals. There are an estimated 40–60 channels matching this profile across MENA.

Pitch to the operator: *"We add a revenue stream that doesn't dilute your IB rebates. Your followers who can't trade manually because of work/timezone become customers. You get 20% of their subscription forever."*

First signed partnership target: **month 3**. Three partnerships: **month 9**. Five partnerships: **month 18**.

## 6.2 Acquisition channels ranked by expected CAC

| Channel | Expected CAC | Why this number | Notes |
|---|---|---|---|
| Channel-operator partnership | $30–$50 | Warm intro + co-branded onboarding; conversion 8–12% of free followers | **Primary** |
| Arabic FX YouTube sponsorships | $80–$120 | 50K–200K-subscriber channels at $15–$25 CPM, conversion 0.3–0.6% | Secondary |
| Arabic FX Twitter/X organic + paid | $110–$150 | Founder-led content + targeted ads to gold-trading lookalikes | Tertiary |
| Broker IB cross-promotion | $60–$90 | Brokers email their MT5 user base; conversion 1–2% | Conditional on broker deal |
| Google paid search | $180–$250 | Generic "MT5 telegram bot" terms are saturated by English competitors | Last resort |

Blended Year 1 CAC: $95 (heavily weighted to channel-operator channel).

## 6.3 First 100 / First 1,000 plan

**First 100 paying users (month 0 → month 4):**
- Month 0–1: Beta with current unpaid pilot operator's followers (manual onboarding, free).
- Month 2: First paid tier launch, $29 only, capped at 50 users for ops bandwidth.
- Month 3: First signed channel partnership; co-announce; uncap.
- Month 4: 100 paying users, $5K MRR.

**First 1,000 paying users (month 4 → month 12):**
- Month 5: Pro tier launches.
- Month 6: Second partnership.
- Month 8: First broker IB deal signed; rebate stream live.
- Month 9: Third partnership; YouTube sponsorship test (3 channels, $15K total).
- Month 12: 1,000 paying users, $58K MRR (subscription) + $12K (IB rebates) = $70K total MRR.

**Month 18 target:** 1,500 paying users, $90K MRR subscription + $21K IB = $111K total MRR ≈ $1.3M ARR run-rate.

---

# 7. Traction & Milestones

## 7.1 Current state (honest)

- ✅ Working end-to-end pipeline on demo MT5 account.
- ✅ 12 weeks of live channel signal logs (380 messages).
- ✅ AI classification F1 of 94% on the 380-message hold-out (action-type accuracy; payload accuracy slightly lower at 89%).
- ✅ One unpaid pilot operator + 4 unpaid pilot followers running the system on demo.
- ❌ No paying customers.
- ❌ No regulated entity yet.
- ❌ No broker partnership yet.

## 7.2 Milestones tied to fundable inflection points

| Month | Milestone | Investor read |
|---|---|---|
| 3 | First signed channel-operator partnership; first 50 paying users | "GTM motion works" |
| 6 | DIFC entity incorporated; Category 4 application filed | "Regulatory path is real" |
| 9 | First broker IB deal closed; IB rebate revenue live | "Second revenue stream proven" |
| 12 | $70K total MRR; 1,000 paying users; 3 channel partnerships | Series A inflection: $850K ARR with two streams |
| 18 | $1.3M ARR run-rate; 5 partnerships; 1,500 users; multi-symbol live | Series A ready ($3–5M) |

---

# 8. Team

## 8.1 Founder

Sole founder, technical. Built the end-to-end system over 9 months: prompt engineering, MQL5 EA, Python pipeline, Telethon integration, SQLite schema, lifecycle sweepers. Background `[to be filled]`. Compensation in Year 1: $80K from raise + equity.

## 8.2 Hires required in first 18 months

| Role | When | Comp band | What they unblock |
|---|---|---|---|
| **Full-stack engineer (Python/FastAPI + frontend)** | Month 2 | $90–$120K + equity | Customer-facing dashboard, account management, billing, multi-account routing for Desk tier |
| **BD / partnerships lead (Arabic-native)** | Month 3 | $70–$90K + 8% commission on signed partnerships | Channel-operator deals, broker IB deals, Saudi/UAE relationships |
| **MQL5 / quant engineer (part-time, then full)** | Month 6 | $100–$130K | Multi-symbol expansion, advanced risk modules, backtesting harness |
| **Compliance / regulatory advisor** (fractional) | Month 4 | $4K/mo retainer | DIFC application, ongoing T&C reviews, broker contract review |
| **Customer success (Arabic-native)** | Month 9 | $40–$55K | Onboarding, support, retention; converts trial → paid |

Total headcount month 18: founder + 4 FTE + 1 fractional.

---

# 9. Risks & Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| **AI misclassification → wrong trade → customer loss** | Critical | Hard per-account lot cap; mandatory demo-account onboarding for first 2 weeks; AI-action audit log per customer; published accuracy metrics; insurance reserve fund equal to 2 months gross profit |
| **Broker T&C violations (algorithmic trading bans)** | High | Sign formal IB agreements with brokers that explicitly permit EA-based execution (most MENA brokers do); ban CopyTrades use on brokers that prohibit it |
| **Channel-operator IP claim / DMCA on signal redistribution** | High | We never redistribute signals — each customer connects their own Telegram account to their own channel subscription. The product is execution infrastructure, not a republishing platform. Documented in T&C and operator partnership agreements. |
| **Regulatory reclassification of copy-trading as portfolio management** | High | DIFC domicile; structure as user-configurable automation, not discretionary management; user retains override at all times via `/halt`; explicit T&C that the user is the trader of record |
| **Key-person risk on the signal source** (channel operator goes dark) | Medium | Five partnerships by month 18 diversifies; users can configure multiple channels at Pro+ tiers; we never depend on a single operator's continuity |
| **LLM provider cost spike or availability** | Medium | Dual-provider abstraction already in code (`AnthropicProvider` and `OpenAIProvider`); prompt caching reduces input cost 90%; triage layer keeps per-message cost <$0.005 average |
| **Currency / payment friction in MENA** | Medium | Stripe + local payment processors (Tap Payments, PayTabs, Hyperpay) at launch; crypto-stablecoin payment option for unbanked corridors |
| **Competitor copies the prompt** | Low–Medium | The prompt is not the moat; the 15 worked examples + state machine + EA-side risk logic are. Each requires live channel exposure (months) to recreate. Trademarks and copyright on the prompt itself filed in DIFC. |

---

# 10. Financial Projections

## 10.1 Year 1 — monthly (USD, thousands)

| Month | Paying users | MRR sub | MRR IB | Total MRR | Total revenue | OpEx | Cash burn |
|---|---|---|---|---|---|---|---|
| M1 | 0 | $0 | $0 | $0 | $0 | $18 | ($18) |
| M2 | 15 | $0.4 | $0 | $0.4 | $0.4 | $25 | ($25) |
| M3 | 60 | $1.7 | $0.2 | $1.9 | $1.9 | $28 | ($26) |
| M4 | 110 | $3.5 | $0.6 | $4.1 | $4.1 | $32 | ($28) |
| M5 | 190 | $7.1 | $1.4 | $8.5 | $8.5 | $36 | ($28) |
| M6 | 290 | $13.0 | $2.6 | $15.6 | $15.6 | $42 | ($26) |
| M7 | 410 | $19.4 | $4.2 | $23.6 | $23.6 | $46 | ($22) |
| M8 | 540 | $27.0 | $5.9 | $32.9 | $32.9 | $50 | ($17) |
| M9 | 680 | $35.0 | $7.7 | $42.7 | $42.7 | $54 | ($11) |
| M10 | 810 | $42.5 | $9.4 | $51.9 | $51.9 | $56 | ($4) |
| M11 | 920 | $49.4 | $10.9 | $60.3 | $60.3 | $58 | $2 |
| M12 | 1,000 | $54.6 | $12.0 | $66.6 | $66.6 | $60 | $7 |
| **Y1 total** | | **$253** | **$54** | | **$307** | **$505** | **($198)** |

Assumptions:
- Subscription MRR = users × $61 blended ARPU × (1 − annual prepay discount factor 0.92)
- IB rebate MRR = users × 0.35 active rate × 3.5 lots/mo × $4/lot
- OpEx ramp: founder + engineer (M2) + BD (M3) + part-time MQL5 (M6) + CS (M9), plus tools/infra/regulatory ($8K/mo blended)

## 10.2 Year 2–3 — quarterly summary (USD, thousands)

| Quarter | Paying users (end) | Quarterly revenue | Quarterly OpEx | Quarterly burn / (profit) |
|---|---|---|---|---|
| Y2 Q1 | 1,200 | $245 | $200 | $45 |
| Y2 Q2 | 1,450 | $310 | $230 | $80 |
| Y2 Q3 | 1,700 | $385 | $260 | $125 |
| Y2 Q4 | 2,000 | $475 | $290 | $185 |
| **Y2 total** | **2,000** | **$1,415** | **$980** | **$435** |
| Y3 Q1 | 2,400 | $580 | $340 | $240 |
| Y3 Q2 | 2,900 | $720 | $390 | $330 |
| Y3 Q3 | 3,500 | $890 | $440 | $450 |
| Y3 Q4 | 4,200 | $1,100 | $490 | $610 |
| **Y3 total** | **4,200** | **$3,290** | **$1,660** | **$1,630** |

Year 3 exit run-rate: $4.4M ARR, $2.4M net profit, ~55% net margin (high because of IB stream and improved gross margin at scale).

## 10.3 Headcount plan

| Period | Headcount | Notes |
|---|---|---|
| Month 0 | 1 (founder) | |
| Month 6 | 3 FTE + 1 fractional | + engineer, BD, compliance |
| Month 12 | 4 FTE + 1 fractional | + MQL5/quant |
| Month 18 | 5 FTE + 1 fractional | + customer success |
| Month 24 | 8 FTE | + 2 engineers, + sales |
| Month 36 | 14 FTE | scale-out |

---

# 11. The Ask

**Round:** $500K SAFE.
**Cap:** $5M post-money.
**Discount:** 20%.
**Why SAFE not priced:** speed-to-close at pre-seed, defer valuation negotiation to seed proper at month 12.

**Comp valuation range.** Pre-seed AI-vertical SaaS with working product and pilot traction: $4–$7M post-money cap is the 2024–2025 norm (Carta SAFE data Q1 2025). $5M is mid-range — defensible without being aggressive.

**18-month use of funds:**

| Bucket | Amount | % | What it buys |
|---|---|---|---|
| Engineering (2 hires + part-time MQL5) | $230K | 46% | Dashboard, billing, multi-account, multi-symbol |
| BD / partnerships (1 hire + commission) | $100K | 20% | 5 channel partnerships, 2 broker deals |
| Regulatory (DIFC entity + fractional compliance + legal) | $60K | 12% | Category 4 advising licence |
| Marketing (YouTube sponsorships, paid pilots) | $50K | 10% | Channel 2 acquisition lever |
| Infrastructure (LLM costs, hosting, monitoring) | $30K | 6% | 18 months runway |
| Founder salary | $80K | 16% | Below-market; signals commitment |
| Buffer | ($50K) | -10% | Reallocated from above contingencies as needed |
| **Total** | **$500K** | **100%** | |

**The milestone the round buys:** $1.3M ARR run-rate at month 18 with two revenue streams (subscription + IB), 5 channel-operator partnerships, DIFC entity operational, 1,500 paying users. This is a fundable Series A profile ($3–$5M at $15–$25M post).

---

# 12. Appendix

## 12.1 Glossary

- **XAUUSD** — Spot gold quoted in US dollars. The single most-traded retail FX/CFD instrument in MENA.
- **MT5 (MetaTrader 5)** — The dominant retail trading platform globally and in MENA; offers EAs and a WebRequest API.
- **EA (Expert Advisor)** — A program written in MQL5 that runs inside MT5 and can place/manage trades.
- **Pip** — Smallest standard price movement. On gold, 1 pip ≈ $0.10 per 0.01 lot.
- **Lot** — Contract size unit. 1.00 lot of XAUUSD = 100 oz gold; 0.01 lot = 1 oz.
- **SL / TP** — Stop-loss / take-profit price levels.
- **Partial close** — Closing a fraction of an open position while leaving the rest running.
- **Copy trading** — Mirroring trades from a signal provider into your own brokerage account.
- **IB (Introducing Broker)** — A revenue-share relationship where a partner is paid per lot traded by referred customers.
- **DIFC** — Dubai International Financial Centre, an independent financial free zone with its own regulator (DFSA).

## 12.2 Architecture diagram (described)

```
Telegram channel ──► Telethon listener (Python)
                          │
                          ▼
              Orchestrator: dedupe + signal memory
                          │
                          ▼
        AI triage (Haiku / nano) ──► keep | ignore
                          │
                          ▼ (kept)
        AI interpreter (Sonnet / gpt-5) + extended thinking
                          │
                          ▼
              Validated Action(s) → SQLite (status: pending)
                          │
                          ▼ (after delay)
              Bot promoter ──► status: sent
                          │
                          ▼
        MT5 EA polls GET /actions?status=sent
                          │
                          ▼
              EA claims, executes, manages stages, trails SL
                          │
                          ▼
              POST result ──► status: executed/failed/rejected/watching
                          │
                          ▼
              EA heartbeats market price every 15s ──► feeds next AI call
```

## 12.3 Sample AI input/output

**Input message (Arabic, real):**
> أمن دخولك يا شباب

**SYSTEM STATE block (rendered live):**
```
OPEN POSITIONS (XAUUSD):
- ticket=4729103, side=BUY, entry=2845.20, vol=0.08/0.08 orig,
  sl=2841.00, tps=[2851.00, 2858.00], partials_taken=0,
  at_BE=false, moved=false, age=14m

MARKET (XAUUSD): bid=2847.30 ask=2847.55 mid=2847.42 age=8s
```

**AI output (structured):**
```json
[
  {
    "action_type": "MOVE_SL_BE",
    "symbol": "XAUUSD",
    "comment": "Operator instruction: secure entry"
  }
]
```

A regex bridge cannot produce this. A stateless LLM cannot reject the reminder version of this message that arrives 4 minutes later. CopyTrades does both.

## 12.4 Unit-economics sensitivity table

LTV under varied churn and ARPU (gross margin held at 78%):

| Monthly churn ↓ / ARPU → | $45 | $61 | $85 |
|---|---|---|---|
| **4%** | $878 | $1,190 | $1,658 |
| **6%** | $585 | $792 | $1,105 |
| **8%** | $439 | $594 | $829 |
| **10%** | $351 | $476 | $663 |

Even at the pessimistic corner (8% churn, $45 ARPU), LTV / CAC stays above 4.6× — the unit economics remain healthy across the plausible parameter space.

---

*End of plan.*
