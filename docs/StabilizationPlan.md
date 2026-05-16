# Stabilization Plan

Five-phase plan to harden the app before sustained live use. Order by
dependency (cleanup → features → tests → real-channel validation).

## Phase A — Stat-helper refactor (foundation cleanup)

**Goal.** Delete the 5 duplicated `_stat_box` shims that route through
`StatCard`. Replace direct calls in all view files with `StatCard`. Kill
inline-styled card patterns elsewhere (cost budget banner, journal
reason table) that don't already follow palette tokens.

**Files.**
- `src/gui/views/journal_view.py` — drop `_stat_box`, call `StatCard` directly.
- `src/gui/views/cost_view.py` — same.
- `src/gui/views/risk_view.py` — same.
- `src/gui/views/rejected_view.py` — same.
- `src/gui/views/replay_view.py` — same.
- `src/gui/panels/_stat_card.py` — optional: add a `set_value(value, accent)`
  method so the views can update existing cards instead of destroying and
  rebuilding them on every refresh (removes the RuntimeError race we
  saw on theme toggle).

**Effort.** ~2 hours.
**Risk.** Low. Read-only views; no DB / EA / wire-protocol touched.
**Deliverable.** ~80 LOC removed, one shared widget, no functional change.

---

## Phase B1 — Backup + restore

**Goal.** One-click export of all per-stack state to a zip, one-click
restore. Lives in Settings → a new "Backup" tab next to Channels /
Tuning / Services.

**What gets backed up.**
- `%APPDATA%/CopyTrades/stacks_config.json`
- `%APPDATA%/CopyTrades/state.json`
- `%APPDATA%/CopyTrades/<stack>/copytrades.db` (each stack)
- `%APPDATA%/CopyTrades/<stack>/profile.json` (each stack)
- `%APPDATA%/CopyTrades/<stack>/logs/trades.log` (high-value history)

**What is NOT backed up.**
- The DPAPI ciphertext only decrypts on the original machine + account
  (machine scope). Restoring to another machine = re-run setup wizard
  to re-encrypt secrets. The backup carries the ciphertext as-is, so
  same-machine restore works fully.

**Files.**
- NEW `src/gui/services/backup_io.py` — `make_backup(target_dir) -> Path`,
  `restore_backup(zip_path) -> RestoreResult`. ~150 LOC.
- NEW `src/gui/views/backup_view.py` OR a tab inside SettingsView with
  Backup / Restore / Show last-backup-time buttons + a path picker. ~120 LOC.
- `src/gui/views/settings_view.py` — wire the new tab.

**Effort.** ~half a day.
**Risk.** Medium. Restore must validate the zip before clobbering — bad
restore could wipe a working stack.
**Deliverable.** `Backup now` button → produces `copytrades-backup-YYYY-MM-DD.zip`.
`Restore from zip…` button → confirms, replaces files, asks operator to
restart the app.

---

## Phase B2 — Hard cost cap (halt on overrun)

**Goal.** When daily AI spend exceeds `cost_daily_budget_usd × cap_multiplier`
(default 1.2, configurable), force `kill_switch=on` and DM the operator.
A spike + misclassification loop can't drain hundreds of dollars unattended.

**Where it runs.** Inside the existing bot service's
`telegram_heartbeat_loop` cadence (every 30s already polling) OR a new
`cost_guard_loop` co-supervised in `post_init`.

**Logic.**
1. Compute today's spend from `logs/ai_calls.jsonl` (same logic the
   Cost view uses — extract into a shared helper if not already).
2. Read `cost_daily_budget_usd` and `cost_cap_multiplier` (new DB setting,
   default `1.2`).
3. If `today_cost > budget × multiplier` AND `kill_switch != on`:
   - Set `kill_switch = on`.
   - DM the operator: "Daily AI spend $X exceeded $Y × 1.2 — auto-halted.
     Investigate in REJECTED / COST views, then click RESUME."
   - Log to `trades.log` for the audit trail.

**Files.**
- `src/cost_guard.py` (NEW, ~80 LOC) — `should_halt(db_path) -> tuple[bool, str]`
  and `enforce(conn) -> bool`.
- `src/bot.py` — add `cost_guard_loop` task in `post_init`.
- `src/db_settings.py` — add `cost_cap_multiplier` default `1.2`.
- `src/gui/views/cost_view.py` — surface the multiplier control next to
  the budget spin box.

**Effort.** ~half a day.
**Risk.** Low — only flips an existing knob (`kill_switch`); no new EA
behavior.
**Deliverable.** Auto-halt fires within ≤60s of the breach, persistent,
operator-clearable.

---

## Phase C — GUI smoke tests

**Goal.** Catch the obvious regressions: every view opens without crashing,
every button is clickable, theme toggle works in both modes.

**Framework.** `pytest-qt` — already pip-installable; gives a `qtbot` fixture
that drives Qt widgets headlessly.

**Coverage targets.**
1. **App boot smoke**: instantiate `MainWindow` against a temp-stack DB,
   verify all 9 views are added to the stack widget, no exceptions.
2. **Theme toggle**: flip light → dark → light, assert palette changes
   propagate, no exceptions.
3. **Wizard pages**: instantiate each `_StackIdentityPage`,
   `_AIProviderPage`, `_BotTokenPage`, `_CredentialsPage` standalone;
   verify form fields validate.
4. **Prompts inspector**: render each of the 5 prompt IDs in both modes,
   assert non-empty system + user content.
5. **Triggers tab**: load profile, mock-edit a trigger, save, verify
   rendered prompt fields reflect the change.
6. **Settings → Backup**: smoke-test `make_backup` against a temp dir
   (no actual restore — destructive).

**Files.**
- `tests/gui/conftest.py` — `qtbot` fixture, `tmp_stack` fixture that
  builds a self-contained APPDATA + DB + profile in a tmp dir.
- `tests/gui/test_smoke.py` — the 6 cases above. ~250 LOC total.
- `pyproject.toml` — add `pytest-qt` to dev deps.

**Effort.** ~1 day.
**Risk.** Low. Pure read-only tests; won't break the trading path.
**Deliverable.** `pytest tests/gui -v` green in CI / locally. Catches:
import errors, missing wires, broken signal connections, palette
regressions, wizard page validation breaks.

---

## Phase D — Live-channel validation

**Goal.** Run the system against a real signal channel on demo MT5 for
30 days. Measure rejection rate, prompt-drift, P&L attribution, and
whether the AI's evaluator score correlates with actual outcomes.

**Process (operator-driven, not code).**
1. Pick one channel. Demo MT5 account funded $5K.
2. Run end-to-end for 30 days. Don't touch settings mid-run unless
   something is on fire.
3. Daily check (5 min):
   - JOURNAL: today's PnL, trade count.
   - REJECTED: any new spike categories?
   - COST: trending vs budget.
   - LIVE: services still all green, EA heartbeat fresh.
4. Weekly inspection (30 min):
   - Drilldown on each rejected category.
   - Manually run the PROMPTS → Live mode on the worst rejection's
     message — was the AI obviously wrong?
   - If yes: add a custom-rule to `classifier_custom_prompt` OR fix
     the profile via Triggers tab. Don't change anything else.
5. End of 30 days:
   - Total PnL.
   - AI cost.
   - Net (PnL − AI cost − broker spreads).
   - Decision: keep this channel or drop it.

**No code changes.** This phase is the input to whatever Phase E becomes.

**Deliverable.** A go / no-go signal on the product itself.

---

## Order of execution

```
A (refactor, 2h)
   ↓
B1 (backup, 0.5d)        B2 (cost cap, 0.5d)
   ↓                         ↓
C (smoke tests, 1d)  ← depends on A + the new code from B1/B2
                                          ↓
                                D (live test, 30d, parallel
                                   to everything from B1 onward)
```

Code work: **roughly 2.5 days**. Phase D runs alongside.

---

## Out of scope here

- Multi-channel-per-stack
- Multi-stack comparative dashboard
- Per-stack EA controls
- Signed installer
- Asyncio shutdown noise filter (Python 3.13 needs a different approach)
- Memory-leak fix for the lambda-connect-without-disconnect pattern
  (mitigated by try/except RuntimeError already; revisit if memory grows)
- ML / signal-quality scoring beyond the evaluator
