# 11 — Observability and Support

The product is a black box to a paying subscriber unless we surface "what happened to my trade and why" with the same fidelity as the admin can see. This document specifies both sides.

---

## Per-subscriber audit trail

Every event that affects a subscriber's experience is queryable in admin and visible (in adapted form) to the subscriber.

**Storage**: derived from already-existing tables; no separate "events" table beyond what's in §3-DATA-MODEL.md. The unified timeline join:

```sql
-- Subscriber timeline (for a single subscriber)
SELECT 'message' kind, m.id, m.received_at as ts, jsonb_build_object('text', m.text) detail
FROM messages m
JOIN subscriber_channel_subscriptions s ON s.signal_provider_id = m.signal_provider_id
WHERE s.subscriber_id = $1
UNION ALL
SELECT 'action', a.id, a.created_at, jsonb_build_object('type', a.action_type, 'status', a.status, 'skipped_reason', a.skipped_reason, 'payload', a.payload_json)
FROM subscriber_actions a
WHERE a.subscriber_id = $1
UNION ALL
SELECT 'position', p.id, COALESCE(p.closed_at, p.opened_at), jsonb_build_object('ticket', p.mt5_ticket, 'pnl', p.realized_pnl_cents, 'status', p.status)
FROM positions p WHERE p.subscriber_id = $1
UNION ALL
SELECT 'notification', n.id, COALESCE(n.sent_at, n.scheduled_for), jsonb_build_object('channel', n.channel, 'event', n.event_type)
FROM notifications n WHERE n.subscriber_id = $1
UNION ALL
SELECT 'billing', i.id, i.created_at, jsonb_build_object('amount', i.amount_cents, 'status', i.status)
FROM invoices i JOIN subscriptions sub ON sub.id = i.subscription_id WHERE sub.subscriber_id = $1
ORDER BY ts DESC
LIMIT 200;
```

Powers:
- Admin: `/admin/subscribers/[id]` "Timeline" tab
- Subscriber: `/trades/[id]` and a more concise version on `/dashboard`

---

## Admin impersonation — safely

Pattern: two-mode impersonation. Read-only is the default; write requires explicit re-authentication.

```mermaid
sequenceDiagram
  participant A as Admin
  participant WEB as web
  participant PG as Postgres
  participant AUD as audit.impersonation_log

  A->>WEB: Click "Impersonate" with reason text required
  WEB->>AUD: INSERT (admin_id, subscriber_id, mode='read_only', started_at, reason)
  WEB->>A: Set cookie `impersonation_token` (JWT, exp=30m)
  WEB->>A: Top banner: "Impersonating Bob (read-only) · End"
  A->>WEB: Browse /admin/subscribers/[id]/dashboard (reads only)
  WEB->>PG: SET LOCAL app.subscriber_id = X; app.role = 'admin_impersonating_ro'
  WEB->>AUD: Append read_paths += '/dashboard'

  alt Admin needs to write
    A->>WEB: Click "Escalate to write mode" — requires MFA challenge
    WEB->>A: MFA prompt (Clerk re-auth)
    A->>WEB: MFA passes
    WEB->>AUD: UPDATE mode='full'; append writes_json
    A->>WEB: Now can issue mutations (e.g., pause subscriber)
  end

  A->>WEB: Click "End impersonation"
  WEB->>AUD: UPDATE ended_at=now()
```

Server enforcement:
- `ctx.role==='admin_impersonating_ro'` → tRPC middleware blocks any mutation router.
- `ctx.role==='admin_impersonating_full'` → mutations allowed but logged with `actor_admin_user_id` AND the impersonated `subscriber_id`.
- Impersonation tokens expire at 30 min idle (auto-extend on activity) or 2h hard cap.

---

## Subscriber-facing transparency

The most common subscriber question: **"Why didn't I get that signal?"**

Surface: `/trades/[id]` for executed/failed/rejected actions; `/trades?status=skipped` for the skip list; each row's `skipped_reason` is rendered with a plain-English explanation pulled from a per-reason catalogue:

| `skipped_reason` | Plain English |
|---|---|
| `subscription_inactive` | "Your subscription wasn't active when this signal fired." |
| `subscriber_paused` | "You had paused signals at this time." |
| `broker_offline` | "Your broker connection wasn't reachable. The signal expired before your EA came back online." |
| `instrument_not_allowed` | "This signal was for an instrument you've disabled (XAUUSD)." |
| `action_type_not_allowed` | "This was a {type} action, which you've disabled in your risk settings." |
| `below_min_balance` | "Your broker account balance was below your minimum-balance setting ($X)." |
| `max_daily_loss_hit` | "You had reached your daily loss cap. Auto-pause engaged." |
| `consecutive_losses` | "You had {N} consecutive losses, hitting your auto-pause threshold." |
| `outside_time_filter` | "This signal was outside your trading hours." |
| `position_already_open` | "You already had an open XAUUSD position; this signal would have violated single-position mode." |
| `lot_size_zero` | "After applying your risk rules, the computed lot size was below your broker's minimum. No order was placed." |
| `account_size_exceeds_plan` | "Your account size exceeds the {plan} tier limit. Upgrade to allow this signal." |

Each row also shows the raw signal text and the resolved lot/SL/TP that would have been used — so the subscriber sees what they missed.

---

## Internal customer-support tooling

Everything CS needs in one place at `/admin/subscribers/[id]`:

| Action | What it does | Permission |
|---|---|---|
| Force-pause | `subscribers.paused_at=now()` + reason | support+ |
| Force-resume | Clear paused state | support+ |
| Force-close position (with confirmation) | Issue server-side `CLOSE_FULL` action → EA executes → audit | admin |
| Cancel pending action | `subscriber_actions.status='cancelled'` (only `pending`/`sent`/`watching`); EA polls and skips | support+ |
| Disable broker connection | `broker_connections.status='disabled'` | support+ |
| Rotate EA token (on subscriber's behalf) | New token, email to subscriber, old invalidated | admin |
| Refund (full / partial) | Paddle refund API + audit | admin |
| Add manual notification | Push an inapp/email notification to this subscriber (e.g., "We noticed your account had X" — service recovery) | support+ |
| Reset 2FA | Clerk API revoke → email subscriber instructions | admin |
| Resend verification email | Clerk API | support+ |
| Send password reset | Clerk API | support+ |
| Annotate | Free-text notes pinned to the subscriber, visible only to staff | support+ |

All actions logged to `audit.admin_actions` with reason text required.

---

## Status page

`status.copytrades.example.com` powered by Better Stack Status Page.

Components tracked:
- Signal pipeline (signal-svc + listener health)
- Subscriber EA API (ea-api availability)
- Web dashboard
- Billing
- Notifications (email + Telegram bot health)

Auto-updated from synthetic checks. Manual incident posts for human-known issues.

---

## Runbooks for the top 10 expected incidents

Each runbook lives at `docs/runbooks/<slug>.md`, linked from PagerDuty alerts.

### RB-01 — Telegram session revoked

**Signal**: listener log "auth_key_unregistered"; `listener.ea_last_seen_at` stale.
**Impact**: no new signals received; existing positions unaffected.
**Steps**:
1. Page admin at `/admin/providers/[id]`.
2. Click "Re-authenticate Telegram" — flow opens a panel requesting the operator's Telegram code (sent via the user-account number).
3. Operator enters code; new session blob KMS-encrypted into `signal_providers.tg_session_blob_enc`.
4. Restart `listener` ECS service.
5. Confirm `listener.ea_last_seen_at` fresh.
6. Backfill catch-up runs automatically (per orchestrator config).
7. Post to status page.

### RB-02 — MetaApi or broker mass-reject

**Signal**: `detect_mass_failure` worker alert; failure rate >50% in 5 min for one broker_company.
**Impact**: that broker's subscribers can't trade; data integrity OK.
**Steps**:
1. Confirm broker status (broker's status page).
2. Halt fan-out for affected broker via system_config flag `halted_brokers += '<broker_company>'`.
3. Notify affected subscribers (in-app + email): "Your broker is experiencing an outage; we've paused signals for affected accounts."
4. Watch broker recovery; un-halt; replay would NOT auto-fire (we don't auto-replay; see §5).
5. Post-mortem within 24h.

### RB-03 — LLM provider outage (Anthropic)

**Signal**: signal-svc errors `anthropic.APIStatusError`.
**Impact**: signals fall back to ALERT-only; no auto-trade.
**Steps**:
1. Confirm via Anthropic status page.
2. Failover to OpenAI by flipping `provider_settings.ai_provider='openai'` in admin UI.
3. Note that LLM-quality is different — set a feature flag `degraded_interpreter=true` shown to subscribers in a banner: "Signals are running on backup engine; we may be more conservative for a few hours."
4. Monitor `audit.ai_calls`.
5. When Anthropic recovers, flip back and remove banner.

### RB-04 — Postgres failover

**Signal**: RDS event "Multi-AZ failover initiated"; brief connection errors.
**Impact**: 60–120s of intermittent requests; queue retries handle it; lifecycle integrity preserved.
**Steps**:
1. Watch RDS health.
2. Confirm app connection pool recovered (Datadog metric `db.connections.healthy`).
3. Verify pg-boss workers processing.
4. Spot-check `subscriber_actions` table for stuck rows.
5. Post-mortem within 24h.

### RB-05 — Stripe/Paddle webhook backlog

**Signal**: `process_paddle_event` queue length > 100.
**Impact**: subscription state stale; subscribers may see wrong plan.
**Steps**:
1. Check webhook endpoint logs.
2. Scale workers (ECS service `worker` desired count +2).
3. If stuck → manual replay via Paddle dashboard webhook re-send.
4. Reconcile via daily `reconcile_paddle_subscriptions` job (force-trigger).

### RB-06 — KMS key disabled / decrypt failure

**Signal**: `kms.Decrypt` errors in ea-api/web.
**Impact**: subscriber broker creds can't be decrypted; no orders executed for affected backends. EA-bearer-token path (Path A) is unaffected because we only hash, never decrypt, those.
**Steps**:
1. Confirm KMS CMK state in AWS console.
2. Re-enable if accidentally disabled.
3. If keys deleted (catastrophic): restore from KMS key-deletion grace period (verify policy is set to 30d).
4. If unrecoverable: subscribers must re-link broker accounts.

### RB-07 — EA fleet-wide silent (cloud-side issue, not subscriber-side)

**Signal**: simultaneous drop in `ea_last_seen_at` across >50% of `broker_connections.status='active'`.
**Impact**: no orders executing.
**Steps**:
1. Check `ea-api` ECS service.
2. Check ALB target group health.
3. Check TLS certificate expiry (`cert-manager` should auto-renew via ACM but verify).
4. If cert pinning issue (we rotated cert without bumping EA's pinned SHA): emergency unpin via system_config flag `ea_cert_pin_bypass=true` (server still requires TLS; EA accepts any chain-valid cert).
5. Force EA reconnect via subscriber notification.

### RB-08 — Bot polling broken (control plane down)

**Signal**: dispatch_telegram_notifications worker errors `Bot was blocked` / `Unauthorized` rates spike.
**Impact**: Telegram notifications fail; subscribers don't get DMs.
**Steps**:
1. Check bot token validity (BotFather).
2. Rotate token if revoked; update `system_config.telegram_bot_tokens`.
3. Notify affected subscribers via email fallback.

### RB-09 — Mass `signal_actions.fanout_completed_at IS NULL` backlog

**Signal**: pg-boss `fanout_signal_action` queue length > 100.
**Impact**: subscribers don't see new signals.
**Steps**:
1. Check worker logs.
2. Scale `worker` service ECS desired count.
3. Identify long-running jobs (DB query against `pg_stat_activity`).
4. If a single bad row blocks the queue, manually mark `fanout_completed_at=now()` after triage.

### RB-10 — Stranded `claimed` actions

**Signal**: `subscriber_actions WHERE status='claimed' AND claimed_at < now() - interval '10 min'` count > 5.
**Impact**: subscribers' EAs missed terminal POSTs; eventual self-heal but slow.
**Steps**:
1. Confirm `release_stale_claims` worker is running.
2. Manually run release SQL with admin button if worker is broken.
3. Check that subscriber's EA recovered (next claim succeeds).

---

## Daily ops review

Each weekday morning a worker job emits an "ops digest" Slack message:
- Subscribers gained/lost yesterday
- Failed payments queue
- Failed actions count (with top reasons)
- Skipped actions count (with top reasons — identify systemic risk-rule issues)
- Stale broker connections (>24h offline)
- Telegram delivery success rate
- LLM cost yesterday vs. budget
- Open admin tickets older than 24h

This is the artifact CS / ops uses to start the day.
