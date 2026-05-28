# 03 — Data Model

Postgres 16. RDS Multi-AZ. All tables tenant-aware from day one. Naming convention: `snake_case`, plural table names, surrogate `id` PKs as `bigint identity` except where natural keys make sense, `created_at`/`updated_at` everywhere, soft-delete via `deleted_at` only where compliance requires retention (most operational tables hard-delete via retention jobs).

Multi-tenancy model: **two tenant axes**.
1. **Signal-provider tenancy** (`signal_provider_id`) — which channel emitted the action. Today: one (Forex Engineer). Schema-ready for N.
2. **Subscriber tenancy** (`subscriber_id`) — which paying user owns this position, action, broker connection, notification.

Provider-level rows (e.g. `messages`, `signal_actions`) have only `signal_provider_id`. Subscriber-level rows (`subscriber_actions`, `positions`, `notifications`) have both.

**RLS strategy**: RLS enabled on every subscriber-scoped table. Two roles:
- `app_subscriber` — `current_setting('app.subscriber_id')::bigint = subscriber_id`
- `app_admin` — full read; writes via service role only

The web app sets `app.subscriber_id` per request via `SET LOCAL` inside the transaction. The signal-svc and worker connect as the service role (RLS-exempt) since they're fanning out across tenants.

---

## Schema DDL

### identity & access

```sql
CREATE TABLE users (
  id              bigint generated always as identity PRIMARY KEY,
  clerk_user_id   text NOT NULL UNIQUE,
  email           citext NOT NULL UNIQUE,
  email_verified_at timestamptz,
  full_name       text,
  country_code    text,                  -- ISO 3166-1 alpha-2
  locale          text DEFAULT 'en',
  role            text NOT NULL DEFAULT 'subscriber' CHECK (role IN ('subscriber','admin','support','readonly')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz
);
CREATE INDEX users_role_idx ON users(role) WHERE deleted_at IS NULL;

CREATE TABLE organizations (        -- future B2B; one row per org
  id              bigint generated always as identity PRIMARY KEY,
  clerk_org_id    text UNIQUE,
  name            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE org_memberships (
  org_id          bigint NOT NULL REFERENCES organizations(id) ON DELETE CASCADE,
  user_id         bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role            text NOT NULL CHECK (role IN ('owner','admin','member')),
  PRIMARY KEY (org_id, user_id)
);

CREATE TABLE subscribers (          -- the SaaS account; a user becomes a subscriber when billing starts
  id              bigint generated always as identity PRIMARY KEY,
  user_id         bigint NOT NULL UNIQUE REFERENCES users(id) ON DELETE RESTRICT,
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','active','past_due','paused','cancelled','banned')),
  paused_at       timestamptz,       -- subscriber-initiated pause
  paused_reason   text,
  jurisdiction    text,              -- captured during signup; drives geo-block
  kyc_status      text DEFAULT 'not_required' CHECK (kyc_status IN ('not_required','pending','approved','rejected')),
  kyc_provider_ref text,
  marketing_opt_in boolean DEFAULT false,
  tos_version_accepted text,         -- e.g., '2026-05-01'
  tos_accepted_at timestamptz,
  risk_disclosure_version_accepted text,
  risk_disclosure_accepted_at timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  deleted_at      timestamptz
);
CREATE INDEX subscribers_status_idx ON subscribers(status);

CREATE TABLE sessions (             -- Clerk-managed; we mirror minimally for audit
  id              bigint generated always as identity PRIMARY KEY,
  user_id         bigint NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  clerk_session_id text NOT NULL UNIQUE,
  ip              inet,
  user_agent      text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz
);
```

### billing

```sql
CREATE TABLE plans (
  id              text PRIMARY KEY,    -- 'starter','pro','elite' — stable string keys
  display_name    text NOT NULL,
  monthly_cents   int NOT NULL,
  currency        text NOT NULL DEFAULT 'USD',
  features_json   jsonb NOT NULL,      -- {max_broker_connections, max_account_size_cents, support_tier, ...}
  paddle_product_id text NOT NULL,
  paddle_price_id text NOT NULL,
  is_active       boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subscriptions (
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE RESTRICT,
  plan_id         text NOT NULL REFERENCES plans(id),
  paddle_subscription_id text NOT NULL UNIQUE,
  status          text NOT NULL CHECK (status IN ('trialing','active','past_due','paused','cancelled')),
  trial_ends_at   timestamptz,
  current_period_start timestamptz NOT NULL,
  current_period_end   timestamptz NOT NULL,
  cancel_at_period_end boolean DEFAULT false,
  cancelled_at    timestamptz,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX subscriptions_subscriber_idx ON subscriptions(subscriber_id);
CREATE INDEX subscriptions_status_idx ON subscriptions(status);

CREATE TABLE invoices (
  id              bigint generated always as identity PRIMARY KEY,
  subscription_id bigint NOT NULL REFERENCES subscriptions(id),
  paddle_invoice_id text NOT NULL UNIQUE,
  amount_cents    int NOT NULL,
  currency        text NOT NULL,
  status          text NOT NULL CHECK (status IN ('draft','open','paid','void','uncollectible','refunded')),
  paid_at         timestamptz,
  hosted_url      text,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE payment_methods (
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id),
  paddle_pm_id    text NOT NULL,
  brand           text,
  last4           text,
  is_default      boolean NOT NULL DEFAULT false,
  created_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE usage_events (         -- future per-trade or per-feature metering hook
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id),
  kind            text NOT NULL,    -- 'trade_executed','signal_received','llm_replay_used'
  qty             int NOT NULL DEFAULT 1,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  meta_json       jsonb
);
CREATE INDEX usage_events_subscriber_kind_idx ON usage_events(subscriber_id, kind, occurred_at);
```

### product — signal providers, subscriptions to channels, broker connections, risk

```sql
CREATE TABLE signal_providers (
  id              bigint generated always as identity PRIMARY KEY,
  slug            text NOT NULL UNIQUE,    -- 'forex-engineer'
  display_name    text NOT NULL,
  language        text NOT NULL,           -- 'ar'
  symbol          text NOT NULL,           -- 'XAUUSD' (v1 single-symbol invariant)
  profile_json    jsonb NOT NULL,          -- channel profile (vocab, examples)
  profile_version int NOT NULL DEFAULT 1,
  tg_chat_id      bigint NOT NULL,         -- Telethon-watched chat
  tg_session_blob_enc bytea,               -- KMS envelope encryption
  tg_session_dek_enc  bytea,
  is_active       boolean NOT NULL DEFAULT true,
  halted          boolean NOT NULL DEFAULT false,
  halted_reason   text,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE subscriber_channel_subscriptions (   -- "I subscribe to Forex Engineer"
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  is_active       boolean NOT NULL DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (subscriber_id, signal_provider_id)
);

CREATE TABLE broker_connections (
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  display_name    text NOT NULL,           -- 'Exness Standard'
  execution_backend text NOT NULL CHECK (execution_backend IN ('ea','metaapi','ctrader')),
  ea_bearer_token_hash text,               -- bcrypt of the per-EA bearer; raw token is shown once on create
  ea_last_seen_at timestamptz,
  ea_version      text,
  metaapi_account_id text,                 -- when backend='metaapi'
  metaapi_credentials_enc bytea,
  metaapi_dek_enc bytea,
  account_currency text DEFAULT 'USD',
  account_balance_cents bigint,            -- last-known, EA-reported
  account_equity_cents  bigint,
  account_balance_at    timestamptz,
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','active','disabled','revoked')),
  created_at      timestamptz NOT NULL DEFAULT now(),
  revoked_at      timestamptz
);
CREATE INDEX broker_connections_subscriber_idx ON broker_connections(subscriber_id);
CREATE INDEX broker_connections_status_idx ON broker_connections(status);
CREATE INDEX broker_connections_token_hash_idx ON broker_connections(ea_bearer_token_hash) WHERE ea_bearer_token_hash IS NOT NULL;

CREATE TABLE risk_profiles (        -- 1:1 with subscriber_channel_subscriptions (or N:1 if we add per-broker overrides later)
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  broker_connection_id bigint NOT NULL REFERENCES broker_connections(id) ON DELETE CASCADE,
  lot_sizing_mode text NOT NULL DEFAULT 'percent_balance' CHECK (lot_sizing_mode IN ('fixed','percent_balance','risk_percent')),
  lot_fixed       numeric(10,2),           -- when mode='fixed'
  lots_per_100_balance numeric(10,4),      -- when mode='percent_balance'; default 0.01
  risk_percent_per_trade numeric(5,2),     -- when mode='risk_percent'; e.g. 1.0 = 1%
  max_lots_per_signal numeric(10,2) NOT NULL DEFAULT 1.0,
  max_open_lots   numeric(10,2),
  max_daily_loss_cents bigint,             -- subscriber-defined drawdown cap
  pause_after_consecutive_losses int,
  min_account_balance_cents bigint,        -- skip signals when below
  instrument_allowlist text[] DEFAULT '{XAUUSD}',
  allowed_action_types text[] NOT NULL DEFAULT '{OPEN,MOVE_SL_BE,MOVE_SL,CLOSE_PARTIAL,CLOSE_FULL,REOPEN_LAST,REINFORCE,TIGHTEN_SL,MODIFY_TPS,OPEN_INSTANT,ATTACH_SIGNAL,CANCEL_PENDING}',
  time_of_day_filter jsonb,                -- {tz, allowed_windows:[{start,end}]}
  chase_price_enabled boolean DEFAULT true,
  chase_min_reward_ratio numeric(3,2) DEFAULT 0.5,
  enable_instant_open boolean DEFAULT false,
  enable_reinforce boolean DEFAULT true,
  enable_ai_partial_and_be boolean DEFAULT true,
  created_at      timestamptz NOT NULL DEFAULT now(),
  updated_at      timestamptz NOT NULL DEFAULT now(),
  UNIQUE (subscriber_id, broker_connection_id)
);
```

### operational — messages, actions, positions, signal memory

```sql
-- Provider-side messages (one row per Telegram message, regardless of how many subscribers see it)
CREATE TABLE messages (
  id              bigint generated always as identity PRIMARY KEY,
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  tg_chat_id      bigint NOT NULL,
  tg_message_id   bigint NOT NULL,
  sender          text,
  text            text,
  received_at     timestamptz NOT NULL DEFAULT now(),
  is_backfill     boolean NOT NULL DEFAULT false,
  reply_to_tg_message_id bigint,
  decided_stage   text,                   -- prefilter_drop|trigger_text|trigger_embedding|triage_ignored|interpreted_signal|interpreted_ignore
  decided_outcome text,
  decided_at      timestamptz,
  pipeline_meta_json jsonb,
  UNIQUE (signal_provider_id, tg_chat_id, tg_message_id)
);
CREATE INDEX messages_provider_received_idx ON messages(signal_provider_id, received_at DESC);

-- Provider-side actions: one row per (action emitted by LLM), tenant-scope = signal_provider only.
-- These are the "blueprint" actions. Fanout worker copies them into subscriber_actions.
CREATE TABLE signal_actions (
  id              bigint generated always as identity PRIMARY KEY,
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  source_msg_id   bigint REFERENCES messages(id),
  action_type     text NOT NULL CHECK (action_type IN (
    'OPEN','MODIFY','CLOSE','CLOSE_ALL','ALERT',
    'MOVE_SL_BE','MOVE_SL','CLOSE_PARTIAL','CLOSE_FULL',
    'REOPEN_LAST','REINFORCE','TIGHTEN_SL','MODIFY_TPS',
    'OPEN_INSTANT','ATTACH_SIGNAL','CANCEL_PENDING')),
  payload_json    jsonb NOT NULL,
  fingerprint     text,                   -- OPENs only
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz,            -- for pending limits / watching
  fanout_completed_at timestamptz,        -- worker timestamp
  fanout_subscriber_count int             -- how many subscriber_actions emitted
);
CREATE INDEX signal_actions_provider_created_idx ON signal_actions(signal_provider_id, created_at DESC);
CREATE INDEX signal_actions_fanout_pending_idx ON signal_actions(id) WHERE fanout_completed_at IS NULL;
CREATE INDEX signal_actions_fingerprint_idx ON signal_actions(signal_provider_id, fingerprint) WHERE fingerprint IS NOT NULL;

-- Subscriber-side actions: one row per (signal_action × eligible subscriber). EA polls these.
CREATE TABLE subscriber_actions (
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE RESTRICT,
  broker_connection_id bigint NOT NULL REFERENCES broker_connections(id),
  signal_action_id bigint NOT NULL REFERENCES signal_actions(id),
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  action_type     text NOT NULL,
  payload_json    jsonb NOT NULL,         -- per-subscriber payload (lot size resolved, risk applied)
  status          text NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','cancelled','sent','claimed','watching','executed','failed','rejected','skipped')),
  skipped_reason  text,                   -- 'paused','below_min_balance','outside_time_filter','disallowed_action_type','instrument_not_allowed','max_daily_loss_hit'
  execute_after   timestamptz,            -- promoter gate
  claimed_at      timestamptz,
  claimed_by_token_hash text,             -- bind claim to EA bearer
  executed_at     timestamptz,
  ea_response     text,
  idempotency_key text,                   -- EA-supplied on result POST
  created_at      timestamptz NOT NULL DEFAULT now(),
  expires_at      timestamptz,
  UNIQUE (signal_action_id, broker_connection_id)
);
CREATE INDEX sub_actions_broker_status_idx ON subscriber_actions(broker_connection_id, status) WHERE status IN ('sent','claimed','watching');
CREATE INDEX sub_actions_subscriber_created_idx ON subscriber_actions(subscriber_id, created_at DESC);
CREATE INDEX sub_actions_signal_idx ON subscriber_actions(signal_action_id);

ALTER TABLE subscriber_actions ENABLE ROW LEVEL SECURITY;
CREATE POLICY sub_actions_owner ON subscriber_actions
  FOR SELECT TO app_subscriber
  USING (subscriber_id = current_setting('app.subscriber_id')::bigint);

-- Subscriber-side positions.
CREATE TABLE positions (
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE RESTRICT,
  broker_connection_id bigint NOT NULL REFERENCES broker_connections(id),
  subscriber_action_id bigint NOT NULL REFERENCES subscriber_actions(id),
  mt5_ticket      bigint NOT NULL,
  symbol          text NOT NULL,
  side            text NOT NULL CHECK (side IN ('buy','sell')),
  volume          numeric(10,2) NOT NULL,
  original_volume numeric(10,2) NOT NULL,    -- never updated; the AI's "already-partial-closed" oracle
  entry_price     numeric(12,4) NOT NULL,
  sl              numeric(12,4),
  tp              numeric(12,4),
  partial_close_count int NOT NULL DEFAULT 0,
  sl_moved_at     timestamptz,
  exit_price      numeric(12,4),
  realized_pnl_cents bigint NOT NULL DEFAULT 0,
  status          text NOT NULL CHECK (status IN ('open','closed')),
  opened_at       timestamptz NOT NULL,
  closed_at       timestamptz,
  close_reason    text,
  is_naked        boolean NOT NULL DEFAULT false,
  naked_opened_at timestamptz,
  UNIQUE (broker_connection_id, mt5_ticket)
);
CREATE INDEX positions_subscriber_status_idx ON positions(subscriber_id, status);
CREATE INDEX positions_broker_open_idx ON positions(broker_connection_id) WHERE status = 'open';
ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY positions_owner ON positions FOR SELECT TO app_subscriber
  USING (subscriber_id = current_setting('app.subscriber_id')::bigint);

CREATE TABLE signal_memory (
  id              bigint generated always as identity PRIMARY KEY,
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  category        text NOT NULL,
  text            text NOT NULL,
  created_at      timestamptz NOT NULL DEFAULT now(),
  cleared_at      timestamptz
);
CREATE INDEX signal_memory_provider_active_idx ON signal_memory(signal_provider_id, created_at DESC) WHERE cleared_at IS NULL;

CREATE TABLE unmatched_messages (
  id              bigint generated always as identity PRIMARY KEY,
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  message_id      bigint NOT NULL REFERENCES messages(id),
  suggested_action_type text NOT NULL,
  suggested_phrase text,
  resolved_at     timestamptz,
  resolved_by_user_id bigint REFERENCES users(id),
  resolution      text                    -- 'curated','ignored','duplicate'
);

CREATE TABLE notifications (
  id              bigint generated always as identity PRIMARY KEY,
  subscriber_id   bigint NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
  channel         text NOT NULL CHECK (channel IN ('email','telegram','webhook','inapp')),
  event_type      text NOT NULL,         -- 'action_executed','position_closed','billing_failed', ...
  payload_json    jsonb NOT NULL,
  scheduled_for   timestamptz NOT NULL DEFAULT now(),
  sent_at         timestamptz,
  failed_at       timestamptz,
  failure_reason  text,
  retry_count     int NOT NULL DEFAULT 0,
  related_action_id bigint REFERENCES subscriber_actions(id),
  related_position_id bigint REFERENCES positions(id)
);
CREATE INDEX notifications_pending_idx ON notifications(scheduled_for) WHERE sent_at IS NULL AND failed_at IS NULL;
CREATE INDEX notifications_subscriber_idx ON notifications(subscriber_id, scheduled_for DESC);

CREATE TABLE provider_settings (    -- per-provider runtime config (replaces today's settings table at provider scope)
  signal_provider_id bigint NOT NULL REFERENCES signal_providers(id),
  key             text NOT NULL,
  value_json      jsonb NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (signal_provider_id, key)
);

CREATE TABLE system_config (        -- global app config
  key             text PRIMARY KEY,
  value_json      jsonb NOT NULL,
  updated_at      timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE market_snapshots (     -- per-symbol heartbeat (replaces settings.market_XAUUSD_*)
  symbol          text PRIMARY KEY,
  bid             numeric(12,4),
  ask             numeric(12,4),
  m15             jsonb,
  h1              jsonb,
  h4              jsonb,
  d1              jsonb,
  adr20           numeric(12,4),
  adx_h1          numeric(6,2),
  updated_at      timestamptz NOT NULL
);
```

### audit

```sql
CREATE TABLE audit.settings_history (
  id              bigint generated always as identity PRIMARY KEY,
  scope           text NOT NULL CHECK (scope IN ('system','provider','subscriber')),
  scope_id        bigint,
  key             text NOT NULL,
  old_value_json  jsonb,
  new_value_json  jsonb,
  changed_by_user_id bigint REFERENCES users(id),
  changed_at      timestamptz NOT NULL DEFAULT now(),
  reason          text
);
CREATE INDEX settings_history_scope_idx ON audit.settings_history(scope, scope_id, changed_at DESC);

CREATE TABLE audit.admin_actions (
  id              bigint generated always as identity PRIMARY KEY,
  admin_user_id   bigint NOT NULL REFERENCES users(id),
  action          text NOT NULL,         -- 'subscriber.pause','subscriber.refund','provider.halt','impersonate_start'
  target_kind     text,                  -- 'subscriber','provider','action','position'
  target_id       bigint,
  meta_json       jsonb,
  ip              inet,
  user_agent      text,
  occurred_at     timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX admin_actions_admin_idx ON audit.admin_actions(admin_user_id, occurred_at DESC);
CREATE INDEX admin_actions_target_idx ON audit.admin_actions(target_kind, target_id);

CREATE TABLE audit.impersonation_log (
  id              bigint generated always as identity PRIMARY KEY,
  admin_user_id   bigint NOT NULL REFERENCES users(id),
  impersonated_subscriber_id bigint NOT NULL REFERENCES subscribers(id),
  started_at      timestamptz NOT NULL DEFAULT now(),
  ended_at        timestamptz,
  mode            text NOT NULL CHECK (mode IN ('read_only','full')),
  reason          text NOT NULL,
  read_paths      text[],                -- which routes were viewed
  writes_json     jsonb                  -- writes performed under impersonation
);

CREATE TABLE audit.api_errors (     -- replaces the 422-handler local-file log
  id              bigint generated always as identity PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  service         text NOT NULL,        -- 'ea-api','web','signal-svc'
  status_code     int NOT NULL,
  route           text NOT NULL,
  method          text NOT NULL,
  request_body    jsonb,
  errors_json     jsonb,
  ip              inet,
  bearer_hash     text,
  subscriber_id   bigint REFERENCES subscribers(id),
  broker_connection_id bigint REFERENCES broker_connections(id)
);
CREATE INDEX api_errors_recent_idx ON audit.api_errors(occurred_at DESC);

CREATE TABLE audit.ai_calls (       -- replaces logs/ai_calls.jsonl
  id              bigint generated always as identity PRIMARY KEY,
  occurred_at     timestamptz NOT NULL DEFAULT now(),
  signal_provider_id bigint REFERENCES signal_providers(id),
  message_id      bigint REFERENCES messages(id),
  stage           text NOT NULL,
  model           text NOT NULL,
  input_tokens    int,
  output_tokens   int,
  cache_read_tokens int,
  cache_creation_tokens int,
  latency_ms      int,
  cost_usd_micro  bigint,                -- micro-USD (1e6) for integer math
  raw_response    text                   -- only when failed or replay needed; otherwise redacted
);
CREATE INDEX ai_calls_provider_time_idx ON audit.ai_calls(signal_provider_id, occurred_at DESC);
```

---

## Retention policy

| Table | Retention |
|---|---|
| `messages` | 7 years (regulatory record-keeping for trade signals) |
| `signal_actions` | 7 years |
| `subscriber_actions` | 7 years |
| `positions` | 7 years |
| `audit.*` | 7 years |
| `audit.ai_calls.raw_response` | 90 days (truncate after) — PII/cost trail kept indefinitely |
| `notifications` | 1 year |
| `sessions` | 1 year |
| `usage_events` | 3 years (billing recon) |
| `signal_memory` | 30 days after `cleared_at` |
| `market_snapshots` | rolling — no history kept beyond live row |
| `subscribers.deleted_at` | soft-delete; PII scrubbed at 30d post-delete (see §9 GDPR) |

Archival: monthly `pg_dump --schema-only` + `COPY ... TO S3` for tables older than 12 months; live DB keeps last 12 months on hot storage. Restore via `pg_restore` from S3 archive into a side schema for forensic queries.

---

## Migration from SQLite (one-time)

A standalone Python script (`apps/signal-svc/scripts/migrate_sqlite_to_pg.py`) ports the operator's existing single-stack SQLite DB into the Postgres schema as **subscriber #1** (the operator).

Mapping:
- The single SQLite `settings` row set → `provider_settings` (LLM keys, models) + `subscribers[user_id=operator].risk_profile` (EA tunables) + `system_config` (cost guard).
- `messages` → `messages` 1:1, `signal_provider_id=1` (Forex Engineer).
- `actions` → both `signal_actions` (one row per distinct LLM-emitted action, by source_msg_id) and `subscriber_actions` (one row, subscriber=operator).
- `positions` → `positions` with the operator's first `broker_connection`.
- `signal_memory` → `signal_memory`.
- `unmatched_messages` → `unmatched_messages`.
- `bot_outbox` → discarded (notifications regenerated forward only).
- DPAPI-encrypted secrets → decrypted on the Windows machine, piped to the migration script over stdin, re-encrypted with KMS envelope encryption.

The migration is **one-way**. The Windows install becomes read-only after cutover; the operator points the new EA at the cloud API and shuts down NSSM services. See §12-MIGRATION-AND-SUNSET.md.

---

## Action lifecycle invariants (carried forward)

- `subscriber_actions.status` transitions: `pending → sent → claimed → {executed|failed|rejected|watching}`, plus `pending → cancelled` and `pending → skipped` for fan-out-time decisions, plus `watching → {executed|cancelled|rejected}`.
- `subscriber_actions` result POST must enforce: `UPDATE ... WHERE id=$1 AND status IN ('claimed','watching')`. Else 409. (REVIEW.md P0 carried forward.)
- `positions.original_volume` set ONCE on insert. Healing case (volume-up post-partial-fill) is the only update path.
- `positions.mt5_ticket` UNIQUE per `broker_connection_id`. INSERT uses `ON CONFLICT DO NOTHING` — first insert wins.
- `partial_close_count` increments only when `new_volume < current_volume`.
- `sl_moved_at` set once on first SL change.
- `subscriber_actions.idempotency_key` — required on result POSTs. EA computes as `sha256(action_id || ea_bearer_token_hash || attempt_n)`. The API uniques on (subscriber_action_id, idempotency_key) for the audit_terminal write path.
