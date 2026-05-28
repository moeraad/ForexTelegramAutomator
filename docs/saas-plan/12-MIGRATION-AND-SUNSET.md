# 12 — Migration and Sunset

The operator (the codebase owner) is **subscriber #1**. Their existing single-stack SQLite install becomes the first paying customer of the new platform without losing in-flight trades, without re-authenticating Telegram, and without rebuilding curated triggers.

---

## Cutover sequence

```mermaid
gantt
    title Operator cutover (real elapsed time, not engineer-weeks)
    dateFormat YYYY-MM-DD
    section Pre-cutover
        New platform staged + smoke-tested:done, 2026-09-01, 2026-09-14
        Operator EA v2 dry-run on demo account: 2026-09-15, 2026-09-18
    section Cutover window (weekend, markets closed)
        Close all open positions on old stack manually OR wait for natural close: 2026-09-19, 1d
        Run SQLite→Postgres migration script: 2026-09-20, 6h
        Operator re-attaches EA v2 to chart on same MT5 terminal: 2026-09-20, 1h
        Smoke: a synthetic signal end-to-end via /admin/providers/[1]/replay: 2026-09-20, 1h
    section Post-cutover
        Old NSSM services disabled (not deleted): 2026-09-21, 1d
        Old PySide6 GUI uninstalled: 2026-09-30, 1d
        Old DB archived to S3: 2026-09-30, 1d
```

---

## What the migration script does

`apps/signal-svc/scripts/migrate_sqlite_to_pg.py` — one-time. Run on the operator's Windows machine where DPAPI can still decrypt secrets.

Inputs:
- Path to operator's SQLite DB (`<APPDATA>/CopyTrades/<stack>/copytrades.db`)
- Postgres connection string for the new platform (production or a staging mirror)
- Cloud KMS key alias for envelope encryption
- Operator's Clerk user_id (created via signup beforehand)

Steps:
1. **Pre-flight**: verify the SQLite DB is closed by all writers (NSSM services stopped); verify Postgres target is empty for the operator subscriber (idempotent check).
2. **Create operator records**: `users` (from Clerk), `subscribers` (active), `subscriptions` (manual, $0 internal plan), `broker_connections` (one row matching the operator's MT5).
3. **Decrypt secrets**: read DPAPI-encrypted blobs (`tg_session_blob`, `anthropic_api_key`, `openai_api_key`, `tg_api_hash`, `tg_bot_token`, `ea_shared_token`) → in-memory plaintext → re-encrypt with KMS envelope → write to `signal_providers` and `provider_settings`.
4. **Create signal provider**: one row for "Forex Engineer", with `tg_chat_id` and the channel profile JSON from `channels/Forex Engineer.json`.
5. **Subscribe operator to provider**: `subscriber_channel_subscriptions` row.
6. **Port messages**: `messages` table 1:1 (preserving `tg_message_id`, `chat_id`, `decided_stage`, etc.). Maps `is_backfill` directly. Sets `signal_provider_id=1`.
7. **Port actions**: deduplicate by `source_msg_id` to derive `signal_actions` (one provider-side row per unique LLM-emitted action), then emit `subscriber_actions` per operator (1:1 with original `actions`). Preserve all status values, payloads, fingerprints, lifecycle timestamps.
8. **Port positions**: `positions` table 1:1; preserve `mt5_ticket`, `original_volume`, `partial_close_count`, `sl_moved_at`. The single `broker_connection_id` maps from the operator's.
9. **Port signal_memory**: 1:1 to provider scope.
10. **Port unmatched_messages**: 1:1.
11. **Port settings** (the system-config-like ones): `cost_daily_budget_usd`, `evaluator_version`, `signal_memory_enabled`, etc. → `system_config` or `provider_settings`.
12. **Discard**: `bot_outbox` (notifications regenerated forward), DPAPI blobs that don't apply (`tg_phone` stored but encrypted now), GUI-side caches.
13. **Verification pass**: row counts, foreign-key integrity, sample lineage check (pick 10 random closed positions, walk back to `messages.text`).
14. **Emit a report** to stdout + S3: `migration-report-<timestamp>.json` with the counts.

Rollback: if the migration fails midway, the script is fully transactional — `BEGIN` at the top, `COMMIT` only on success. On failure: nothing written to Postgres; old SQLite is untouched.

---

## Telethon session migration — keeping the listener authenticated

The Telethon session blob is DPAPI-encrypted bytes. After decryption it's a Telethon `StringSession` — portable across machines. The migration script:
1. Decrypts on the Windows machine via the existing `src/secret_box.py`.
2. Hands the plaintext blob to the migration script via stdin (NEVER on disk in plaintext outside DPAPI).
3. Script writes envelope-encrypted version into `signal_providers.tg_session_blob_enc` (KMS DEK) and stores the encrypted DEK in `signal_providers.tg_session_dek_enc`.
4. The cloud `listener` decrypts at startup using the service-role KMS permission, loads the `StringSession`, and connects.
5. Telethon recognizes the same session: no SMS code, no 2FA prompt. The listener picks up where the old one left off.

If Telegram's anti-abuse decides "session from a new IP" → prompts a 2FA code: the operator goes to admin `/admin/providers/[1]` and enters the code via a one-time panel (not stored).

---

## EA v2 attach on the operator's existing MT5

1. Compile and download `CopyTrades-v2.0.0.ex5` from the staging release artifact.
2. Place in `MQL5/Experts/` on the operator's machine.
3. Detach current `CopyTrades.ex5` from chart; remove the loopback URL from Tools > Options > Expert Advisors > Allowed URLs.
4. Add `https://ea-api.copytrades.example.com` to Allowed URLs.
5. Attach `CopyTrades-v2.0.0.ex5` to the XAUUSD chart.
6. Inputs: paste `ApiBaseUrl`, `ApiBearerToken`, `ApiCertSha256`.
7. EA's `OnInit` calls `GET /v1/ea/connection/me` → confirms.
8. EA's `ManagePlans` rehydrates `g_plans[]` from `GlobalVariables` — same logic as before. **In-flight signals survive the cutover**.

Critical: do NOT delete the old `g_plans[]` GlobalVariables. The new EA's `LoadPersistedPlans` reads them at the same key namespace. If we change the namespace in v2.0.0, then in-flight management state is lost — explicit decision, must preserve key compatibility.

---

## Sunset checklist

### Discarded immediately (cutover day)

- ☐ NSSM services stopped + disabled (kept installed for emergency rollback first 30 days, then uninstalled)
- ☐ Old EA `CopyTrades.ex5` detached from MT5 chart, kept on disk for first 30 days
- ☐ `launch.bat`, `setup.bat`, `services/install_services.bat` archived to `legacy/` branch
- ☐ Operator's `gui_launcher.py` removed from Startup
- ☐ Bot token rotated (the new system gets a fresh bot; the old `@CopyTrades***Bot` is decommissioned)
- ☐ EA shared token revoked (the secret no longer authenticates anything)

### Discarded at +30 days

- ☐ PySide6 GUI fully uninstalled (`CopyTrades.exe` and Inno Setup installer removed from operator's Downloads)
- ☐ NSSM services fully uninstalled (`nssm remove CT-* confirm`)
- ☐ Operator's `<APPDATA>/CopyTrades/` directory zipped, encrypted, uploaded to S3 cold storage, then deleted locally
- ☐ DPAPI-encrypted secrets effectively dead — DPAPI key tied to old Windows install
- ☐ Local `logs/` archived to S3 then deleted
- ☐ `<APPDATA>/CopyTrades/stacks_config.json` deleted

### Discarded at +90 days

- ☐ SQLite archive moved to Glacier
- ☐ Old `<DB_PATH>` permanently dead
- ☐ Code repo: `legacy/` branch tagged; mainline of new repo is sole source

### Things that NEVER come back

- DPAPI / Windows-specific secret encryption
- The `EA_SHARED_TOKEN` global
- The 127.0.0.1:8765 transport
- The "stack" concept (replaced by signal_provider + subscriber pair)
- Per-stack NSSM service set
- The single-operator `_owner_only` check
- The PySide6 GUI

---

## Rollback plan

If the new platform fails catastrophically within the first 30 days:

1. **Stop the new EA v2** on operator's MT5 (immediate; no orders queued during this window).
2. **Re-enable the old NSSM services** (`nssm start CT-AR-Api CT-AR-Bot CT-AR-Listener`).
3. **Re-attach the old EA `CopyTrades.ex5`** (`g_plans[]` GlobalVariables still present because we didn't reset).
4. **Re-add the loopback URL** to MT5 allowed URLs.
5. **Telegram session continuity**: the same Telethon session blob still lives in the SQLite settings (we read it, we didn't move it). Listener reconnects.
6. **Data divergence**: anything that happened in the cloud platform after cutover is lost on rollback. Acceptable for the operator (sole subscriber); not acceptable if real paying subs are already on. **Therefore rollback windows close as soon as a paying subscriber other than the operator is onboarded.**

To make rollback safer for the paying-subscriber phase: do not onboard ANY new subscriber until the operator's cutover has been live and stable for 14 days. The cutover is then declared "irreversible" formally and the rollback plan is archived.

---

## What the operator notices

- Same MT5 terminal, same chart, same broker.
- New EA on chart (different version string in EA name).
- New canvas dashboard (slightly different — connection card vs. stack card).
- Bot DMs come from a new bot (operator opts-in via the deep link).
- Web admin at `admin.copytrades.example.com` replaces the PySide6 GUI:
  - Live view → `/admin/system/health` + `/admin/subscribers/1` (themselves).
  - Journal → `/admin/providers/1/journal`.
  - Replay → `/admin/providers/1/replay`.
  - Triggers → `/admin/providers/1/triggers`.
  - Pipeline → `/admin/providers/1/pipeline`.
  - Settings → `/admin/system/config` + `/admin/providers/1`.

No data loss. Curated triggers carry forward. Unmatched messages carry forward. Signal memory carries forward. All historic positions carry forward.
