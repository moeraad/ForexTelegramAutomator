# 08 — Communications

Outbound channels: **email, Telegram DM, webhook, in-app inbox**. All flow through one queue (`notifications` table → pg-boss dispatcher workers per channel).

---

## Email — Resend

Recommendation: **Resend** (`resend.com`). Runner-up: **Postmark**.

| Vendor | Pros | Cons |
|---|---|---|
| Resend | Best DX, React Email templating, generous free tier (3k/mo), modern API | Newer; ecosystem still maturing |
| Postmark | Best inboxing reputation, mature transactional flow | More expensive at scale; older DX |
| SES | Cheapest at scale | Send-quota ramp painful; we have to handle bounces, complaints, DKIM ourselves |

Resend at our scale (100 subs × ~30 emails/mo = 3k → 50 subs × 200 = 10k mo at peak): ~$20/mo on the Pro plan (verify).

### Templates (React Email)

| Template | Trigger |
|---|---|
| `welcome.tsx` | Email verified |
| `onboarding_step_reminder.tsx` | Onboarding dropped at step 2/3 for 48h |
| `trial_starting.tsx` | Subscription `trialing` created |
| `trial_ending_3d.tsx` | 3 days before trial end |
| `trial_ended.tsx` | Trial converted or cancelled |
| `payment_succeeded.tsx` | invoice paid |
| `payment_failed.tsx` | invoice payment_failed |
| `subscription_cancelled.tsx` | cancel_at_period_end set |
| `subscription_resumed.tsx` | resume |
| `plan_upgraded.tsx` | plan change |
| `broker_connection_added.tsx` | broker created — includes the raw bearer token download instructions (NOT the token itself) |
| `broker_offline.tsx` | EA silent 5 min |
| `broker_disabled.tsx` | EA silent 30 min, connection disabled |
| `action_executed.tsx` | subscriber_actions terminal — per their prefs |
| `action_rejected.tsx` | rejected/failed result |
| `position_closed.tsx` | position closed with P&L |
| `risk_profile_changed.tsx` | self-edit confirmation (security) |
| `password_changed.tsx` | self-edit confirmation (security) |
| `new_login.tsx` | new device/IP login (security) |
| `kyc_required.tsx` | KYC trigger fired |
| `kyc_approved.tsx` / `kyc_rejected.tsx` | KYC webhook result |
| `data_export_ready.tsx` | GDPR export job done |
| `account_deletion_scheduled.tsx` | request delete confirmed |
| `provider_halt_notice.tsx` | signal provider halted |
| `system_incident.tsx` | incident bulletin (admin opt-in) |
| `refund_issued.tsx` | refund processed |

### SPF / DKIM / DMARC checklist

- Sending domain: `mail.copytrades.example.com` (separate from primary domain to isolate reputation).
- SPF: `v=spf1 include:_spf.resend.com -all`
- DKIM: Resend provides 2 CNAMEs.
- DMARC: `v=DMARC1; p=quarantine; rua=mailto:dmarc@copytrades.example.com; ruf=mailto:dmarc-forensic@copytrades.example.com; aspf=s; adkim=s; pct=100`
- BIMI optional post-launch.
- Pre-launch: run `mail-tester.com`, target ≥9/10. Run a 2-week warm-up: 50 → 200 → 500 mails/day before bulk volume.
- Bounce handling: Resend webhooks → `notifications.failure_reason='hard_bounce'` → subscriber email flagged → admin queue.
- Complaint handling: `mailbox_disabled` events → mark `users.email_verified_at=null` → require re-verify; auto-unsubscribe from marketing.

---

## Telegram — opt-in via owner-controlled bot

Subscribers opt-in by clicking a deep link `t.me/CopyTradesAlertsBot?start=<one-time-code>`. The bot DMs `Hi! Reply YES to enable trade alerts`. On confirmation, we store the Telegram `chat_id` in `subscribers.telegram_chat_id`.

### Bot fleet

- **One bot per ~500 active Telegram-opted-in subscribers.** Telegram limits: ~30 msg/sec total across distinct chats per bot. With 500 subs, a single hot signal blasts 500 messages → ~17 seconds drain — acceptable. At 2000 subs we run 4 bots and shard by `subscriber_id % bot_count`.
- Bot tokens stored in `system_config` (envelope-encrypted).
- The owner-controlled bot is `@CopyTradesAlertsBot`. Additional shards: `@CopyTradesAlertsBot2`, `@CopyTradesAlertsBot3`, …
- The dispatcher worker `dispatch_telegram_notifications` drains the `notifications` table where `channel='telegram'` and `sent_at IS NULL`, with a per-bot 25-msg/sec governor (leaving headroom).
- On HTTP 429 from Telegram, worker honors `retry_after`.

### Telegram message content

- Trade execution: terse, single message, ASCII-only fallback for clients that don't render emoji. Example:
  ```
  ✅ XAUUSD BUY 4694 (0.07 lot)
  SL 4686 · TP 4705 · ticket 8802700000
  Risk: $56 (0.6% of balance)
  ```
- Position close:
  ```
  XAUUSD BUY closed
  +$112 (1.2R · entry 4694 → 4710 trail)
  ```
- Risk disclaimer footer on signal messages (required per §9-COMPLIANCE-AND-LEGAL.md):
  ```
  Past performance is not a guide. Trading carries risk of total loss.
  ```

Subscribers can opt-out at any time with `/stop` in the bot DM.

---

## In-app notification inbox

`notifications` table powers the bell icon + `/notifications` page in the web app. SSE pushes new-row events; React Query invalidates the inbox query.

- All other channels' notifications are mirrored to `inapp`. Subscriber sees a single source of truth.
- Markup: rendered server-side (no XSS risk from event payloads).
- "Mark all read" + per-row "Mark read" + auto-archive after 30d viewed.

---

## Webhook (Elite tier)

Elite subscribers can register up to 3 webhook URLs:

```
POST {their URL}
Headers:
  X-CopyTrades-Signature: sha256=<hmac(subscriber_secret, body)>
  X-CopyTrades-Event: action.executed | position.closed | broker.offline | ...
  X-CopyTrades-Delivery-Id: <uuid>
Body: same payload as `notifications.payload_json`
```

- Retry: exponential backoff 6 attempts over ~24h.
- Subscriber UI surfaces last 50 delivery attempts with status codes.
- Webhook secrets rotatable via UI.

---

## Throughput math

Worst-case fan-out: one hot signal (OPEN) on a busy day → emits to N subscribers × M notification channels each.

Assumptions per subscriber: 1 email + 1 Telegram (Pro+) + 1 in-app + 0 or 1 webhook = ~3 messages.

| Subscribers | Telegram-opted-in | Hot-signal blast | Telegram drain time (1 bot, 25 msg/s) | Email blast (3k/sec Resend Pro, verify) | Bot fleet needed |
|---|---|---|---|---|---|
| 50 | 30 | ~150 msgs | 1.2s | <1s | 1 |
| 100 | 70 | ~300 msgs | 2.8s | <1s | 1 |
| 500 | 350 | ~1,400 msgs | 14s | <1s | 1 (acceptable) |
| 2000 | 1,400 | ~5,600 msgs | 56s (too slow) | <2s | 4 (~14s each) |

At 2000 subscribers we shard Telegram across 4 bots. Email scales linearly with Resend's quota.

Daily totals at 2000 subs assuming ~10 signals/day:
- Telegram: 2000 × 0.7 × 10 = 14,000 msgs/day. Within Telegram's per-bot daily budgets across 4 shards.
- Email: 2000 × 10 = 20,000 transactional + ~1× lifecycle = ~25,000/day. Resend Pro covers 50k/mo; need Scale tier (~$80/mo) at this volume.
- Webhooks: 2000 × 0.1 (Elite share) × 10 = ~2,000/day. Easy.

---

## Rate limits & abuse controls

- Per-subscriber email-per-day cap: 50 (alert above + auto-throttle).
- Per-subscriber Telegram-per-day cap: 200.
- Anti-spam: notification dispatcher coalesces "same event_type + same target + within 30s" into one delivery with a count badge.
- Marketing emails (newsletter, upsell) go through Resend with `unsubscribe` header and a one-click endpoint. We do not send marketing without explicit opt-in (`subscribers.marketing_opt_in=true`).

---

## Internal alerts (NOT to subscribers)

Engineering alerting goes to Slack + PagerDuty, not via this notifications system. See `10-INFRASTRUCTURE.md` § Observability.
