# 05 — Signal Fan-out

How one LLM-emitted `signal_actions` row becomes N `subscriber_actions` rows.

---

## Data flow

```mermaid
flowchart LR
  TG[Telegram] --> LIS[listener]
  LIS --> SVC[signal-svc orchestrator]
  SVC --> SA[(signal_actions)]
  SA -- pg-boss notify --> FW[fanout_signal_action worker]
  FW --> SC[(subscriber_channel_subscriptions)]
  FW --> BC[(broker_connections)]
  FW --> RP[(risk_profiles)]
  FW --> SUBA[(subscriber_actions pending|skipped)]
  PROM[promoter worker every 1s] --> SUBA
  PROM --> SUBA2[(subscriber_actions sent)]
  SUBA2 --> EAP[EA polling]
```

---

## The fan-out worker (TypeScript, pg-boss)

```ts
// apps/worker/src/jobs/fanoutSignalAction.ts (sketch)
export async function fanoutSignalAction({ signalActionId }: Job) {
  const sa = await db.signalActions.findById(signalActionId);
  if (!sa) return;
  if (sa.fanoutCompletedAt) return; // idempotent

  // Provider-level halt
  const provider = await db.signalProviders.findById(sa.signalProviderId);
  if (provider.halted) {
    await markFanoutComplete(sa.id, 0);
    return;
  }

  // Find eligible subscribers
  const subs = await db.query(`
    SELECT s.id AS subscriber_id, scs.id AS scs_id, bc.id AS broker_connection_id,
           rp.* AS risk_profile_*, bc.account_balance_cents, bc.status AS bc_status,
           s.paused_at, sub.status AS sub_status, sub.current_period_end
    FROM subscriber_channel_subscriptions scs
    JOIN subscribers s ON s.id = scs.subscriber_id
    JOIN subscriptions sub ON sub.subscriber_id = s.id AND sub.status IN ('trialing','active')
    JOIN broker_connections bc ON bc.subscriber_id = s.id AND bc.status='active'
    JOIN risk_profiles rp ON rp.broker_connection_id = bc.id
    WHERE scs.signal_provider_id = $1 AND scs.is_active=true
  `, [sa.signalProviderId]);

  const inserts: SubscriberActionRow[] = [];
  for (const row of subs) {
    const decision = applyRiskGates(sa, row);
    inserts.push({
      subscriber_id: row.subscriber_id,
      broker_connection_id: row.broker_connection_id,
      signal_action_id: sa.id,
      signal_provider_id: sa.signalProviderId,
      action_type: sa.actionType,
      payload_json: decision.payload,                  // lot resolved, risk applied
      status: decision.allowed ? 'pending' : 'skipped',
      skipped_reason: decision.reason ?? null,
      execute_after: decision.allowed
        ? new Date(Date.now() + provider.auto_execute_delay_sec * 1000)
        : null,
      idempotency_key: null,                           // EA fills in on result
    });
  }

  await db.subscriberActions.insertMany(inserts, { onConflict: 'ignore_on_unique' });
  await markFanoutComplete(sa.id, inserts.length);

  // notify watchers (admin live feed) via Postgres LISTEN
  await db.query(`NOTIFY fanout_complete, '${sa.id}'`);
}
```

The `onConflict: 'ignore_on_unique'` is the key: `(signal_action_id, broker_connection_id)` UNIQUE means a worker retry never double-emits.

---

## Risk-control enforcement pipeline

Order matters. The gates are evaluated in this sequence; the first that fails records the reason and returns `allowed=false`. The subscriber's `subscriber_actions` row is still inserted with `status='skipped'` so the audit trail is intact and the subscriber can ask "why didn't I get this signal?".

| # | Gate | Source | Decision rule | Reason code |
|---|---|---|---|---|
| 1 | Subscription active | `subscriptions.status IN ('trialing','active')` AND `current_period_end > now()` | else skip | `subscription_inactive` |
| 2 | Subscriber not paused | `subscribers.paused_at IS NULL` | else skip | `subscriber_paused` |
| 3 | Provider not halted | `signal_providers.halted = false` | else fan-out aborts entirely | (no row written) |
| 4 | Broker connection active | `broker_connections.status = 'active'` AND `ea_last_seen_at > now() - 5 min` | else skip | `broker_offline` |
| 5 | Instrument allowed | `payload.symbol IN risk_profile.instrument_allowlist` | else skip | `instrument_not_allowed` |
| 6 | Action type allowed | `sa.action_type IN risk_profile.allowed_action_types` | else skip | `action_type_not_allowed` |
| 7 | Min account balance | `broker_connections.account_balance_cents >= risk_profile.min_account_balance_cents` | else skip | `below_min_balance` |
| 8 | Max daily loss not hit | sum(`positions.realized_pnl_cents`) over today across subscriber's positions ≥ -`risk_profile.max_daily_loss_cents` | else skip + auto-pause subscriber | `max_daily_loss_hit` |
| 9 | Consecutive losses guard | last N closed positions all losers AND N >= `pause_after_consecutive_losses` | else skip + auto-pause | `consecutive_losses` |
| 10 | Time-of-day filter | now() ∈ `risk_profile.time_of_day_filter.allowed_windows` | else skip | `outside_time_filter` |
| 11 | OPEN type, position already open | for OPEN/OPEN_INSTANT: subscriber has open position on symbol | else skip (consistent with current single-position invariant) | `position_already_open` |
| 12 | Lot sizing | compute lot from mode (fixed / percent_balance / risk_percent + sl distance) | floor at broker `MinLot`; cap at `risk_profile.max_lots_per_signal`; if floor>cap, skip | `lot_size_zero` |
| 13 | Cost guard (signal-svc level) | provider's daily LLM budget OK | (enforced at signal-svc, not at fan-out) | — |

The output payload per allowed subscriber:
- Inherits the LLM-emitted payload (entry/SL/TPs, etc.) for that action_type.
- OPEN/OPEN_INSTANT: payload extends with `{lot: 0.07, max_loss_cents: ..., risk_profile_id: ..., risk_profile_version: ...}`. The EA receives the resolved lot; it does not run lot math.
- Management actions (no ticket): payload unchanged — EA resolves singleton via `FindSingletonOpenTicket`.

---

## Idempotency strategy

Multiple layers:

1. **Provider-side dedupe (existing)**: `signal_actions.fingerprint` on OPENs (banded by `FINGERPRINT_BAND_PRICE` over `FINGERPRINT_WINDOW_HOURS`). Re-quoted signals don't re-emit.
2. **Fan-out idempotence (new)**: `subscriber_actions UNIQUE(signal_action_id, broker_connection_id)`. Worker retries cannot duplicate.
3. **EA claim idempotence (preserved)**: `UPDATE ... WHERE status='sent'` returns affected-rows=0 on second call; EA treats 0 as "someone else claimed".
4. **EA result idempotence (new)**: `Idempotency-Key` header required on `POST /actions/{id}/result`. Server stores key → response in `audit.idempotency_keys` (24h TTL). Replay returns cached response.
5. **Status-guarded result write (preserved REVIEW.md P0)**: `UPDATE subscriber_actions ... WHERE id=$1 AND status IN ('claimed','watching')`. Late POSTs from stale-claim races return 409.

---

## Replay strategy — catching subscribers up after downtime

The question: a subscriber's EA was offline for 2 hours; what do they execute when it comes back?

**Default policy: don't replay**. The EA polling `/actions?status=sent` will see only:
- Actions still in `status='sent'` because no other EA could claim them (per-connection scoping means there is exactly one EA per `broker_connection`).
- Actions with `execute_after <= now()` AND `expires_at IS NULL OR expires_at > now()`.

**Aging rules**:
- OPEN actions get `expires_at = created_at + 15 min` by default (configurable per provider; market signals stale fast).
- `MOVE_SL_BE` / `CLOSE_PARTIAL` / `CLOSE_FULL` / `MOVE_SL` / management actions get `expires_at = created_at + 5 min`. Stale management is dangerous — closing a position 90 min late is a different trade.
- `REOPEN_LAST` / `REINFORCE`: `expires_at = created_at + 30 min`.
- `ALERT`: no expires; just a notification, never expires from the queue but won't reach the EA (server-side filter).

**The sweeper** `expire_stale_actions` runs every 30s: any `subscriber_actions.status IN ('pending','sent','watching') AND expires_at < now()` flips to `rejected` with `ea_response='expired_before_ea_pickup'`. Subscriber notified per their notification preferences.

**What IS replayed**: nothing automatically. If the subscriber wants to "catch up", they have the admin replay tool (admin app, see §6) which lets a human re-emit a specific historical action. We do not auto-replay — the price has moved, and the subscriber bought a copy service, not a backfill service.

---

## Telegram throttling on notifications

Telegram Bot API caps: ~30 messages/sec per bot, ~1 message/sec to the same chat. With 1 bot serving N subscribers, a hot signal that produces 200 fan-out notifications floods our quota.

Mitigations:
- One bot per ~500 subscribers. We scale by adding bots (each with its own token); the `bot_outbox`-pattern survives but is per-bot.
- The fan-out worker writes `notifications` rows; a separate `dispatch_telegram_notifications` worker drains the queue with a per-bot 25-msg/sec governor.
- For the EA execution event (which fires per subscriber whether the signal is hot or cold), default to **email + in-app** for Starter, add Telegram on Pro+.

See §8-COMMUNICATIONS.md for the throughput math at 100/500/2000 subscribers.

---

## Audit trail per subscriber action

Every `subscriber_actions` row carries enough metadata for the subscriber and the admin to reconstruct "why":
- `signal_action_id` → `messages.id` → raw Telegram text + sender + pipeline meta.
- `status` + `skipped_reason` + `ea_response`.
- `payload_json` showing the resolved lot and risk-profile version applied.

The admin's "Subscriber detail" page surfaces this as a single timeline. The subscriber's "Trade history" surfaces the executed + skipped ones with a "Why was this skipped?" disclosure.
