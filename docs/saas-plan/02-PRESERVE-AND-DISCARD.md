# 02 — Preserve and Discard

Source-of-truth catalog: every meaningful file in the current repo, classified as **preserve verbatim**, **preserve-as-pattern**, or **discard**. New-repo destinations assume the monorepo layout in §10-INFRASTRUCTURE.md.

New repo layout (target):

```
copytrades-saas/
├── apps/
│   ├── web/                 # Next.js subscriber + admin app (TS)
│   ├── signal-svc/          # FastAPI + listener + orchestrator (Python)
│   ├── ea-api/              # FastAPI EA-facing API (Python)
│   └── worker/              # pg-boss workers (TS)
├── packages/
│   ├── db/                  # Drizzle schema + migrations + clients (TS)
│   ├── ui/                  # shadcn/ui-derived shared components (TS)
│   ├── core/                # shared TS domain types (zod schemas)
│   └── py-shared/           # shared Python: validators, llm provider, secret box, profile context
├── ea-v2/                   # MQL5 sources
├── infra/
│   ├── terraform/
│   └── docker/
└── docs/
```

---

## Preserve verbatim — port with cosmetic-only changes

These files contain irreplaceable IP and should land in the new repo unchanged except for import paths, config-source swap (DB row → env/vault), and removing Windows-bound bits.

| Current file | New path | Justification |
|---|---|---|
| `src/ai.py` (`_TEMPLATE` + `AIClient`) | `apps/signal-svc/src/ai.py` | The ~11 KB interpreter system prompt is the moat. Touch only when adding a new channel. |
| `src/ai_triage.py` | `apps/signal-svc/src/ai_triage.py` | Triage gate is a cost-control mechanism we've tuned for this channel. |
| `src/validators.py` | `apps/signal-svc/src/validators.py` (mirror schemas in `packages/core/actions.ts` via zod for the Node layer) | 16 action types; Pydantic models with field constraints. Source of truth. |
| `src/orchestrator.py` | `apps/signal-svc/src/orchestrator.py` | Stage cascade is the pipeline shape we ship. |
| `src/listener.py` | `apps/signal-svc/src/listener.py` | Telethon backfill + supervisor + heartbeat is hard-won. |
| `src/state_summary.py` | `apps/signal-svc/src/state_summary.py` | Renders SYSTEM STATE block to the prompt — coupled to ai.py. |
| `src/signal_memory.py` | `apps/signal-svc/src/signal_memory.py` | Channel-summary buffer drives prompt context. |
| `src/fingerprint.py` | `apps/signal-svc/src/fingerprint.py` | OPEN-dedup. Bug-for-bug compatible. |
| `src/prefilter.py` | `apps/signal-svc/src/prefilter.py` | Deterministic gate. |
| `src/trigger_matcher.py` | `apps/signal-svc/src/trigger_matcher.py` | Embedding cache + curated triggers. |
| `src/profile_context.py` | `apps/signal-svc/src/profile_context.py` | Profile JSON loader + mtime cache. |
| `src/llm_provider.py` | `apps/signal-svc/src/llm_provider.py` | Anthropic/OpenAI abstraction. |
| `src/cost_guard.py` | `apps/signal-svc/src/cost_guard.py` (extend to per-tenant budgets) | Daily-budget loop logic; minor refactor for multi-tenant accounting. |
| `src/ai_evaluator.py` + `src/evaluator/` | `apps/signal-svc/src/evaluator/` | Async directional scorer; informational only. |
| `src/ai_logger.py` | `apps/signal-svc/src/ai_logger.py` (output target → S3 + Postgres `ai_calls` table; drop JSONL writer) | Schema preserved; sink swapped. |
| `src/feeds/*` | `apps/signal-svc/src/feeds/` | macro/cot/etf_flows/news_scan/calendar feed loops. |
| `channels/Forex Engineer.json` | `apps/signal-svc/channels/forex-engineer.json` | Vocabulary table + worked examples. |
| `src/api_models.py` (EA-facing models) | `apps/ea-api/src/api_models.py` | Pydantic models guarding EA POST bodies. |
| `ea/CopyTrades.mq5` (logic) | `ea-v2/CopyTrades.mq5` (transport rewritten — see §4-EXECUTION-LAYER.md) | ManagePlans, ExecuteOne, ReconcileClosedPositions, dedup-by-action-id (line 1461), retry queue. Logic survives; loopback HTTP → mTLS to cloud. |
| `ea/Dashboard.mqh` | `ea-v2/Dashboard.mqh` | Canvas dashboard with hash-gated repaint. |
| `ea/BrokerCheck.mqh` | `ea-v2/BrokerCheck.mqh` | Startup capability checks. |
| `tests/test_management_replay.py` | `apps/signal-svc/tests/test_management_replay.py` | Prompt-drift safety net. The 7 management types live or die by this. |
| `tests/test_replay.py` | `apps/signal-svc/tests/test_replay.py` | OPEN-fixture replay against real LLM. |
| `tests/fixtures/management_messages.jsonl` + `fixtures/messages.jsonl` | `apps/signal-svc/tests/fixtures/` | Worked-example corpus. |
| `src/promoter.py` (the three sweepers) | `apps/worker/src/sweepers/` ported to TS, OR keep Python and run as a small periodic Python job | Three sweepers (promote_due, release_stale_claims, expire_stale_watches) — see §4. Recommend porting to TS to keep all schedulers in one place. |
| `src/schema.sql` (as reference) | Consumed when designing `packages/db/schema.ts` — see §3-DATA-MODEL | Source of truth for invariants. |

---

## Preserve as pattern — reimplement in the new stack

Architectural patterns that survive; their current code does not.

| Pattern | Today | New implementation |
|---|---|---|
| Action lifecycle state machine | SQLite CHECK constraint + atomic `UPDATE ... WHERE status=?` | Postgres CHECK constraint + Drizzle queries; status-guarded UPDATE preserved in `apps/ea-api/src/actions.py` and `packages/db/src/actions.ts`. The REVIEW.md P0 fix (`post_result` rejects unless `status IN ('claimed','watching')`) is reproduced verbatim. |
| Status-guarded post_result | `src/api.py:452` | `apps/ea-api/src/routes/actions.py`. Test coverage required from day one. |
| Reconciliation pattern (DB-authoritative) | `ReconcileClosedPositions()` in EA polls `/positions?status=open` | EA v2 keeps the same call shape against the cloud API. The 48h history scan stays in MQL5. |
| Fail-loud, recover-locally | NSSM restarts on supervisor death; visible-dead beats silent-dead | ECS task definition with health check + auto-restart; explicit hard-exit on supervised-task failure preserved. |
| Audit chain (messages → actions → positions) | FK joins in SQLite | Same FK shape in Postgres; tenant-scoped + RLS (§3-DATA-MODEL). |
| Cost guard kill-switch | Per-stack daily LLM budget loop | Per-tenant (signal-provider) budget loop in `signal-svc`; per-subscriber budget tracked separately for premium features. |
| Retry queue (idempotent) | EA writes failed POSTs to `MQL5\Files\` | Preserved verbatim in EA v2; on-cloud-side idempotency handled via `Idempotency-Key` headers on every EA POST. |
| Per-tick supervisor with hard-exit | bot.py `_supervise` | All worker tasks in `apps/worker/` use the same supervisor: any unhandled exception → process exit 1 → ECS restarts. |
| 422 forensic logger | Persists raw body for postmortem | Preserved; sink → `audit.api_errors` Postgres table + Sentry breadcrumb. |
| Idempotent migrations | 17 functions in `src/db.py:init_schema` | Drizzle migrations + `IF NOT EXISTS`/`IF EXISTS` everywhere; pre-deploy migration gate. |
| Channel profile JSON (vocab + examples) | `channels/<name>.json` | Same JSON schema; per-provider versioned in Postgres `signal_providers.profile_json` and shipped to file on signal-svc container startup. |
| Per-stage decision write-back to messages | `messages.decided_stage/_outcome/_at` columns | Preserved on `messages` table in Postgres. Powers admin "why didn't I get this signal" view. |
| Unmatched-messages curation backlog | `unmatched_messages` + GUI Triggers tab | `audit.unmatched_messages` table + admin curation UI. |
| Per-tenant kill switch | `settings.kill_switch=on` | `signal_providers.halted` AND `subscribers.paused_at` — two independent kill switches. |
| Per-signal heartbeat (market price) | `POST /market/price` every 15s; STALE > 60s | Same shape against `ea-api`; freshness drives prompt STALE marker. |
| Compound action emission | One LLM call → multiple action rows in order | Preserved; the fan-out worker iterates compound results per-subscriber. |

---

## Discard

These do not survive.

| File / module | Why |
|---|---|
| `src/api.py` (as-is) | Loopback-HTTP-only design with `EA_SHARED_TOKEN` single shared secret. Logic moves to `apps/ea-api` (per-EA bearer) and `apps/web/api` (subscriber-facing). |
| `src/bot.py` (as a Telegram-bot-as-control-plane) | The operator's Telegram bot IS the control plane today. In the SaaS it becomes only a notification surface (subscribers opt-in). Admin commands move to the web admin UI. The promoter/sweeper/feed loops migrate to `apps/worker`. |
| `src/config.py` (env+settings hybrid) | Replaced by Doppler-style env injection via Secrets Manager and per-tenant config in Postgres. |
| `src/config_v2.py` | Operator-side "Stack/Account/Profile/Channel/Destination/Bot/Route/BotBinding" entity model. Useful as a thinking aid; the real subscriber-aware model is in §3-DATA-MODEL. Discard the file; concepts replaced. |
| `src/secret_box.py` (DPAPI) | Windows-bound. Replaced by KMS envelope encryption (§9 of 01). |
| `src/db.py` (SQLite + 17 migrations) | SQLite dies in production. Migrations rewritten as Drizzle. |
| `src/schema.sql` | Reference only; replaced by `packages/db/schema.ts`. |
| `src/db_settings.py` | The `settings` table is a key→value bag. Replaced by typed config tables per concern (per-tenant settings, per-subscriber risk_profile, system_config). |
| `src/gui/**` (~50 modules: PySide6) | The entire desktop GUI is operator-only. Replaced by the Next.js admin app (§6-WEB-APP.md). Patterns survive (Triggers curation, Replay, Pipeline view, Cost view) — they become admin pages. |
| `gui_launcher.py` | Replaced by web. |
| `CopyTrades.spec` (PyInstaller) | No bundled .exe ships. |
| `services/install_services.bat`, `install_services.bat`, `services/nssm_client.py` (in gui), all NSSM-related | Linux containers. NSSM is gone. |
| `launch.bat`, `setup.bat`, `scripts/sign_exe.bat`, `scripts/run_*.bat` | Replaced by Dockerfiles + GitHub Actions CI. |
| `src/gui/services/backup_io.py` | Replaced by RDS automated PITR + nightly logical dumps to S3. |
| `dist/` and Inno Setup installer | No distributed binary. |
| `src/notification_dispatcher.py` (legacy poller path) | Replaced by pg-boss jobs + Resend (email) + Telegram bot fan-out worker. |
| `src/bot_outbox_tailer.py` | Replaced by `notifications` table + worker. |
| `gui_launcher.py` setup wizard, telegram_wizard, profile_wizard | The "owner pastes Telethon API ID and phone" wizard moves to admin web app for the operator's one-time channel onboarding; subscribers never see it. |
| `src/migrations/config_v1_to_v2.py` | One-time data migration (SQLite→Postgres) replaces this — §3-DATA-MODEL. |
| `tests/test_integration.py` (NSSM/GUI parts) | Replaced; the orchestrator + API integration tests survive in renamed form. |
| Any "stack switcher" UI concept | Subscribers never see stacks. Admins see signal-providers. The mental model changes. |
| `_owner_only` check | Replaced by RBAC (`role` column on `subscribers`/`users`, plus Clerk roles claim). |
| `EA_SHARED_TOKEN` single-secret model | Per-EA bearer tokens, rotatable, scoped to one `broker_connection`. |
| `127.0.0.1:8765` loopback transport | mTLS HTTPS to `ea-api.example.com` (cert pinning in EA v2). |
| `src/api.py` 422-handler that writes to a local file | Preserved as a pattern but writes to `audit.api_errors` table + Sentry. |

---

## Things in source comments that turned out to matter

The 2026-05-27 incident referenced at `ea/CopyTrades.mq5:1461` (dedup-by-action-id in `g_pending_orders[]`) MUST be in the EA v2 cutover regression suite. Same with the `RegisterPlan` dedup-by-ticket fix and the status-guarded `post_result` (REVIEW.md P0). These are the three "you'll see them again on day-1 of fan-out if you don't carry the fix" bugs.
