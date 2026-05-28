# 09 — Compliance and Legal

> **Disclaimer**: this is an engineering plan, not legal advice. Every irreversible item below MUST be reviewed by a fintech attorney admitted in the launch jurisdiction before any subscriber pays.

---

## Launch jurisdiction — ADGM (UAE)

Per §10 of `01-ARCHITECTURAL-DECISIONS.md`. Rationale:

- Channel is Arabic; target buyer is MENA-resident retail.
- ADGM has explicit "FinTech RegLab" pathways for innovative fintech that don't require a full broker-dealer license upfront.
- The path-A execution model ("we publish signals; the customer's machine executes the order on their own broker account") sits closest to "investment information / research publisher" rather than "discretionary investment manager" — that framing is easier in ADGM than in CySEC/FCA.
- Excellent banking access for MENA-targeted business.
- English-language law (DIFC/ADGM common law) reduces drafting overhead.

**Runners-up**: BVI (faster + cheaper but banking has been getting harder); Cyprus (CIF / MiFID II if EU passporting becomes essential).

**Capital structure**: parent BVI holding, ADGM operating subsidiary that holds the license. Tax: 9% UAE corporate rate above ~$100k revenue (verify); 0% on dividends to BVI parent.

**Timeline**: 3–6 months for ADGM registration + license; start the process during Phase 0.

---

## Geo-blocking from day one

`subscribers.jurisdiction` captured at signup (Cloudflare-derived IP-country + self-attestation form + KYC corroboration when triggered).

| List | Behaviour at signup |
|---|---|
| **Red** (full block) | Signup blocked; landing page shows "Not available in your region" with no further detail. |
| **Amber** (KYC at signup + counsel-approved jurisdictions only) | Signup allowed but KYC required before any feature access. Some product features (e.g., higher account size, Elite tier) gated. |
| **Green** (clear) | Standard onboarding; KYC at threshold ($25k account size or Elite tier or AML watchlist hit). |

| Region | Status | Reason |
|---|---|---|
| US, US territories | **Red** | Reg-S, FINRA, state-by-state IA registration; copy-trading framed as IA in many states |
| Canada | **Red** until counsel | Provincial securities commissions, IIROC posture |
| UK | **Red** until passporting via Cyprus | FCA permission requirements for "providing financial advice" |
| EEA + Switzerland | **Red** until MiFID II / FIDLEG path | "Investment advice" framing risk |
| Australia, NZ | **Red** until counsel | ASIC posture |
| China, Iran, NK, Syria, Cuba, OFAC SDN list | **Red** | Sanctions / legality |
| India | **Amber** | RBI overseas-trading guidance review |
| Russia | **Red** | Sanctions, banking |
| MENA (GCC, Egypt, Jordan, Morocco, Tunisia) | **Green** | core target |
| LatAm (BR, MX, AR, CO, CL, PE) | **Amber** | per-country counsel review |
| SE Asia (SG: amber, MY/TH/PH/VN/ID: amber) | **Amber** | per-country review |
| Sub-Saharan Africa (NG, KE, ZA, GH, EG already MENA) | **Amber** | per-country |
| Israel, Turkey | **Amber** | Regulatory specifics |

Implementation:
- Cloudflare WAF rule blocks `/signup`, `/checkout`, `/onboarding` from Red ISO codes.
- App-layer middleware double-checks `subscriber.jurisdiction` on every billing action; mismatch → flag in admin queue + block.
- VPN-detection (Persona's signals during KYC + IP-intelligence vendor like IPinfo) raises a manual-review flag.

---

## Policy documents required

Drafted with counsel; each must cover the minimum points listed. All policies are versioned in Postgres (`system_config.policy_versions`); subscribers accept a specific version at signup, accepted version stored in `subscribers.tos_version_accepted`.

| Document | Must cover |
|---|---|
| **Terms of Service** | Service description (signal publisher; customer's machine executes on customer's broker); no fiduciary relationship; not investment advice; no profit guarantee; eligibility (geo, age 18+); account creation/termination; acceptable use; IP ownership of signals + prompt; arbitration clause (ADGM Arbitration Centre); limitation of liability (capped at fees paid in last 12 months); indemnification; force majeure; right to refuse service; modification of terms with notice |
| **Privacy Policy** | Data collected (account, payment, KYC ref, IP, trading metadata); purposes (delivery, fraud, compliance); processors (Clerk, Paddle, Persona, Resend, AWS, Anthropic, OpenAI); retention (7y operational, 30d post-delete PII); subject rights (access, erasure, portability, rectification); contact for DPO; international transfers (standard contractual clauses where applicable); cookie usage |
| **Risk Disclosure** | Trading carries risk of total loss; copy-trading does not guarantee outcomes; past performance is not a guide; leverage amplifies losses; subscriber decides lot sizing and risk; broker outages and slippage are real; signal delays/missed signals possible; subscriber must understand each trade type before enabling auto-execute. **Must be displayed and explicitly accepted before trial starts.** Re-acknowledge on major policy version change. |
| **Refund Policy** | 7-day satisfaction refund within first billing cycle; no refund of partial-month after cancellation; no refund for trading losses; chargeback policy |
| **AML/KYC Policy** | Conditions triggering KYC; documents accepted; sanctions screening process; suspicious-activity reporting per ADGM FSRA; record-keeping 7 years |
| **Cookies Policy** | Essential, analytics (Posthog if used), opt-out mechanism; GDPR-style consent banner for EEA visitors even if we don't sell there (we display content) |

---

## GDPR — Right-to-Access and Right-to-Erasure

Even though we geo-block EEA at v1, EEA visitors may land on the marketing site and create accounts that we then block. GDPR applies because we process data of EU residents during the geo-block evaluation; we minimize collection and respect rights from day one.

### Right-to-Access

Endpoint: `subscribers.me.requestExport`. Async pg-boss job `generate_subject_export`:
1. Gather:
   - `users` (excluding `clerk_user_id`; we instead include the Clerk-exported JSON via Clerk API)
   - `subscribers`, `subscriptions`, `invoices`, `payment_methods` (with PM tokenized references only)
   - `broker_connections` (without raw tokens — those don't exist on our side anyway)
   - `risk_profiles`
   - `subscriber_channel_subscriptions`
   - `subscriber_actions`, `positions`, `notifications`, `usage_events`
   - `sessions`, `audit.api_errors` (subscriber-scoped), `audit.impersonation_log` (where subscriber was impersonated)
2. Bundle as JSON + CSV, zipped, encrypted with subscriber's email-verified password (PBKDF2-derived key) OR per a one-time signed S3 URL valid 7 days.
3. Email subscriber the link.

### Right-to-Erasure

`subscribers.me.requestDelete` → confirmation email → on confirm:

| Table | Action |
|---|---|
| `users` | `email` → `deleted_<id>@redacted.local`; `full_name`, `country_code` → null; `deleted_at=now()` |
| Clerk user | Delete via Clerk API at +30d |
| `subscribers` | `deleted_at=now()`; preserve `id` for FK integrity |
| `subscriptions` | `cancel_at_period_end=true` |
| `broker_connections` | `status='revoked'`; encrypted credentials zeroed |
| `payment_methods` | delete (Paddle holds the actual token) |
| `messages`, `signal_actions` | retained (provider-scope; no subscriber PII) |
| `subscriber_actions`, `positions`, `notifications`, `usage_events`, `audit.*` | retained 7 years per regulatory record-keeping; subscriber join intact, no PII inside payloads (we deliberately don't put subscriber names in payloads) |
| `signal_memory` | retained (provider-scope) |
| Persona KYC | delete via Persona API; keep our `kyc_status='approved'` + `kyc_provider_ref` |

A 30-day cooling-off window before PII scrub to allow chargeback handling and law-enforcement requests to land.

### Right-to-Rectification

Standard via account settings UI.

### Right-to-Portability

The Access export is the portability artifact. Format is documented JSON.

---

## Record retention

| Concern | Retention | Reason |
|---|---|---|
| Trade signals received + emitted + executed | 7 years | Regulatory record-keeping standard for financial signal services |
| KYC documents | NOT held by us — Persona | Persona's policy + 7 years per local law |
| Customer communications (emails, support tickets) | 3 years | Customer dispute resolution |
| Auth audit (logins, sessions, password changes) | 2 years | Security forensics |
| Admin audit (impersonation, settings changes, refunds) | 7 years | Internal governance + regulator request |
| Billing records (invoices, payment failures, refunds) | 7 years | Tax + audit |
| Marketing consent | duration of relationship + 3 years | Demonstrate consent in event of dispute |
| Logs (application, error, security) | 90 days hot, 13 months cold S3 | Operational + security |

---

## Required disclaimers in user-facing copy

- **Every trade notification** (email, Telegram, in-app, webhook): footer "*Past performance is not a guide to future results. Trading carries the risk of total loss of capital.*"
- **Dashboard P&L numbers**: "Realized P&L. Not investment advice."
- **Onboarding** and **risk-disclosure page** — explicit checkbox: "I understand that CopyTrades publishes signals only and does not provide investment advice; I am responsible for trades placed on my broker account."
- **Marketing copy**: no testimonial claims unless from real customers with documented consent; no profit projections; no "guaranteed returns" language anywhere.

---

## Vendor due-diligence checklist

For each third-party processor, on file before launch:

| Vendor | Need | Stored |
|---|---|---|
| AWS | DPA signed; SOC 2 Type 2; ISO 27001 | legal vault |
| Paddle | DPA; merchant-of-record agreement; PCI-DSS attestation | legal vault |
| Clerk | DPA; SOC 2 Type 2 | legal vault |
| Persona | DPA; SOC 2 Type 2; financial-services KYC coverage | legal vault |
| Resend | DPA; SOC 2 | legal vault |
| Anthropic | DPA; data-retention policy (no training on our inputs); usage geography | legal vault |
| OpenAI | DPA; data-retention policy; usage geography | legal vault |
| Telegram (Bot API + MTProto) | No formal DPA — limit data sent; document the limitation | risk register |
| Cloudflare | DPA; SOC 2 | legal vault |
| Sentry | DPA; PII-scrubbing config documented | legal vault |
| Datadog | DPA; data-retention; log-PII-scrubbing config | legal vault |

Annual vendor review: re-pull current SOC 2 reports, re-confirm sub-processor lists, re-evaluate retention.

---

## Specific risks to surface to counsel before launch

1. **MQL5 EA distribution**: do any of our target broker EULAs prohibit "third-party EA that monetizes signals to customer's account"? Some prop-firms do.
2. **Telegram channel rights**: Is the Forex Engineer channel owned by us, or are we a redistributor? IP / licensing risk — see §15-OPEN-QUESTIONS.md Q31.
3. **"Copy trading" naming**: many jurisdictions reserve "copy trading" for licensed brokers' social-trading features. Consider re-naming to "Signal automation" if counsel raises concerns.
4. **Profit-share narrative in marketing**: even if our pricing is flat-fee, any marketing claim like "users averaged +X%" likely triggers IA framing in multiple regs.
5. **The interpreter prompt could be argued to constitute "advice"**: this is the single most-likely-to-bite framing. Counsel must agree we ship as a publication-of-signals model, not advice.
6. **Channel halt / kill switch obligations**: if we have power to halt signals mid-trade, are we acting as agent? Counsel must agree the halt is a service-availability action, not discretion.
