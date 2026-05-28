# 06 — Operational Posture

**Summary.** The system runs on **one Windows machine, owned by one operator**. There is no remote monitoring, no centralized alerting, no remote management plane. Health is observed via the operator's PySide6 GUI (services-bar pills, log tailer), Telegram DMs from the bot, and Windows Event Viewer / NSSM logs. Single point of failure everywhere: the OS, the MT5 terminal, the network, the SQLite file, the Telegram session, the AI provider account. End-to-end latency from Telegram receipt to MT5 OrderSend is roughly 2–10 seconds in the typical path (LLM call dominates).

## How it is deployed today

**Local-machine-only.** The README documents the three-terminal-window dev mode (`launch.bat`) and the production-ish NSSM install (`services/install_services.bat`). The install script encodes a two-stack layout (`CT-SMC-*` + `CT-AR-*`) with hardcoded absolute paths to one Windows user (`C:\Users\Administrator\...`). There is no parametrization, no ansible/puppet/chef, no Docker image, no Kubernetes manifest. Cloud deployment is not attempted in any code path.

The GUI installer (`CopyTrades-Setup.exe`, built by Inno Setup) bundles the Python services as a PyInstaller one-folder app and lets the operator install via standard Windows installer flow. The GUI's services-bar can install / start / stop / uninstall NSSM services on behalf of the operator (`src/gui/services/nssm_client.py`). Operator must run elevated.

## Monitoring and alerting (what exists)

**There is no remote monitoring.** Observability is local-only:

| Surface | What it shows | When operator sees it |
|---|---|---|
| Telegram bot DMs | Every terminal action (executed / failed / rejected / watching), position closes, ALERTs, recovery prompts on restart | Real-time push to the operator's phone |
| EA's on-chart dashboard | Daily stats (signals, executed, rejected, chased), open positions, signal-quality eval, broker checks, market price freshness | When operator looks at the MT5 chart |
| GUI services-bar | Health pills for each service (api/bot/listener) + Snapshot freshness | When operator opens the GUI |
| GUI Live view | Open positions, recent actions, equity, oldest claim age | When operator opens the GUI |
| Crash banner | Persistent banner if any service stopped abnormally (read from NSSM exit codes) | When operator opens the GUI |
| logs/* on disk | Forensic detail | grep |
| MT5 terminal's own Journal | Broker-side trace | When operator clicks Journal tab in MT5 |

**No alerting** to operator outside of Telegram DMs:

- The bot's DM channel is the only "alert pipe". If Telegram bot polling itself breaks, **no alarm fires**. The `telegram_heartbeat_loop` writes `bot_telegram_ok_at` every 30 s; the GUI services-bar reads it; nothing else does.
- If the operator's Telegram bot account is silenced/muted, alerts are invisible.
- The bot doesn't escalate (no SMS, no email, no PagerDuty, no on-call rotation).

**Cost guard** (`src/cost_guard.py`): the only automated runtime cap. Sums today's LLM spend from `logs/ai_calls.jsonl` once per minute; if `total > cost_daily_budget_usd × cost_cap_multiplier`, flips `kill_switch=on` with `kill_switch_reason="cost_guard"` and DMs the operator. Resets at UTC midnight or when operator clicks RESUME. Step 18 wires per-route budgets; current scope (1:1) means it's effectively a global cap.

## Error-handling philosophy as evidenced by the code

The codebase has a clear "fail-loud, recover-locally" pattern:

1. **Every long-running loop has `try/except Exception` per tick.** Each loop logs at WARNING/ERROR and continues (`src/bot.py`, `src/listener.py`, `src/bot_loops/*`).
2. **`_supervise` hard-exits the bot process** if a supervised loop dies entirely (BaseException escape). The expectation is that NSSM restarts the service — visible-dead beats silent-dead (`src/bot.py:302`).
3. **AI provider failures persist as `ALERT` actions** rather than crashing the orchestrator (`src/orchestrator.py:570`).
4. **Schema migrations are idempotent**: every migration function gates on `PRAGMA table_info` or `sql LIKE` (`src/db.py`).
5. **Tests don't mock the DB**: integration tests run against real SQLite to catch schema bugs end-to-end (CLAUDE.md, `tests/test_integration.py`).
6. **Defensive guards have explicit reason strings** that show up in `ea_response` / `trades.log`: `duplicate_signal`, `no_open_position`, `already_open`, `last_closed_unparseable`, `mt5_not_found`, `backfill_management_review_required`, `claim_expired`, `instant_open_disabled`, `noop_partial_and_be_disabled`, `pending_order_ticket=...`, `cancelled_by_channel`, `sl_too_wide_for_max_risk_pct`.

The `REVIEW.md` audit is itself part of the operational posture: it lists known-but-unfixed issues by severity (P0/P1/P2/P3). Several P0/P1 items called out there appear to have been fixed since (e.g., status-guarded `post_result`, ALERT terminal-row policy).

## End-to-end latency

Measured at the time-marks in the code:

| Hop | Typical latency | Source |
|---|---|---|
| Telegram → listener handler fires | <1 s for live, batched for backfill | Telethon NewMessage event |
| Listener → API `/incoming_message` (v2) or in-proc orchestrator (v1) | <100 ms | urllib timeout=10 s |
| Prefilter + matcher (Stage 0+1) | <50 ms on a typical message; embedding match adds 500–1500 ms when Layer-1 misses | `src/trigger_matcher.py` |
| Triage LLM call (Stage 2) | 500–2000 ms (Haiku / gpt-5-nano) | `src/ai_triage.py` |
| Interpreter LLM call (Stage 3) | 1500–8000 ms (Sonnet 4.6 + extended thinking budget 4000 tokens) | `src/ai.py` |
| Action INSERT + promoter sweep | 0–1000 ms (promoter polls at 1 Hz, default `execute_after=now+0s` so it's the very next tick) | `src/promoter.py` |
| EA polling latency | 0–1000 ms (1 Hz `OnTimer`) | EA |
| EA claim + OrderSend + result POST | 200–1500 ms (broker-dependent) | EA |

**Best case**: ~2.0 s (matcher hits on a CLOSE_FULL phrase + promoter+EA at 0 s/0 s with a fast broker).

**Typical case**: ~4–7 s (interpreter runs).

**Worst case**: 10+ s when the interpreter's extended thinking budget is fully consumed and the broker is slow. No SLO is specified anywhere.

These numbers are **estimated from code timeouts and typical LLM behavior**, not measured. The only persisted latency observation is `latency_ms` in `logs/ai_calls.jsonl`.

## Single points of failure

In approximate "how often this bites in practice" order:

1. **The single SQLite file.** Three Python processes + GUI all hold connections. Corruption from a hard power-off (no WAL checkpoint) loses recent state. No documented backup schedule. GUI has `src/gui/services/backup_io.py` but no enforced cadence.
2. **The Telethon user-account session.** If Telegram revokes it (from another device, expired, rate-limit) the listener exits and the operator must re-run the wizard to log in (`src/listener.py:514`). No automated rotation.
3. **The MT5 terminal process.** No watchdog beyond MT5's own restart-EA-on-attach behavior. If MT5 crashes, the EA is gone until manual restart. The DB shows positions as "open" while MT5 is down; reconciliation runs only when the EA is up.
4. **The single Anthropic / OpenAI API key.** Rate limit or billing hold = listener falls back to ALERT, all signals queue as ALERTs.
5. **The single owner Telegram user ID.** Lose the phone → cannot acknowledge or cancel actions; the bot polls indefinitely.
6. **The single shared secret (`EA_SHARED_TOKEN`).** No rotation flow.
7. **The single channel.** Profile JSON is tuned to one signal source.
8. **Windows OS** + DPAPI. Reinstalling Windows or moving to a new machine breaks all encrypted secrets (DPAPI-protected with `CRYPTPROTECT_LOCAL_MACHINE` — not portable). The operator must re-enter every secret in the new install.

## Security posture

**Secrets at rest**:
- `src/secret_box.py` wraps Windows DPAPI (CryptProtectData / CryptUnprotectData) with `CRYPTPROTECT_LOCAL_MACHINE`. Plaintext in → base64 ciphertext out → stored in `settings` TEXT column.
- `SECRET_KEYS` enumerates which `settings` keys are auto-encrypted (`src/db_settings.py:28`): `tg_api_hash`, `tg_bot_token`, `tg_session_blob`, `anthropic_api_key`, `openai_api_key`, `ea_shared_token`.
- DPAPI tradeoff (commented at `src/secret_box.py:3`): any administrator on this machine can decrypt. Acceptable because admin already owns the user account.
- **No HSM, no key vault, no rotation policy, no audit log of secret access.**

**Secrets in transit**:
- All inter-process traffic is over `http://127.0.0.1:8765` — loopback only. The API does NOT terminate TLS. `_enforce_auth_bind_policy` warns (but doesn't refuse) if the API binds non-loopback without `EA_SHARED_TOKEN` set.
- LLM SDK calls go over the SDK's HTTPS to provider endpoints.
- Telegram MTProto: Telethon's own encryption layer.

**Authentication**:
- API: single static shared-secret header. No rotation, no per-user keys, no scoped tokens. Set the same value in the EA's `ApiSharedToken` input.
- Bot: `_owner_only(user_id)` checks `TG_BOT_OWNER_USER_ID`. Hardcoded single user.
- GUI: no authentication — the GUI runs as the logged-in Windows user and trusts the OS session.

**Authorization**: there is no concept of multiple roles. There is one owner.

**Channel credential storage**:
- Telegram channel watched is identified by numeric `tg_watched_chat_id` in settings. No password.
- Broker credentials live in the MT5 terminal, not in this codebase. Trust boundary is the MT5 terminal.

**Inputs validated**:
- All EA → API bodies are Pydantic models in `src/api_models.py`. The 422 handler persists the raw body to `logs/api.log` for forensic.
- LLM raw output is parsed defensively (markdown fences tolerated, `try/except JSONDecodeError`, unknown action types rejected, `src/validators.py:357`).
- **Prompt injection is handled by the prompt itself** (the `UNTRUSTED INPUT POLICY` block). No regression tests for the defense path.
- Telegram inbound text is untrusted but only reaches the AI as data inside delimited blocks.

**Logging hygiene**:
- API keys never logged in plaintext. `logs/ai_calls.jsonl` stores token counts and model name, not the full prompt body in plaintext beyond what the LLM emitted as `raw_response`.
- Telethon's API hash is encrypted; phone number IS stored in plaintext in `stacks_config.json` (`src/config_v2.py:Account.phone` docstring acknowledges this PII concern — full encryption is in `docs/plans/sunset-list.md`).

**Network exposure**:
- API binds `127.0.0.1` by default. Operator can change to `0.0.0.0` via Settings → `api_host` — `_enforce_auth_bind_policy` blocks that unless a non-blank `EA_SHARED_TOKEN` is set.

**No code signing of the EA**. `ea/CopyTrades.ex5` is a compiled MQL5 binary; broker terminals will run any `.ex5` from `MQL5/Experts/`. No tamper detection on the EA. `scripts/sign_exe.bat` exists for the GUI installer but its current cert status is unverified in this audit.

## What "running healthy" looks like

The operator's mental model (reconstructed from GUI services-bar and dashboard code):

- All three NSSM services in SERVICE_RUNNING state.
- `bot_telegram_ok_at` and `listener_telegram_ok_at` fresh within 60 s.
- `market_XAUUSD_at` fresh within 60 s (EA → API heartbeat).
- MT5 chart shows the EA dashboard with green "API OK" and recent action ids.
- `actions` table has no rows stranded in `claimed` longer than 5 minutes.
- `pending` count near zero (everything either executed, rejected, or watching).
- `kill_switch=off`.
- `logs/api_http.log` has zero recent 4xx/5xx entries.
- Daily LLM spend in `logs/ai_calls.jsonl` well below `cost_daily_budget_usd`.

## What "running unhealthy" looks like — and what catches it

- Listener disconnected: `listener_telegram_ok_at` stale → GUI pill turns amber. Bot DMs nothing. **Bot doesn't proactively notify the operator** that the listener is dead.
- Bot polling broken: no command will work. **No automated alarm** — the operator only notices when they try to `/halt` and get no response.
- API down: EA logs "API unreachable" in MT5 Experts log; dashboard shows red. **No DM** — the API is the thing the DM relies on.
- MT5 / EA disconnected from broker: `g_broker_check` updates, dashboard reflects, ALERT is POSTed once at OnInit. **No periodic health POST**.
- Database corruption: the next process to open the DB hits SQLite errors and crashes; NSSM restarts; crash banner appears in GUI. **No backup-restore tool ships with the product.**
- DPAPI failure (new machine): secrets fail to decrypt; setup wizard reopens. Operator must re-enter everything.

## Backups, disaster recovery, secret rotation

**Backups**: There is a `src/gui/services/backup_io.py` and a Backup tab (`src/gui/views/_backup_tab.py`), but it appears to be operator-initiated rather than scheduled. Not opened in this audit; behavior **UNCLEAR** beyond filename suggesting manual zip-and-archive flow.

**Disaster recovery**: There is no documented DR plan. Recovery procedure (inferred): reinstall on a working Windows box, copy `<APPDATA>/CopyTrades/<stack>/copytrades.db` over, re-enter all secrets (because DPAPI ciphertext is machine-bound), re-run Telegram wizard.

**Secret rotation**: nothing automated. To rotate Anthropic key: open GUI Settings → edit `anthropic_api_key`. To rotate EA shared token: edit Settings AND edit the EA's `ApiSharedToken` input on the chart. To rotate Telegram bot token: BotFather → revoke → new token → Settings. To rotate Telethon session: re-run wizard.

**Audit trail**: `logs/trades.log` records every lifecycle event. There is no separate audit log for settings changes or for who/when modified the channel profile (the GUI has the only edit surface; it does not write history rows).
