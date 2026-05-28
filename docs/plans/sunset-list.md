# CopyTrades Sunset List

**Purpose**: track every compatibility shim, legacy code path, and deferred
encryption / cleanup task that exists for back-compat reasons. Each entry
has a clear **retirement trigger** so the work doesn't get forgotten when
adoption catches up.

**Companion to**:
- `docs/plans/2026-05-23-multi-channel-routing.md` — what we BUILT
- `docs/plans/2026-05-23-multi-channel-runbook.md` — how to validate
- **this file** — what we'll REMOVE later

Mirror image of the deferred-features list. Both are gated on real
operational triggers: deferred features build when a customer asks;
sunset items retire when adoption metrics make them safe to remove.

---

## Status legend

| Status | Meaning |
|---|---|
| ☐ **Active** | Compat shim is live. Customers depend on it. |
| ◐ **Aging** | Compat shim is live but its use case has shrunk. Plan retirement. |
| ☑ **Retired** | Already removed. Documented for audit purposes. |

---

## Compat shims to retire

### 1. Single-channel listener delegates to `listener.main()` (Step 5 design)

**Status**: ☐ Active
**Where**: `src/shared_listener.py::main()` — when v2 config has exactly 1 channel under 1 account, dispatches to `_run_legacy_single_account()` which calls the v1 `listener.main()`. The v2 multi-channel runner (`_run_multi_channel`) handles only N≥2.

**Why it exists**: Step 5's plan called for "feature-parity cutover" without rewriting 660+ lines of working code. The legacy `listener.main()` has battle-tested heartbeat / second-pass-catchup / supervisor logic that the new multi-channel runner doesn't replicate yet.

**Cost of keeping**: two code paths to maintain, two log line formats, two startup sequences. Subtle behavior differences possible.

**Retirement trigger**: when the multi-channel runner reaches feature parity (heartbeat + second-pass-catchup + supervisor ported) AND has been validated against multi-channel production for ≥30 days. Then the N=1 case can use the same runner unchanged.

**Plan to retire**:
1. Port `_second_pass_catchup` from `listener.py` to `shared_listener.py`'s multi-channel runner.
2. Port `telegram_heartbeat_loop` similarly.
3. Port supervisor / reconnect loop.
4. Change `shared_listener.main()` to ALWAYS call `_run_multi_channel(account, channels, cfg)` regardless of channel count.
5. Delete `_run_legacy_single_account` and remove the `from src.listener import main` lazy import.
6. Remove `src/listener.py::main()` (keep helpers like `_collect_missed`, `_resolve_sender`, `MissedMessage`, `_ApiDispatchTarget`, `_post_incoming_message`, `replay_missed_messages` as a thin shared module — or move them to `src/listener_helpers.py`).
7. Remove `--service listener` route from `gui_launcher.py`.
8. Update bootstrap_services_install spec hidden imports.

---

### 2. `position_close_notifier` in legacy branch (Day 3 deferred path)

**Status**: ◐ Aging (Day 3 mutex'd it into the no-v2-binding branch)
**Where**: `src/bot.py::post_init()` — runs ONLY when `resolve_bot_id_for_destination(config.DB_PATH)` returns None (i.e. no v2 binding for this destination).

**Why it exists**: legacy single-stack deployments without a v2 config still need close DMs. Day 3 routed v2 path through the dispatcher; the legacy poller stays for the v1 case.

**Cost of keeping**: parallel notification code path. Two `render_position_closed` invocation sites. If `render_position_closed` is updated, both paths need testing.

**Retirement trigger**: when every active deployment has had its v1→v2 migration run AND a destination-scoped BotBinding configured. Today every fresh install gets this automatically; only pre-v2 installs that haven't opened the GUI lack it.

**Plan to retire**:
1. Confirm via telemetry (or operator audit) that no production deployment runs without a v2 binding.
2. Delete `position_close_notifier` from `src/bot_loops/notification.py`.
3. Remove from `src/bot_loops/__init__.py` re-exports.
4. Remove `_supervise(asyncio.create_task(position_close_notifier(app)), ...)` from `src/bot.py::post_init`.
5. Drop the `else` branch in post_init entirely (when v2 binding is universal, the OutboxTailer path is unconditional).

---

### 3. Legacy `notification_dispatcher` polling loop (Step 7 design)

**Status**: ◐ Aging (mutex'd in Step 7, same condition as #2)
**Where**: `src/bot_loops/notification.py::notification_dispatcher` — only runs when no v2 binding exists for this destination.

**Why it exists**: same as #2 — pre-v2 fallback.

**Retirement trigger**: same as #2. Both retire together.

**Plan to retire**: combine with #2's plan — single deletion pass removes both.

---

### 4. Module-level `SYSTEM_PROMPT` / `TRIAGE_SYSTEM_PROMPT` globals (Step 3 design)

**Status**: ☐ Active
**Where**:
- `src/ai.py` — `SYSTEM_PROMPT = _render_system_prompt()` at module import
- `src/ai_triage.py` — `TRIAGE_SYSTEM_PROMPT = _render_triage_prompt()` at module import
- `src/orchestrator.py` — falls back to module globals when `profile=None`

**Why it exists**: Step 3 made AI clients accept per-call / per-instance prompts but left the module-level globals as a third-priority fallback. The Playground and Profile Generator wizard still use the name-based form (`_render_system_prompt(profile_name)`).

**Cost of keeping**: prompt is rendered eagerly at module import even when not used (cheap but unnecessary). Confusing for new readers: "which prompt does this call use?"

**Retirement trigger**: when Playground + Profile Generator wizard pass a ProfileContext explicitly instead of relying on the global.

**Plan to retire**:
1. Refactor `src/gui/services/playground.py` to construct a ProfileContext from the active stack's profile and pass it to AIClient/TriageClient.
2. Refactor `src/gui/windows/profile_generator_wizard.py` similarly. The wizard renders the triage prompt against a bootstrap profile (no real channel data); use `render_bootstrap_triage_prompt(symbol)` directly, no global needed.
3. Delete the module-level `SYSTEM_PROMPT = _render_system_prompt()` line in `src/ai.py`.
4. Delete the module-level `TRIAGE_SYSTEM_PROMPT = _render_triage_prompt()` line in `src/ai_triage.py`.
5. Update `AIClient.call` to require `system_prompt` (parameter becomes mandatory). Same for `TriageClient.classify`.
6. Update tests that relied on the global fallback to pass explicit prompts.

---

### 5. v1 config fallback in `discover_stacks()` (Step 1 design)

**Status**: ☐ Active
**Where**: `src/gui/services/stack_registry.py::discover_stacks` — on every call, checks if `stacks_config.json` is v1 (`is_v2()` False) and triggers auto-migration via `config_v1_to_v2.migrate()`.

**Why it exists**: ensures existing v1 installs upgrade seamlessly on first GUI launch after the v2 release.

**Cost of keeping**: every GUI launch reads `stacks_config.json` and checks `is_v2()` — microseconds, negligible. The actual migration code (`src/migrations/config_v1_to_v2.py`) is ~150 lines that no one will ever exercise after universal migration.

**Retirement trigger**: when the operator population is fully on v2 (≥6 months past v2 release with no legacy reports). Also: if anyone EVER manually rolls back a v2 config to v1, this path is the rescue — so even after retirement, keep `migrate()` reachable via a manual CLI command.

**Plan to retire**:
1. Replace auto-migration check with a one-line log warning ("v1 config detected — run `copytrades migrate-config` to upgrade").
2. Move migration to a manual CLI command in `src/gui/__main__.py` (`--helper migrate-config`).
3. Eventually delete `src/migrations/config_v1_to_v2.py` and the helper after no operator has reported needing it for 12+ months.

---

### 6. `--service listener` route in `gui_launcher.py` (Step 8 design)

**Status**: ☐ Active
**Where**: `gui_launcher.py::_dispatch_service` — supports both `--service listener` (legacy per-stack) and `--service shared-listener` (v2 per-account).

**Why it exists**: existing NSSM services installed before Step 8 still point at `--service listener`. The Step 8 migration removes those services and reinstalls them pointing at `--service shared-listener`, but the route stays for any operator who has cached / pinned the old config.

**Retirement trigger**: when Step 8 migration has been run on every production deployment AND no legacy `CT-<NAME>-Listener` services exist anywhere.

**Plan to retire**: tied to #1 — when single-channel uses the multi-channel runner, `listener.main()` goes away entirely and so does the `--service listener` route.

---

### 7. Plaintext `Account.phone` in `stacks_config.json` (review item #7, Day 4 partial)

**Status**: ☐ Active (Day 4 added redaction helper, did NOT encrypt)
**Where**: `src/config_v2.py::Account.phone` — plaintext string in JSON on disk.

**Why it's a concern**: PII. Phone numbers leak via:
- Operator screenshots for support
- Accidental backups uploaded to cloud
- Filesystem reads by any process on the box
- `git` commits if the config ever gets checked in

**Day 4 mitigation**: added `Account.phone_display()` returning redacted form (`+961***4567`). All operator-visible surfaces (GUI Accounts tab, listener startup log) now use this. Raw `account.phone` is still readable in code paths that need it for Telethon auth.

**What's still pending**: at-rest encryption. The existing `src/secret_box.py` (DPAPI) wraps secrets in destination DBs (`tg_bot_token`, `tg_session_blob`, `tg_api_hash`). The config file itself isn't covered.

**Retirement trigger**: first customer complaint about phone leakage, OR a planned product release that promises "no PII at rest." Not blocking today.

**Plan to retire (full encryption)**:
1. Extend `src/db_settings.py::SECRET_KEYS` to include `tg_phone` (writes encrypt, reads decrypt automatically).
2. Add `Account.phone_secret_db: str | None` field — when set, the listener resolves the phone by reading the named destination DB's `tg_phone` setting (decrypted in transit).
3. Add a one-shot migration GUI action: "Encrypt phones in stacks_config.json" → moves each Account's plaintext phone into the linked destination's settings table, sets `phone_secret_db`, blanks the plaintext field.
4. Update the v1→v2 migration to encrypt by default for new installs.
5. After 90 days: deprecate plaintext `Account.phone` (log warning when present).
6. After 180 days: remove plaintext field; fail GUI launch if encrypted phone can't be resolved.

---

### 8. Plaintext `Account.session_path` in `stacks_config.json`

**Status**: ☐ Active (less sensitive than phone — it's a path, not a secret)
**Where**: `src/config_v2.py::Account.session_path` — plaintext filesystem path.

**Why it's a (minor) concern**: discloses operator's username + APPDATA layout. Less PII than the phone but still leaks system structure.

**Retirement trigger**: low priority. Realistically retires alongside the phone-encryption work.

**Plan to retire**: include in #7's encryption work. The path becomes derived from `account.id` (convention: `%APPDATA%/CopyTrades/accounts/<account_id>/telegram.session`) so it doesn't need to be stored at all.

---

### 9. Hot-path `_render` returning None on noop ea_response (Step 7 design)

**Status**: ☐ Active
**Where**: `src/bot_outbox_tailer.py::_render_action_terminal` — returns None when `ea_response.startswith("noop_")`. Caller logs the skip but DOESN'T mark `delivered_at`. Row stays in the table permanently.

**Why it exists**: noop responses mean the EA explicitly suppressed an action; operator asked for these to be invisible. Marking delivered_at felt wrong (no DM was actually sent).

**Cost of keeping**: undelivered rows accumulate in `bot_outbox` over time. Per the legacy operator pattern of ~200 noop_* events/year, this is hundreds of permanently-undelivered rows after a few years. Hot path's `WHERE delivered_at IS NULL LIMIT 20` query gets less efficient as the never-delivered set grows.

**Retirement trigger**: when `bot_outbox` table size exceeds 10MB OR when the polling latency becomes user-visible.

**Plan to retire**:
1. Change `_render_action_terminal` to set a special marker (`delivered_at = '1970-01-01T00:00:00+00:00'`) when the render returns None — distinguishes "intentionally suppressed" from "real but unsent."
2. Hot-path query filter: `WHERE delivered_at IS NULL` continues to skip the suppressed rows.
3. Periodic cleanup task: `DELETE FROM bot_outbox WHERE delivered_at = '1970-01-01T00:00:00+00:00' AND created_at < datetime('now', '-30 days')`.
4. Or simpler: just mark as delivered. Operators who asked for "invisible" get one undelivered row instead of zero — acceptable.

---

### 10. Per-bot polling at 1-second tick (Step 7 design)

**Status**: ☐ Active
**Where**: `src/bot_outbox_tailer.py::run_forever` — polls every 1.0s for undelivered rows.

**Cost of keeping**: with 1 bot today, invisible (3600 queries/hour, each returning ≤20 rows). With multi-bot expansion (Step 14 deferred), N polls/second on shared destinations. Still small in absolute terms (sqlite is fast) but wasteful.

**Retirement trigger**: when bot count per destination exceeds 3 OR when DB lock contention becomes measurable.

**Plan to retire**:
1. Add a wakeup signal — could be a file mtime, a named pipe, or a sqlite trigger that sets a `dirty` setting.
2. `OutboxTailer.run_forever` waits on the signal with a timeout fallback (every 30s catch-up poll for safety).
3. `dispatch_notification` raises the signal after writing the outbox row.
4. Net: zero polling under steady-state load; one cheap signal write per event.

---

## Quick-reference table

| # | Item | Status | Trigger |
|---|---|---|---|
| 1 | N=1 listener delegate | Active | Multi-channel runner parity + 30d validation |
| 2 | Legacy position_close_notifier | Aging | Universal v2 binding adoption |
| 3 | Legacy notification_dispatcher | Aging | Universal v2 binding adoption |
| 4 | Module-level SYSTEM_PROMPT globals | Retired (2026-05-24) | Prompts/Playground gap closed: `prompt_inspector.render` + `run_playground` now accept `profile_name=` and load via `profile_io.load_profile` → `_render_system_prompt_from_data`. ai.py globals stay as fallback for legacy callers (no caller depends on them for new code). |
| 5 | v1 config auto-migration | Active | 6mo+ post-v2 with zero legacy reports |
| 6 | `--service listener` launcher route | Active | After #1 retires |
| 7 | Plaintext phone in v2 config | Active | First leak complaint OR planned product promise |
| 8 | Plaintext session_path | Active | Retires with #7 |
| 9 | Permanently-undelivered noop rows | Active | bot_outbox table size > 10MB |
| 10 | 1-second polling tick | Active | >3 bots per destination |
| 11 | `bootstrap_services_install` (rigid 3-tuple) | Aging | After v2-spec install path proves out for 30d in production. Phase-4 migrated BootstrapManager + Settings to the new `bootstrap_v2_install` helper but kept the old one as a fallback for v1-only configs. |
| 12 | `Stack` dataclass + `discover_stacks` | Active | Renaming to `DestinationView` would be ~40 files of mechanical churn with zero operator-visible benefit. Stack now correctly = one-per-Destination (Phase 1) and only carries view-scope data the GUI needs. Retire ONLY if a future feature needs to bind views to a non-destination scope (none planned). |
| 13 | Add Stack wizard | Aging | After 90d of operator data showing zero new-from-scratch wizard use vs. high standalone Add-Account/Profile/Destination/Bot use. Phase-4 added a "use V2 Config for incremental adds" tip on the welcome page; full deprecation waits on adoption. |

---

## When to retire something

Before deleting a compat shim, run this checklist:

1. **Has the retirement trigger fired?** Be honest — if you're tempted to skip the trigger, you don't have evidence yet.
2. **Are there hermetic tests covering BOTH paths?** Delete only after the legacy path is unreachable, not just unreachable-on-paper.
3. **Have you grepped for usages?** Includes tests, scripts, docs, comments referencing the shim.
4. **Is there a fallback plan if removal breaks something?** Cherry-pick the deletion to its own commit so revert is one git command.
5. **Update this file.** Move the entry from Active/Aging to Retired with the date and commit SHA.

---

## What's NOT in this list (and why)

- **Deferred features** (Steps 11–21 of the multi-channel plan): these are *additions*, not removals. Tracked separately in the routing plan's "Deferred steps" section.
- **Open TODOs / FIXMEs in code**: those are micro-issues. Track via grep when bored, not in a plan doc.
- **Subjective code-quality work** ("rename this function", "extract this helper"): not compat shims — handle via normal review.
