# 06 — Web App

Single Next.js 15 app (app router) at `apps/web/` serving:
- Subscriber surface at `/(subscriber)/*`
- Admin surface at `/(admin)/*` (gated by Clerk role claim `admin`)
- Marketing surface at `/(marketing)/*` (statically rendered, no auth)
- EA-facing API is `apps/ea-api/` (FastAPI Python) — separate codebase; this doc covers the Node-side API only.

Component library: shadcn/ui + Tailwind v4 + Radix Primitives + lucide-react. Charts: Recharts for P&L, tremor.so blocks for dashboard cards. Tables: TanStack Table.

Design tokens: defined in `packages/ui/tokens.css` as CSS custom properties. Dark mode default (fintech trader audience). Per `~/.claude/rules/web/coding-style.md`, animation-only properties (transform, opacity).

State: TanStack Query for server state + URL params for filter/page/tab state + Zustand for ephemeral UI (sidebar collapse, theme override). No client-side duplication of server data.

Real-time: **Server-Sent Events** for the trade feed and the live dashboard (one-way push from server; minimal infra). Long-poll fallback. WebSockets only for the admin pipeline-live view where we need bidirectional control. Polling for everything billing-related (10s).

---

## Subscriber routes

```
/                                  marketing landing
/pricing                           tier comparison
/risk-disclosure                   required pre-signup view
/terms, /privacy, /aml             policy docs
/signup                            Clerk-hosted email/social
/signin
/onboarding                        first-run flow (jurisdiction → plan → broker → risk profile)
/dashboard                         live P&L + open positions + recent actions (SSE)
/trades                            history table, filterable (date, status, broker, action_type)
/trades/[id]                       drill-down: signal text, pipeline decision, execution detail, broker leg snapshot
/channels                          which signal providers I subscribe to (v1: one)
/brokers                           list of my broker connections
/brokers/new                       guided EA install flow (download .ex5, copy token, paste inputs)
/brokers/[id]                      connection detail; rotate token; pause; delete
/brokers/[id]/risk                 risk profile editor
/notifications                     channel preferences (email / telegram / inapp / webhook)
/notifications/telegram            deep-link to opt-in bot for Telegram DMs
/billing                           current plan, invoices, payment method, cancel
/billing/upgrade                   plan switcher
/settings                          profile, password, MFA, sessions, language, timezone
/settings/security                 active sessions, login history, GDPR export, delete account
/support                           knowledge base + contact form
```

---

## Admin routes (role=admin or support)

```
/admin                             health overview + alerts feed
/admin/subscribers                 search + filters; CSV export
/admin/subscribers/[id]            detail with tabs: profile, broker connections, trade history, billing, audit, impersonate
/admin/subscribers/[id]/impersonate   one-click read-only impersonation; audit-logged
/admin/providers                   signal-provider list
/admin/providers/[id]              channel detail; halt switch; profile editor
/admin/providers/[id]/triggers     curated-trigger editor (port of PySide6 Triggers tab)
/admin/providers/[id]/unmatched    unmatched-messages queue
/admin/providers/[id]/journal      messages + actions timeline (port of GUI Journal)
/admin/providers/[id]/replay       replay one historical message through orchestrator
/admin/providers/[id]/pipeline     live pipeline stream (WebSocket) — see each message advance through stages
/admin/providers/[id]/profile      profile JSON editor (versioned, requires "publish" step)
/admin/providers/[id]/prompts      view current SYSTEM_PROMPT (read-only) and last 50 LLM calls
/admin/providers/[id]/cost         per-provider LLM spend chart
/admin/system/health               services health, EA fleet status, broker outage detection
/admin/system/incidents            incident timeline; create/update post-mortem
/admin/system/feature-flags        runtime flags
/admin/system/config               system_config table editor (gated; audit-logged)
/admin/billing                     revenue dashboard, MRR, churn, failed payments queue
/admin/billing/refunds             issue refund flow
/admin/audit                       audit log search (settings_history, admin_actions, impersonation_log)
```

---

## API surface — tRPC routers (Node) + REST internal endpoints (Python ↔ Node)

The web app exposes one tRPC router per concern. tRPC inputs/outputs are zod schemas in `packages/core/`. All inputs validated; all outputs typed.

### `subscribers` router
```ts
subscribers.me.get()                                                      → SubscriberMe
subscribers.me.update(input: { fullName?, locale?, country? })           → SubscriberMe
subscribers.me.pause(input: { reason?: string })                         → SubscriberMe
subscribers.me.resume()                                                   → SubscriberMe
subscribers.me.requestExport()                                            → { exportId: string }   // async — pg-boss job emails link
subscribers.me.requestDelete()                                            → { confirmationToken }
subscribers.me.confirmDelete(input: { confirmationToken })                → void
```

### `brokers` router
```ts
brokers.list()                                                            → BrokerConnection[]
brokers.create(input: { displayName, executionBackend: 'ea' })           → { connection, rawBearerToken }   // token shown ONCE
brokers.get(input: { id })                                                → BrokerConnection
brokers.rotateToken(input: { id })                                        → { rawBearerToken }
brokers.revoke(input: { id })                                             → BrokerConnection
brokers.update(input: { id, displayName })                                → BrokerConnection
brokers.eaHealth(input: { id })                                           → { lastSeenAt, version, mt5Build, brokerCompany }
```

### `risk` router
```ts
risk.get(input: { brokerConnectionId })                                   → RiskProfile
risk.update(input: { brokerConnectionId, patch: Partial<RiskProfile> })   → RiskProfile
                                                                            // increments broker_connections.config_version
                                                                            // bumps via Postgres trigger; EA picks up on next poll
```

### `signals` router (subscriber-facing)
```ts
signals.providers.list()                                                  → SignalProvider[]
signals.subscriptions.list()                                              → SubscriberChannelSubscription[]
signals.subscriptions.toggle(input: { providerId, active: boolean })      → SubscriberChannelSubscription
```

### `trades` router
```ts
trades.list(input: PaginatedFilter<{ status?, dateFrom?, dateTo?, brokerId?, actionType? }>)
                                                                          → Paginated<SubscriberActionRow>
trades.get(input: { id })                                                 → TradeDetail
                                                                            // joins subscriber_actions + signal_actions + messages + positions
trades.positionsOpen()                                                    → Position[]
trades.summary(input: { dateFrom, dateTo })                               → { realizedPnlCents, count, winRate, ... }
```

### `notifications` router
```ts
notifications.preferences.get()                                           → NotificationPrefs
notifications.preferences.update(input: { ... })                          → NotificationPrefs
notifications.telegram.startLink()                                        → { deepLinkUrl, code }       // user clicks link, bot DMs them, code-confirms
notifications.list(input: PaginatedFilter)                                → Paginated<Notification>
notifications.markRead(input: { ids: number[] })                          → void
notifications.unreadCount()                                               → number
```

### `billing` router
```ts
billing.subscription.get()                                                → Subscription | null
billing.subscription.changePlan(input: { planId })                        → { paddleCheckoutUrl }
billing.subscription.cancel(input: { reason })                            → Subscription
billing.subscription.resume()                                             → Subscription
billing.invoices.list(input: PaginatedFilter)                             → Paginated<Invoice>
billing.paymentMethods.list()                                             → PaymentMethod[]
billing.paymentMethods.updateDefault(input: { id })                       → void
```

### Admin routers
```ts
admin.subscribers.search(input: { q?, status?, ... })                     → Paginated<SubscriberSummary>
admin.subscribers.get(input: { id })                                      → AdminSubscriberDetail
admin.subscribers.pause(input: { id, reason })                            → Subscriber
admin.subscribers.refund(input: { id, invoiceId, amountCents, reason })   → Invoice
admin.subscribers.impersonate.start(input: { id, mode, reason })          → { impersonationToken, expiresAt }
admin.subscribers.impersonate.end(input: { impersonationId })             → void

admin.providers.list()                                                    → SignalProvider[]
admin.providers.halt(input: { id, reason })                               → SignalProvider
admin.providers.resume(input: { id })                                     → SignalProvider
admin.providers.profile.publish(input: { id, profileJson })               → { newVersion }
admin.providers.triggers.list(input: { providerId })                      → Trigger[]
admin.providers.triggers.upsert(input: TriggerInput)                      → Trigger
admin.providers.unmatched.list(input: { providerId })                     → UnmatchedMessage[]
admin.providers.unmatched.resolve(input: { id, resolution, asTrigger? }) → void
admin.providers.replay(input: { messageId })                              → { signalActionsCreated }

admin.system.health()                                                     → SystemHealth
admin.system.incidents.create(input: { title, severity, services[] })     → Incident
admin.audit.search(input: { kind, scopeId?, dateFrom, dateTo })           → Paginated<AuditEntry>
```

### Internal REST (Python ↔ Node)

Python `signal-svc` writes directly to Postgres for performance; it also calls these Node endpoints for cross-domain notifications:

```
POST /internal/v1/notify              { subscriberId | providerId, eventType, payload }
POST /internal/v1/cost-guard/trip     { providerId, reason }              // server marks provider.halted
GET  /internal/v1/subscribers/eligible-for-provider/{providerId}          // optional optimization
```

Node `web` calls Python `signal-svc` for:
```
POST /internal/v1/replay              { messageId }                       // operator-initiated replay
POST /internal/v1/profile/preview     { providerId, profileJson, sampleMessages: [] }
GET  /internal/v1/health
```

mTLS between containers via ECS service mesh + private CA. No internet exposure for internal endpoints.

---

## Real-time strategy per screen

| Screen | Channel | Why |
|---|---|---|
| `/dashboard` (open positions, recent trades) | SSE — `/api/sse/dashboard` | One-way push, low frequency (~once per signal), simple |
| `/trades` (history table) | TanStack Query refetch on focus + invalidation via SSE control channel | Browse-mode; don't burn connections |
| `/notifications` (inbox) | SSE on `unreadCount` only; manual refresh on the list view | Notification badge needs liveness; list does not |
| `/admin/providers/[id]/pipeline` (live messages flow) | WebSocket via `/api/ws/admin/pipeline` | Bidirectional (admin can pause/inspect); high frequency |
| `/admin/system/health` | SSE | One-way |
| `/admin/subscribers/[id]/trades` | SSE filtered to that subscriber | Same as dashboard, scoped |
| `/billing` | Polling 30s | State changes are rare; not worth a connection |

---

## Auth / impersonation

Clerk session JWT in every tRPC call. tRPC middleware:
1. Verify Clerk token, hydrate `ctx.user`.
2. If `ctx.user.role === 'subscriber'`: load subscriber → `SET LOCAL app.subscriber_id` for the request transaction.
3. If admin + impersonation header `X-Impersonate-Subscriber: <id>` is set AND a valid `impersonation_token` cookie exists: log row to `audit.impersonation_log` (or update existing in-flight row's `read_paths`); set the impersonation context as `app.subscriber_id` but ALSO keep `app.role='admin_impersonating'` so write paths can guard against `mode='read_only'`.
4. Admin writes outside impersonation → log to `audit.admin_actions`.

Impersonation tokens expire after 30 min; auto-renew while active; explicit "End impersonation" button visible at all times in a top banner.

---

## Component design notes

- Onboarding is a 4-step wizard rendered as one route with URL-state-driven steps. Each step writes through; no "save at the end" — if the subscriber drops off, we can resume.
- Broker connection creation shows the raw token on the SAME page that confirms creation, with a "copy token" button + "I copied it" confirmation that disables the display. Never store the raw token anywhere it can be re-displayed.
- The risk profile editor uses a 2-column layout: settings on the left, "your last 10 trades simulated with this profile" preview on the right (read from `subscriber_actions` + replay through `riskCheck` server-side). Defaults match the EA's defaults so no migration shock.
- Trade detail timeline: vertical timeline component, rows = pipeline stage decisions + each `subscriber_actions` lifecycle transition + each `positions` event. Same data the admin replay tool consumes.
- The "Download EA" button on `/brokers/new` returns a personalized ZIP containing `CopyTrades.ex5`, a `README.txt` with the subscriber's `connection_id`, `ApiBaseUrl`, `ApiCertSha256`, and a placeholder `ApiBearerToken=<paste>` line.

---

## Forms, validation, errors

- Every form uses React Hook Form + zod resolver. Same zod schemas as tRPC inputs — defined once in `packages/core/`.
- Server errors surface as Sonner toasts; field errors inline.
- 401 → redirect to Clerk sign-in with `redirect_url`. 403 (RLS or role) → "Permission denied" page; do NOT expose what they tried to access.

---

## Accessibility

Per `~/.claude/rules/web/coding-style.md`: semantic HTML first, keyboard navigation, focus management on route change, ARIA labels on icon-only buttons, color contrast verified for dark+light. We ship one theme (dark) at v1 — light theme post-launch.

## Internationalization

V1: English + Arabic (right-to-left). The channel is Arabic; subscribers will land via Arabic marketing. Use `next-intl`. Date/number formatting via `Intl.*`. Currency from the subscriber's preference, default USD. Arabic font: Cairo or IBM Plex Arabic.
