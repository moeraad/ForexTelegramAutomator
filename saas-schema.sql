-- =============================================================================
-- CopyTrades SaaS — Postgres schema (MVP / v1)
-- Derived from saas discussion.md, points 1–18.
--
-- Conventions:
--   * Postgres 16+. All timestamps are timestamptz (UTC). The desktop app stored
--     ISO-8601 text in SQLite; here we use native timestamptz — never text dates.
--   * Primary keys are UUID (prefer uuidv7 for index locality / shard-friendliness,
--     point 5). Helper default below; swap to uuidv7() on PG18+.
--   * State columns are text + CHECK (point 6: CHECK validates the enum value;
--     ALLOWED TRANSITIONS are enforced in the application state machine, not triggers).
--   * Multi-tenant: shared tables + tenant columns (provider_id / subscriber_id) + RLS
--     (point 5). RLS pattern illustrated at the bottom; apply to every tenant table.
--   * Money: numeric(18,6) for FX prices/lots, numeric(12,2) for fiat.
--   * v1 = MetaApi-managed execution ONLY (point 18). EA-path columns/tables are
--     marked DEFERRED but the model leaves room (execution path enum).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS pgcrypto;   -- gen_random_uuid()
-- On PG18+: prefer uuidv7() — define a wrapper so table defaults don't change later.
-- CREATE FUNCTION new_id() RETURNS uuid LANGUAGE sql AS $$ SELECT uuidv7() $$;
CREATE OR REPLACE FUNCTION new_id() RETURNS uuid LANGUAGE sql VOLATILE
  AS $$ SELECT gen_random_uuid() $$;

CREATE OR REPLACE FUNCTION touch_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN NEW.updated_at = now(); RETURN NEW; END $$;


-- =============================================================================
-- B · IDENTITY & TENANCY
-- =============================================================================

-- Subscribers (end users). Auth itself lives in the hosted IdP (Supabase/Clerk,
-- point 7); we mirror the external id + the fields we need for routing/access.
CREATE TABLE subscribers (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    idp_user_id     text NOT NULL UNIQUE,            -- subject from hosted IdP
    email           citext NOT NULL UNIQUE,
    display_name    text,
    country_code    char(2),                         -- for geo-gating (point 15)
    status          text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','suspended','closed')),
    -- billing mirror (Stripe = source of truth, mirrored here for fast checks; point 11)
    stripe_customer_id   text UNIQUE,
    platform_tier        text NOT NULL DEFAULT 'free'
                      CHECK (platform_tier IN ('free','self_hosted','managed')),
    tier_status          text NOT NULL DEFAULT 'active'
                      CHECK (tier_status IN ('active','past_due','canceled','trialing')),
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_subscribers_touch BEFORE UPDATE ON subscribers
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Providers (signal-channel owners). Onboarded operator-assisted, invite-only (point 13).
CREATE TABLE providers (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    idp_user_id     text UNIQUE,                      -- provider's login (nullable until they activate)
    email           citext NOT NULL UNIQUE,
    display_name    text NOT NULL,
    status          text NOT NULL DEFAULT 'applied'
                      CHECK (status IN ('applied','approved','active','suspended','rejected')),
    -- Stripe Connect (Express) payout side (point 13). Connect = source of truth.
    stripe_connect_id    text UNIQUE,
    payout_eligible      boolean NOT NULL DEFAULT false,  -- false where Connect unavailable (point 13 geo gap)
    commission_pct       numeric(5,2) NOT NULL DEFAULT 25.00,  -- platform cut (point 11)
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_providers_touch BEFORE UPDATE ON providers
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Platform staff. RBAC (point 17): support / ops / finance / superadmin.
CREATE TABLE admin_users (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    idp_user_id     text NOT NULL UNIQUE,
    email           citext NOT NULL UNIQUE,
    role            text NOT NULL
                      CHECK (role IN ('support','ops','finance','superadmin')),
    mfa_enrolled    boolean NOT NULL DEFAULT false,   -- MFA mandatory (point 7/17)
    created_at      timestamptz NOT NULL DEFAULT now()
);


-- =============================================================================
-- C · INGESTION (listener, channels, prompt layering)
-- =============================================================================

-- Operator-owned Telethon accounts. Single account for MVP; pool-ready (point 1).
CREATE TABLE telethon_accounts (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    label           text NOT NULL,
    phone_e164      text,                             -- the SIM behind it
    -- session string stored ENCRYPTED at rest (KMS envelope; never plaintext)
    session_enc     bytea,
    status          text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('active','challenged','banned','disabled')),
    channel_count   int NOT NULL DEFAULT 0,           -- against the 500 cap
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE TRIGGER trg_telethon_touch BEFORE UPDATE ON telethon_accounts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Language / asset-class profiles — the middle prompt layer (point 2).
-- Platform-maintained (e.g. arabic-gold, english-forex). Shared across channels.
CREATE TABLE profiles (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    key             text NOT NULL UNIQUE,             -- 'arabic-gold'
    language        text NOT NULL,
    asset_class     text NOT NULL,
    content         jsonb NOT NULL,                   -- idempotency/shorthand/filter rules
    version         int NOT NULL DEFAULT 1,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);

-- Channels (signal sources). One provider → many channels.
CREATE TABLE channels (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    provider_id     uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    telethon_account_id uuid REFERENCES telethon_accounts(id),  -- which reader serves it (point 1)
    tg_chat_id      bigint,                           -- Telegram chat id (bound after ownership verify)
    name            text NOT NULL,
    description     text,
    profile_id      uuid REFERENCES profiles(id),     -- attached language/asset profile (point 2)
    -- marketplace (points 11/14)
    monthly_price   numeric(12,2) NOT NULL DEFAULT 0, -- provider-set channel price
    listing         text NOT NULL DEFAULT 'unlisted'
                      CHECK (listing IN ('public','unlisted','invite_only')),
    free_trial_days int NOT NULL DEFAULT 0,           -- provider opt-in (point 14)
    status          text NOT NULL DEFAULT 'onboarding'
                      CHECK (status IN ('onboarding','verifying','active','paused','suspended')),
    -- ownership verification (point 13)
    verify_code     text,
    verified_at     timestamptz,
    tracked_since   timestamptz,                      -- min-track-record gate for public listing (point 14)
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_channels_provider ON channels(provider_id);
CREATE INDEX idx_channels_listing  ON channels(listing) WHERE listing = 'public';
CREATE TRIGGER trg_channels_touch BEFORE UPDATE ON channels
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Per-channel overlay — bottom prompt layer (points 2, 16).
-- Replaces the desktop single profile.json. Versioned; accepted CLL rules land here (v2).
CREATE TABLE channel_overlays (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    channel_id      uuid NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    version         int NOT NULL DEFAULT 1,
    is_active       boolean NOT NULL DEFAULT false,   -- only one active per channel (index below)
    vocab           jsonb NOT NULL DEFAULT '{}'::jsonb,   -- phrase → action_type
    worked_examples jsonb NOT NULL DEFAULT '[]'::jsonb,   -- few-shot anchors (also the replay test set, point 13B)
    noise_patterns      jsonb NOT NULL DEFAULT '[]'::jsonb,
    triage_keep_triggers jsonb NOT NULL DEFAULT '[]'::jsonb,
    triggers        jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at      timestamptz NOT NULL DEFAULT now()
);
-- exactly one active overlay per channel
CREATE UNIQUE INDEX uq_overlay_active ON channel_overlays(channel_id) WHERE is_active;


-- =============================================================================
-- D · SUBSCRIPTIONS & EXECUTION WIRING
-- =============================================================================

-- subscriber ↔ channel link. subscribed_at enforces no-retroactive-execution (point 4).
CREATE TABLE subscriptions (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    subscriber_id   uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    channel_id      uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
    status          text NOT NULL DEFAULT 'active'
                      CHECK (status IN ('trialing','active','paused','past_due','canceled')),
    subscribed_at   timestamptz NOT NULL DEFAULT now(),   -- signals strictly after this route (point 4)
    canceled_at     timestamptz,
    -- unsubscribe behavior choice (point 4)
    on_cancel       text NOT NULL DEFAULT 'manage_to_close'
                      CHECK (on_cancel IN ('manage_to_close','stop_immediately')),
    stripe_subscription_item_id text,                 -- billing mirror (point 11)
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (subscriber_id, channel_id)
);
CREATE INDEX idx_subs_channel ON subscriptions(channel_id) WHERE status IN ('active','trialing');
CREATE INDEX idx_subs_subscriber ON subscriptions(subscriber_id);

-- Per-subscription declarative filters (point 3). Scope: per (channel, symbol).
CREATE TABLE subscription_filters (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    subscription_id uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    symbol          text,                             -- NULL = applies to all symbols
    disabled_action_types text[] NOT NULL DEFAULT '{}',  -- e.g. {CLOSE_PARTIAL}
    lot_mode        text NOT NULL DEFAULT 'fixed'
                      CHECK (lot_mode IN ('fixed','risk_percent','multiplier')),  -- point 8
    lot_value       numeric(18,6),                    -- lots / % / multiplier per lot_mode
    max_lots        numeric(18,6),                    -- cap (point 3)
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_filters_sub ON subscription_filters(subscription_id);

-- Execution account = where a subscriber's trades land.
-- v1: path='metaapi' only. EA path DEFERRED to v1.1 (point 18) but modelled.
-- CRITICAL (point 12): broker password is NEVER stored. MetaApi custodies it.
CREATE TABLE execution_accounts (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    subscriber_id   uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    path            text NOT NULL CHECK (path IN ('metaapi','ea')),  -- 'ea' deferred
    -- MetaApi-managed (point 12): we store only the account id + KMS-encrypted token.
    metaapi_account_id   text,                        -- MetaApi's account reference
    metaapi_token_enc    bytea,                       -- KMS envelope-encrypted; trade authority
    broker_server        text,                        -- non-secret; for display/diagnostics
    broker_login         text,                        -- non-secret account number
    -- EA path (DEFERRED): ea_token_hash text, ...
    status          text NOT NULL DEFAULT 'provisioning'
                      CHECK (status IN ('provisioning','connected','deployed',
                                        'undeployed','disconnected','failed','revoked')),
    -- server-side hard caps — managed analog of EA-local caps (points 8/12)
    cap_max_lots        numeric(18,6),
    cap_max_open_trades int,
    cap_max_daily_loss  numeric(12,2),
    last_connected_at   timestamptz,
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_execacct_subscriber ON execution_accounts(subscriber_id);
CREATE TRIGGER trg_execacct_touch BEFORE UPDATE ON execution_accounts
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();


-- =============================================================================
-- F · SIGNAL FLOW — two-tier state machine (points 2, 6)
-- =============================================================================

-- Channel-level: one row per AI inference output (point 2/6). Pre-fan-out.
CREATE TABLE signal_actions (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    channel_id      uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
    source_message_id   text,                         -- Telegram message id (dedup/audit)
    action_type     text NOT NULL CHECK (action_type IN (
                        'OPEN','MOVE_SL_BE','MOVE_SL','CLOSE_PARTIAL','CLOSE_FULL',
                        'REOPEN_LAST','REINFORCE','TIGHTEN_SL','ALERT',
                        'PENDING_ORDER','CANCEL_PENDING')),   -- point 3 (last two deferred-use)
    symbol          text,                             -- canonical symbol (translated per-broker at fan-out)
    payload         jsonb NOT NULL DEFAULT '{}'::jsonb,
    schema_version  int NOT NULL DEFAULT 1,           -- action schema versioning (point 3)
    interpreted_at  timestamptz NOT NULL DEFAULT now(),
    state           text NOT NULL DEFAULT 'interpreted'
                      CHECK (state IN ('interpreted','fanned_out','archived')),
    fan_out_complete boolean NOT NULL DEFAULT false,  -- restart-safe fan-out (point 4)
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_sigact_channel_time ON signal_actions(channel_id, interpreted_at DESC);
CREATE INDEX idx_sigact_pending_fanout ON signal_actions(id) WHERE NOT fan_out_complete;

-- Per-subscriber: N rows per signal_action (point 2/6). The lifecycle lives here.
CREATE TABLE subscriber_executions (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    signal_action_id uuid NOT NULL REFERENCES signal_actions(id) ON DELETE RESTRICT,
    subscriber_id   uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    subscription_id uuid NOT NULL REFERENCES subscriptions(id) ON DELETE CASCADE,
    execution_account_id uuid REFERENCES execution_accounts(id),
    -- translated to the subscriber's broker before dispatch (point 8/10)
    broker_symbol   text,
    resolved_lots   numeric(18,6),
    state           text NOT NULL DEFAULT 'pending' CHECK (state IN (
                        'pending','pending_approval','sent','claimed',
                        'executed','failed','rejected','watching',
                        'skipped_filter','skipped_kill_switch',
                        'expired_approval','expired_offline')),   -- point 6
    reason          text,                             -- e.g. already_partialed, no_subscriber_history
    result_payload  jsonb,                            -- broker ticket, fill price, etc.
    created_at      timestamptz NOT NULL DEFAULT now(),
    updated_at      timestamptz NOT NULL DEFAULT now(),
    -- fan-out idempotency: a signal reaches each subscriber at most once (point 4)
    UNIQUE (signal_action_id, subscriber_id)
);
CREATE INDEX idx_exec_open_work ON subscriber_executions(state)
    WHERE state IN ('pending','pending_approval','sent','claimed','watching');
CREATE INDEX idx_exec_subscriber_time ON subscriber_executions(subscriber_id, created_at DESC);
CREATE TRIGGER trg_exec_touch BEFORE UPDATE ON subscriber_executions
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- Subscriber positions. Carries the desktop state fields (points 2/10).
CREATE TABLE positions (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    subscriber_id   uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    subscription_id uuid REFERENCES subscriptions(id) ON DELETE SET NULL,
    channel_id      uuid REFERENCES channels(id) ON DELETE SET NULL,
    broker_ticket   text,                             -- MetaApi position id
    symbol          text NOT NULL,                    -- broker symbol
    side            text NOT NULL CHECK (side IN ('buy','sell')),
    volume          numeric(18,6) NOT NULL,
    original_volume numeric(18,6) NOT NULL,           -- snapshot, never updated (point 2 convention)
    partial_close_count int NOT NULL DEFAULT 0,       -- per-subscriber idempotency (point 10)
    entry_price     numeric(18,6),
    sl              numeric(18,6),
    sl_moved_at     timestamptz,                      -- set once on first SL change (point 2)
    status          text NOT NULL DEFAULT 'open'
                      CHECK (status IN ('open','closed')),
    opened_at       timestamptz NOT NULL DEFAULT now(),
    closed_at       timestamptz,
    close_reason    text,
    updated_at      timestamptz NOT NULL DEFAULT now()
);
-- INVARIANT (point 2): one open position per (subscriber, symbol)
CREATE UNIQUE INDEX uq_one_open_per_symbol
    ON positions(subscriber_id, symbol) WHERE status = 'open';
CREATE INDEX idx_positions_subscriber_open
    ON positions(subscriber_id) WHERE status = 'open';
CREATE TRIGGER trg_positions_touch BEFORE UPDATE ON positions
  FOR EACH ROW EXECUTE FUNCTION touch_updated_at();

-- In-flight autonomous management plans (chase/stage/trail) — the shared plan schema
-- (point 8). v1 runs the Python interpreter; chase/ATR DEFERRED to v1.1 (point 18),
-- but the plan record (staged closes + native SL/TP) ships in v1.
CREATE TABLE trade_plans (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    position_id     uuid NOT NULL REFERENCES positions(id) ON DELETE CASCADE,
    plan            jsonb NOT NULL,                   -- entry zone, stage table, trail cfg, native SL/TP
    stage           int NOT NULL DEFAULT 0,
    active          boolean NOT NULL DEFAULT true,
    updated_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (position_id)                              -- dedupe by position (desktop RegisterPlan rule)
);


-- =============================================================================
-- G · MARKET REFERENCE (point 10) — one price per symbol, not per broker
-- =============================================================================
CREATE TABLE market_reference (
    symbol          text PRIMARY KEY,                 -- canonical symbol
    bid             numeric(18,6),
    ask             numeric(18,6),
    mid             numeric(18,6),
    updated_at      timestamptz NOT NULL DEFAULT now()  -- >60s old => STALE in prompt
);


-- =============================================================================
-- H · COST METERING (point 11) — provider-borne, variable AI cost
-- =============================================================================
-- Per-interpreter-call ledger, keyed by channel. Aggregated into provider payout.
CREATE TABLE ai_usage_ledger (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    channel_id      uuid NOT NULL REFERENCES channels(id) ON DELETE RESTRICT,
    signal_action_id uuid REFERENCES signal_actions(id) ON DELETE SET NULL,
    model           text NOT NULL,
    input_tokens    int NOT NULL DEFAULT 0,
    output_tokens   int NOT NULL DEFAULT 0,
    cache_read_tokens int NOT NULL DEFAULT 0,
    cost_usd        numeric(12,6) NOT NULL DEFAULT 0,  -- metered cost → deducted from payout
    call_kind       text NOT NULL CHECK (call_kind IN ('triage','interpret','extract','learn')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_ai_usage_channel_time ON ai_usage_ledger(channel_id, created_at DESC);

-- Monthly provider payout rollup (point 13). Revenue share − metered AI cost.
CREATE TABLE provider_payouts (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    provider_id     uuid NOT NULL REFERENCES providers(id) ON DELETE RESTRICT,
    period_start    date NOT NULL,
    period_end      date NOT NULL,
    gross_revenue   numeric(12,2) NOT NULL DEFAULT 0,  -- provider share of channel subs
    ai_cost         numeric(12,2) NOT NULL DEFAULT 0,  -- metered, from ai_usage_ledger
    chargebacks     numeric(12,2) NOT NULL DEFAULT 0,  -- clawed back (point 13)
    net_payout      numeric(12,2) NOT NULL DEFAULT 0,
    status          text NOT NULL DEFAULT 'pending'
                      CHECK (status IN ('pending','below_threshold','paid','failed')),
    stripe_transfer_id text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider_id, period_start)
);


-- =============================================================================
-- I · AUDIT & CONSENT (points 4, 6, 12, 17) — append-only
-- =============================================================================

-- Every subscriber_execution state transition (point 6).
CREATE TABLE execution_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    execution_id    uuid NOT NULL REFERENCES subscriber_executions(id) ON DELETE CASCADE,
    from_state      text,
    to_state        text NOT NULL,
    reason          text,
    actor           text NOT NULL CHECK (actor IN ('subscriber','system','executor','admin')),
    payload_snapshot jsonb,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_execevents_exec ON execution_events(execution_id, at);

-- signal_actions transitions (provider-side analytics, point 6).
CREATE TABLE signal_events (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    signal_action_id uuid NOT NULL REFERENCES signal_actions(id) ON DELETE CASCADE,
    from_state      text,
    to_state        text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now()
);

-- Admin actions — every privileged operation (point 17). Immutable.
CREATE TABLE admin_audit_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    admin_user_id   uuid NOT NULL REFERENCES admin_users(id),
    action          text NOT NULL,                    -- e.g. 'circuit_breaker_trip','refund','channel_halt'
    target_type     text,
    target_id       uuid,
    before          jsonb,
    after           jsonb,
    reason          text,
    at              timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX idx_adminaudit_time ON admin_audit_log(at DESC);

-- Break-glass: time-boxed, reason-logged tenant-data access (point 17).
CREATE TABLE admin_access_grants (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    admin_user_id   uuid NOT NULL REFERENCES admin_users(id),
    subscriber_id   uuid REFERENCES subscribers(id),
    reason          text NOT NULL,
    granted_at      timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL
);

-- Credential / trade-authority consent (point 12), versioned + timestamped.
CREATE TABLE consent_log (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    subscriber_id   uuid NOT NULL REFERENCES subscribers(id) ON DELETE CASCADE,
    consent_kind    text NOT NULL,                    -- 'trade_authority','tos','risk_disclosure'
    document_version text NOT NULL,
    at              timestamptz NOT NULL DEFAULT now(),
    ip              inet
);


-- =============================================================================
-- J · DEFERRED to v2 (Channel Learning Loop, points 13B/16) — schema stubs
-- =============================================================================
-- Shipped empty in v1; populated when the CLL/extraction tool lands.
CREATE TABLE learning_samples (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    channel_id      uuid NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    raw_text        text NOT NULL,
    category        text,
    action_types    text[],
    triage_decision text,
    created_at      timestamptz NOT NULL DEFAULT now(),
    UNIQUE (channel_id, md5(raw_text))                -- per-channel dedup
);
CREATE TABLE learning_suggestions (
    id              uuid PRIMARY KEY DEFAULT new_id(),
    channel_id      uuid NOT NULL REFERENCES channels(id) ON DELETE CASCADE,
    rule_kind       text NOT NULL,                    -- noise / triage_keep / trigger
    dedupe_key      text NOT NULL,
    proposed_rule   jsonb NOT NULL,
    false_suppression_count int,                      -- safety gate (point 16)
    purity          numeric(5,4),
    status          text NOT NULL DEFAULT 'proposed'
                      CHECK (status IN ('proposed','accepted','dismissed','expired')),
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX uq_suggestion_dedup
    ON learning_suggestions(channel_id, dedupe_key) WHERE status = 'proposed';


-- =============================================================================
-- K · ROW-LEVEL SECURITY (point 5) — pattern, applied per tenant table
-- =============================================================================
-- App sets per-request: SET app.current_subscriber = '<uuid>'  (and/or provider).
-- Admin/break-glass uses a privileged role that bypasses (BYPASSRLS) or a policy
-- that checks an admin grant. Illustrated on two tables; replicate the pattern.

ALTER TABLE subscriber_executions ENABLE ROW LEVEL SECURITY;
CREATE POLICY exec_tenant_isolation ON subscriber_executions
    USING (subscriber_id = current_setting('app.current_subscriber', true)::uuid);

ALTER TABLE positions ENABLE ROW LEVEL SECURITY;
CREATE POLICY pos_tenant_isolation ON positions
    USING (subscriber_id = current_setting('app.current_subscriber', true)::uuid);

ALTER TABLE subscriptions ENABLE ROW LEVEL SECURITY;
CREATE POLICY sub_tenant_isolation ON subscriptions
    USING (subscriber_id = current_setting('app.current_subscriber', true)::uuid);

ALTER TABLE execution_accounts ENABLE ROW LEVEL SECURITY;
CREATE POLICY execacct_tenant_isolation ON execution_accounts
    USING (subscriber_id = current_setting('app.current_subscriber', true)::uuid);

-- Provider-scoped tables (channels, payouts) get analogous policies keyed on
-- current_setting('app.current_provider'). signal_actions is visible to the
-- channel's provider (channel→provider join) AND to subscribers of that channel
-- (via subscriptions) — enforced in the service layer + a policy using a security-
-- definer helper; kept out of this stub for brevity.

-- =============================================================================
-- NOTE on transition enforcement (point 6): the CHECK constraints above validate
-- the ENUM VALUE only. Legal transitions (pending→sent→claimed→…) are enforced in
-- the application state-machine layer, NOT DB triggers — one source of truth,
-- easier to audit/migrate.
-- =============================================================================
