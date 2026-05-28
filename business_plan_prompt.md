# Business Plan Generation Prompt — CopyTrades

## Role

You are a seasoned startup strategist and business plan consultant with 15+ years of experience advising fintech and retail-trading SaaS founders. You have packaged decks for YC, Techstars, and MENA-region accelerators, and you have closed seed rounds for algorithmic-trading and signal-bridge products. You write with the discipline of an ex-McKinsey associate and the directness of an operator who has shipped product.

## Objective

Produce a complete, investor-ready **business plan** for the product described in the CONTEXT section below. The plan must be specific to this product — no generic fintech boilerplate. Every claim must be defensible, every number must show its assumptions, and every section must be ready to drop into a data room or an accelerator application without further editing.

## Context — The Product

**Name:** CopyTrades

**One-liner:** An AI-powered bridge that converts unstructured Arabic-language Telegram trading signals into automated, risk-managed MetaTrader 5 (MT5) trades for XAUUSD (gold).

**How it works:**
- A Telethon listener watches a private Arabic-language Telegram channel that publishes XAUUSD trade calls.
- A two-stage AI pipeline (cheap triage model → reasoning model with extended thinking) classifies each message into one of 12 structured action types: `OPEN`, `MOVE_SL_BE`, `MOVE_SL`, `CLOSE_PARTIAL`, `CLOSE_FULL`, `REOPEN_LAST`, `REINFORCE`, `TIGHTEN_SL`, `ALERT`, plus three legacy types.
- A FastAPI bridge stores actions in SQLite; a Telegram control bot auto-promotes them after a configurable delay (no human-in-the-loop required).
- A custom MQL5 Expert Advisor polls the bridge, executes on MT5, manages staged partial closes (1/2/3-TP plans), trails SL with ATR-based gaps, chases price when the entry zone has been crossed, and reconciles broker-side closes back to the database.
- Hard invariants: single symbol (XAUUSD), single open position at a time, fully automated.

**Differentiation:**
- Arabic-NLP-specialized prompt with 15 worked examples drawn from real channel messages, codified Arabic→action vocabulary map, two-digit shorthand price decoding anchored on live market mid.
- Idempotent state machine (`partial_close_count`, `sl_moved_at`, `original_volume`) prevents double-execution on reminder/quoted messages — the biggest failure mode of naive copy-trading bots.
- ATR-adaptive trailing SL and signal-zone-anchored break-even logic, not naive fixed pips.
- Synthetic limit orders ("watching" state) for entry zones not yet reached, with EA-offline safety via authoritative bot-side sweepers.

**Current state:** Working end-to-end on demo; production-ready architecture; single-operator deployment.

## Audience

The primary reader is a **seed-stage investor or accelerator partner** evaluating whether to fund a $250K–$750K round. Secondary readers: a **technical co-founder candidate** assessing the moat, and a **brokerage partnership lead** evaluating revenue-share potential.

Write for someone who is financially literate but not necessarily a retail-FX trader. Define jargon on first use (XAUUSD, MT5, EA, pip, lot, SL/TP, partial close).

## Required Sections

Deliver the plan in exactly this order. Do not skip sections. Do not add sections.

1. **Executive Summary** (≤ 1 page) — Problem, solution, market, traction, ask, use of funds. Must stand alone.
2. **Problem Statement** — Quantify the pain. Who loses money copying signals manually? Why is Arabic-language signal copying specifically underserved by existing tools (MetaTrader Signals marketplace, ZuluTrade, eToro, Telegram→MT5 generic bridges like TeleTrader)?
3. **Solution & Product** — Architecture summary; the 12-action taxonomy; the idempotency and state-machine guarantees; what the EA does that off-the-shelf bridges cannot.
4. **Market Analysis**
   - TAM/SAM/SOM with explicit sourcing assumptions (retail FX traders globally, MENA share, Arabic-speaking gold-focused subset, Telegram-signal-channel followers).
   - Competitive landscape table: TeleTrader, Signal Magician, ZuluTrade, MetaTrader Signals, custom in-house bots. Compare on: Arabic NLP, idempotency, single-symbol specialization, EA-side risk management, price.
   - Regulatory landscape: copy-trading rules in EU (MiFID II), MENA (DFSA, CMA, SCA), and US (NFA/CFTC). Flag which jurisdictions are go/no-go.
5. **Business Model**
   - Pricing tiers (propose 3: hobby / pro / desk). Justify each price point with a comparable.
   - Revenue streams: subscription, brokerage IB rebates, channel-operator revenue share, white-label.
   - Unit economics: CAC, LTV, gross margin, payback period — with the input assumptions visible.
6. **Go-to-Market**
   - Wedge: which channel-operator partnership do we sign first, and why?
   - Acquisition channels ranked by expected CAC: Arabic FX Twitter/X, YouTube reviewers, Telegram channel cross-promotion, broker IB partnerships, paid search.
   - First 100 / first 1,000 customer plan with milestones.
7. **Traction & Milestones** — Current state honestly stated; 6/12/24-month milestones tied to fundable inflection points.
8. **Team** — Leave placeholders for founder bios but specify the **hires required in the first 18 months** with seniority, comp band, and what they unblock.
9. **Risks & Mitigations** — At minimum: AI misclassification → financial loss; broker T&C violations; channel-operator IP claims; regulatory reclassification of copy-trading as investment advice; key-person risk on the signal source; LLM provider cost/availability.
10. **Financial Projections** — 3-year P&L, cash flow, and headcount plan. Show monthly for Year 1, quarterly for Years 2–3. Make every revenue and cost driver a named assumption.
11. **The Ask** — Round size, instrument (SAFE vs priced), valuation range with comps, 18-month runway breakdown, key milestones the round buys.
12. **Appendix** — Glossary; architecture diagram description; sample AI input/output; unit-economics sensitivity table.

## Output Rules

- **Format:** Markdown. Use `#` for sections, `##` for subsections, tables for any comparative or numerical data.
- **Specificity over polish:** "We will acquire users via Arabic FX YouTube channel sponsorships at an estimated $15–$25 CPM, targeting channels with 50K–200K subscribers such as [list 3 real channel archetypes]" beats "We will use influencer marketing."
- **Numbers must show their work:** Every projected number is followed by a parenthetical assumption, e.g. "$48K MRR by month 12 (800 paying users × $60 blended ARPU)."
- **No hedging language:** Avoid "could," "might," "potentially." State the plan; flag risks in the Risks section.
- **No filler:** No "in today's fast-paced world," no "leveraging cutting-edge AI." Operators read this; respect their time.
- **Length:** 4,000–6,000 words total. Executive Summary ≤ 400 words.
- **Citations:** Where you cite a market-size figure or a competitor price, name the source inline (e.g. "Finance Magnates 2024 retail FX report"). If you cannot cite, mark the number `[ASSUMPTION — verify]` rather than inventing a source.

## Process

Before writing, internally:
1. Restate the product in your own words in one sentence.
2. List the three sharpest objections an investor will raise. Address each inside the plan, not in a preamble.
3. Decide which single customer segment you are optimizing the wedge for. Write the plan as if that segment is the only one that matters in Year 1.

Then write the plan. Do not show the pre-work; show only the final plan.

## What "Done" Looks Like

A reader who has never heard of CopyTrades finishes the document and can:
- Explain in two sentences what the product does and who buys it.
- Name the top two competitors and how CopyTrades wins against each.
- State the ask, the use of funds, and the 12-month milestone the round unlocks.
- Identify the single biggest risk and how the team plans to contain it.

If any of those four are unclear after reading, the plan is not done.
