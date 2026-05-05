# CopyTrades — Fixes Log

System review completed 2026-05-05. **All 22 issues from the review are
fixed.** This file is now an audit trail of what shipped.

To reopen the review or add new items, run the review again
(`security-reviewer`, `silent-failure-hunter`, `code-reviewer`,
`database-reviewer` agents in parallel) and append findings below.

---

## Shipped fixes (in order applied)

### CRITICAL

- **#1 Promoter type filter** — `src/promoter.py`. Removed the
  `action_type IN (...)` whitelist; all 7 Phase-2 management types now
  flow through. 11 stranded pre-fix actions were marked `rejected` with
  `ea_response='stranded_pre_promoter_fix'`.
- **#2 post_result atomicity** — `src/api.py`. UPDATE actions + INSERT
  positions wrapped in explicit BEGIN/COMMIT with ROLLBACK. trades.log
  emissions moved after the commit so rollbacks don't leave phantom
  lines.

### HIGH

- **#3 Kill switch fail-closed** — `ea/CopyTrades.mq5:KillSwitchOn`.
  Cached last-known state via two file-scope statics; defaults to true
  (halted) on first failure, returns last-known on subsequent failures.
  Requires EA recompile.
- **#4 DoCloseAll status** — `ea/CopyTrades.mq5:DoCloseAll`. Status now
  picked by outcome: `failed` if every PositionClose failed; `executed`
  for vacuous success or partial success. Requires EA recompile.
- **#5 DoOpen result-POST resilience** — `ea/CopyTrades.mq5`. On POST
  failure, the result body is enqueued via `EnqueueRetry` to
  `MQL5\Files\ct_retry_<seq>.txt` and resent every OnTimer tick by
  `DrainRetryQueue` until success or 24h drop-dead. Requires EA
  recompile.
- **#6 DoClosePartial verify-then-advance** — `ea/CopyTrades.mq5`.
  Channel-triggered partial now uses volume-diff verification (same
  pattern as ManagePlans stage 0) instead of trusting CTrade's bool
  return. Requires EA recompile.
- **#7 PostPositionUpdate resilience** — `ea/CopyTrades.mq5`. Captures
  the bool return from `HttpPostJsonWithStatus`; on failure routes the
  request body through the same retry queue as #5. Requires EA
  recompile.
- **#8 ManagePlans give-up alert** — `ea/CopyTrades.mq5` + `src/api.py`.
  New `POST /alerts` endpoint inserts ALERT rows into `actions`; bot's
  `notification_dispatcher` already DMs them. EA posts to `/alerts` from
  both stage-1 and stage-2 give-up branches in `ManagePlans`. 2 tests
  added. Requires EA recompile.
- **#9 release_stale_claims window 120s→300s** — `src/promoter.py`
  default bumped; `src/bot.py` call site updated to rely on the new
  default. Buys headroom against legitimately slow EA POST /result.
  EA-side re-check / nonce deferred.
- **#10 API auth middleware** — `src/api.py` + `ea/CopyTrades.mq5` +
  `src/config.py` + `.env` + `.env.example`. New `EA_SHARED_TOKEN` env
  var; new `auth_gate` middleware enforces `X-EA-Token` header when
  token is set (blank = dev-mode unauth). EA's `AuthHeader()` helper
  injects the token on every WebRequest. 4 tests added. Requires EA
  recompile + lockstep deploy.
- **#11 Symbol whitelist on /market/price** — `src/api.py`. Validates
  `body.symbol.upper() in config.SUPPORTED_SYMBOLS`; 400 on anything
  other than XAUUSD. Test added.

### MEDIUM

- **#12 ResultBody.status as Literal** — `src/api.py`. Switched from
  `str` to `Literal["executed","failed","rejected"]` so Pydantic
  rejects invalid values at parse time (422) instead of relying on the
  schema's CHECK constraint as the last line of defense.
- **#13 Single-position validator** — `src/validators.py`. Replaced
  `_has_overlapping_open_position` (zone-overlap, side-aware) with
  `_has_open_position` (any open position on the symbol blocks new
  OPEN). Defense-in-depth alongside the EA's CountOurOpenPositions
  guard. 2 new tests cover same-side and opposite-side cases.
- **#14 DoReinforce snapshot before close** — `src/api.py` adds new
  `GET /positions/by_ticket/{ticket}` endpoint joining the open
  position with its originating action's signal payload.
  `ea/CopyTrades.mq5:DoReinforce` now snapshots the live position via
  this endpoint BEFORE closing, eliminating the race against the
  fire-and-forget /positions/{t}/close POST that previously could
  return an older trade's params from /positions/last_closed. 2 tests
  added. Requires EA recompile.
- **#15 AI prompt-injection sentinels** — `src/ai.py`. Added "UNTRUSTED
  INPUT POLICY" section to SYSTEM_PROMPT instructing the model to
  treat content between `[BEGIN UNTRUSTED CHANNEL CONTENT]` /
  `[END UNTRUSTED CHANNEL CONTENT]` markers as data, never directives.
  Both `build_messages` and `AIClient.call` now wrap channel-originated
  text (recent_chat + new_message) with the sentinels.
- **#16 .env.bak.* in .gitignore** — `.gitignore`. Prevents
  `scripts/switch_account.py`'s rotated env backups from leaking into
  commits via `git add -A`.
- **#17 Atomic market-price write** — `src/api.py`. The 3 upserts for
  `bid`, `ask`, `at` now run inside an explicit BEGIN/COMMIT so a
  reader can't see a fresh bid paired with a stale `at` timestamp.
- **#18 Position-close notifier watermark by id** — `src/bot.py`.
  Switched from `closed_at`-string-compare watermark (which silently
  skipped positions sharing the same `closed_at` second across batch
  boundaries) to a monotonic `positions.id` watermark. New setting key
  `position_close_last_notified_id` seeded in the live DB to skip the
  current backlog.
- **#19 Bot asyncio task supervisor** — `src/bot.py`. New `_supervise`
  helper attaches a done-callback to each loop task; on uncaught
  exceptions logs and `os._exit(2)` so launch.bat / a supervisor
  restarts the process instead of leaving one loop silently dead.
- **#20 WAL auto-checkpoint pragma** — `src/db.py`. Added explicit
  `PRAGMA wal_autocheckpoint=1000` to `connect()` so WAL doesn't grow
  unbounded between manual checkpoints.

### LOW

- **#21 Bot callback split with maxsplit** — `src/bot.py:on_button`.
  `q.data.split(":", 1)` + op whitelist + int parse with try/except —
  malformed callback data now edits the message to "Invalid callback."
  instead of raising and leaving the button in a spinner.
- **#22 Partial fingerprint index** — `src/schema.sql` and
  `_migrate_actions_add_fingerprint` in `src/db.py`. Replaced the
  unconstrained `idx_actions_fingerprint(fingerprint)` with the partial
  composite `idx_actions_fingerprint(fingerprint, created_at) WHERE
  fingerprint IS NOT NULL`. Excludes ALERT/management-type rows
  (majority NULL) and lets `_has_recent_duplicate_open` run as a
  single range scan.

---

## Test count progression

- Session start: 161 hermetic tests
- After all fixes: **171 hermetic tests** — all pass
- Live replay tests (`tests/test_replay.py`,
  `tests/test_management_replay.py`) skipped per CLAUDE.md (require
  provider key, cost money). Re-run after any AI prompt edit (#15
  changed SYSTEM_PROMPT).

---

## Deployment checklist

To activate every fix on the live system:

1. **Restart `api.py`** — picks up #2, #10, #11, #12, #14, #17, #20
   (and the new `/alerts` and `/positions/by_ticket/{ticket}` endpoints).
2. **Restart `bot.py`** — picks up #1 (already shipped), #18, #19.
3. **Restart `listener.py`** — picks up #15 (prompt injection sentinels).
4. **Recompile EA** in MetaEditor (F4 → F7) — picks up #3, #4, #5, #6,
   #7, #8, #14 (EA side). Then in the EA's properties dialog paste the
   `EA_SHARED_TOKEN` value from `.env` into the new `ApiSharedToken`
   input field.
5. **Smoke test**:
   - Stop `api.py` → EA log should show kill switch=true (no trades
     fire), retry queue files appear in `MQL5\Files\` for any in-flight
     POSTs.
   - Restart `api.py` → retry queue drains within next OnTimer tick.
   - Send a CLOSE_ALL when no positions exist → DM shows
     `closed=0 failed=0 executed`. Send when N close attempts fail and
     0 succeed → DM shows `failed`.
   - Set `PartialMaxRetries=1` on demo and force a stuck partial → DM
     arrives via `/alerts`.
   - Switch broker accounts → MT5 disables algo-trading; EA respects
     and auto-recovers when re-armed.
