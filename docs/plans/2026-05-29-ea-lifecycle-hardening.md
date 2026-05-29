# EA Lifecycle Hardening — Implementation Plan

**Date:** 2026-05-29
**Status:** Planned (not started — awaiting go-ahead)
**Origin:** Lifecycle review triggered by diagnostics bundle `SMC-20260529-152134`
(see also shipped fix #23 in `FIXES_TODO.md` — stale singleton-management retarget).

## Summary

A review of the EA's run loop (`ea/CopyTrades.mq5` `OnInit` → `OnTimer`/`OnTick` →
`OnDeinit`) surfaced 8 issues arising from how the lifecycle is structured: a
single-threaded blocking-I/O loop that couples stop management to API health,
an over-broad kill-switch scope, an asymmetry in result-POST retry handling, a
one-directional reconciler, and several act-then-persist / restart windows.

This plan fixes all 8, scoped as **separate PRs per phase** for review
granularity. Note the EA is a single compiled artifact: EA-side PRs are reviewed
independently but **deploy together** in one MetaEditor recompile.

## Decisions (locked)

1. **M3 orphan recovery DMs the operator.** A recovered orphan position is
   surfaced via the existing `/alerts` path (bot `notification_dispatcher` DMs
   the owner), because a recovery means a result POST was lost and the operator
   should know.
2. **Recovered positions are flagged** with a queryable `recovered` column on
   `positions` so the DM is accurate and the GUI can distinguish recovered rows.
3. **Each phase is its own branch/PR off `main`** (this work is unrelated to the
   current `feat/channel-learning-loop` branch).

## Severity recap

| ID | Sev | Issue | Primary file |
|----|-----|-------|--------------|
| H1 | HIGH | Single-threaded blocking I/O starves trade management; mgmt sequenced after network | `ea/CopyTrades.mq5` |
| M1 | MED | `/halt` freezes management of the open position, not just new entries | `ea/CopyTrades.mq5` |
| M2 | MED | Management-action results bypass the retry queue (asymmetry with OPEN) | `ea/CopyTrades.mq5` |
| M3 | MED | Reconciliation is one-directional — orphan broker positions invisible | `ea/CopyTrades.mq5` + `src/api.py` |
| M4 | MED | "Act, then persist" crash windows (stage advance; OPEN result) | `ea/CopyTrades.mq5` |
| L1 | LOW | `KillSwitchOn` blocking GET every second | `ea/CopyTrades.mq5` |
| L2 | LOW | Trailing SL only evaluated on `OnTimer`, not `OnTick` | `ea/CopyTrades.mq5` |
| L3 | LOW | File-scope statics reset on recompile; recent-opens grace empty after restart | `ea/CopyTrades.mq5` |

## Constraints

- **MQL5 has no local CI here** — EA changes verified by MetaEditor F7 compile +
  scripted demo smoke tests. Python changes get hermetic `pytest` coverage.
- **Lockstep:** M3's `POST /positions/recover` (Phase 0, Python) must ship and
  the API be restarted **before** the recompiled EA (Phase 3) calls it, else the
  EA's recovery POSTs 404 (non-fatal but defeats the fix).
- Changes stay additive and follow existing patterns: cache-TTL mirrors
  `RefreshBeSettings`; result retry mirrors `DoOpen`'s tail; recovery insert
  mirrors `post_result`'s `INSERT OR IGNORE` first-insert-wins rule.

---

## Phase 0 — Server groundwork (M3 endpoint) — `fix/positions-recover-endpoint`

**Branch off:** `main`. **Fully testable here.**

**Files**
- `src/api.py` — add `POST /positions/recover`. Body = EA snapshot
  (`mt5_ticket, symbol, side, volume, entry_price, sl, tp`). Insert with
  `INSERT OR IGNORE INTO positions(action_id=NULL, ..., original_volume=volume,
  recovered=1, status='open')`. `OR IGNORE` (UNIQUE `mt5_ticket`) makes it a safe
  no-op when the row exists and prevents resurrecting a `closed` row
  (first-insert-wins). On a genuine insert, fire the `/alerts` notification path
  so the operator is DM'd ("recovered orphan position ticket=…").
- `src/schema.sql` — add `recovered INTEGER NOT NULL DEFAULT 0` to `positions`.
- `src/db.py` — add idempotent migration `_migrate_positions_add_recovered`
  (add column if absent), registered in `init_schema()` alongside the others.
- `src/api_models.py` — request model for the recover body (validated like other
  EA-facing bodies).
- `tests/test_api.py` — hermetic tests:
  - recover inserts a new open row with `recovered=1`;
  - recover is idempotent (second call no-ops via UNIQUE ticket);
  - recover does **not** resurrect a `closed` row for the same ticket;
  - recover triggers exactly one operator notification on first insert.

**Maps to:** M3 (server half).
**Verify:** full hermetic suite green + new cases. No EA dependency.

---

## Phase 1 — EA `OnTimer` restructure — `fix/ea-ontimer-restructure`

**Branch off:** `main`. **File:** `ea/CopyTrades.mq5` only.

- **H1 — management before network + tighter timeouts.** Reorder `OnTimer`
  (`:331`) so broker-local management runs first, then network pulls:
  ```
  RolloverDayIfNeeded()
  ManagePlans(); TrailStage2Sls(); ManageNakedPlans(); ManagePendingOrders()
  g_kill_switch_cached = KillSwitchOn()         // cached (L1)
  if(!halted) PollAndExecute()
  ReconcileClosedPositions(); DrainRetryQueue()
  HeartbeatMarketPrice(); dashboard; PostMarketSnapshot()
  ```
  Add inputs `HttpGetTimeoutMs` (default 2000, was hard 5000 at `:1055`) and
  `HttpPostTimeoutMs` (default 3000, was hard 10000 at `:1092`). Bounds the
  worst-case single-tick stall from ~30–60s to a few seconds.
- **M1 — halt scope.** Gate **only** `PollAndExecute` on the kill switch; let
  management/trailing/naked/pending/reconcile/retry-drain run while halted so an
  open position stays managed and reconciled. Channel-driven CLOSE/MOVE still
  pause (they flow through `PollAndExecute`).
- **L1 — cache kill switch.** `KillSwitchOn` (`:1017`): add a `GetTickCount` TTL
  (~3–5s) mirroring `RefreshBeSettings` (`:848`); keep fail-closed
  (`g_kill_switch_known`/last-known).
- **L2 — trail on tick.** `OnTick` (`:819`): add `TrailStage2Sls()` beside
  `ManagePlans()`.

**Maps to:** H1, M1, L1, L2.
**Verify:** F7 compile; demo smoke tests 1 & 2 (below).

---

## Phase 2 — EA result robustness — `fix/ea-result-retry-queue`

**Branch off:** `main` (stack on Phase 1 if merged after — same file).
**File:** `ea/CopyTrades.mq5` only.

- **M2 — route `PostResult` through the retry queue.** `PostResult` (`:3479`):
  switch `HttpPostJson` → `HttpPostJsonWithStatus`; on failure branch on
  `IsRetryableStatus(status)` → `EnqueueRetry(resultUrl, body)` for transport/5xx,
  log-and-drop on 4xx — mirroring `DoOpen`'s tail (`:1708–1725`). `EnqueueRetry`'s
  URL dedupe handles per-action uniqueness. Aligns management-result durability
  with the server-side sweeper change (#23).

**Maps to:** M2.
**Verify:** F7 compile; demo smoke test 3.

---

## Phase 3 — EA reconcile + restart safety — `fix/ea-reconcile-restart-safety`

**Branch off:** `main` (stack on Phase 2 — same file). **Depends on Phase 0
merged + API restarted.** **File:** `ea/CopyTrades.mq5` only.

- **M3 — reverse reconcile.** In `ReconcileClosedPositions` (`:3083`), after the
  DB→MT5 pass add an MT5→DB pass: iterate `PositionsTotal()`, filter
  `POSITION_MAGIC == Magic` and `Symbol_Override`; for any ticket not in the
  open-set already fetched from `GET /positions?status=open`, `POST
  /positions/recover` with the snapshot. Skip `IsRecentlyOpened(ticket)` to avoid
  racing a just-opened fill.
- **M4a — stage-advance crash window.** Move `GlobalVariableSet(stage)` to fire
  immediately after the partial fill is confirmed (`partialOk`, `:2289`), before
  the SL modify + `PostPositionUpdate`. In `LoadPersistedPlans` (`:1829`) add
  volume-based stage inference: if restored live volume is materially below
  `origLots` for the persisted stage, bump `stage` to match what already fired
  (single-position model makes this unambiguous). Prevents a double partial on
  restart.
- **M4b — OPEN orphan window.** Covered structurally by M3 (live position with
  no DB row gets recovered next tick). No extra code; note in commit.
- **L3 — reconcile warmup after attach.** Set `g_init_at = TimeCurrent()` in
  `OnInit`; make `ReconcileClosedPositions` no-op for the first ~5s after attach
  so it can't false-close a restored position before MT5's local cache is warm.

**Maps to:** M3 (EA half), M4, L3.
**Verify:** F7 compile; demo smoke tests 4 & 5.

---

## Phase 4 — Docs — `docs/ea-lifecycle-hardening`

**Branch off:** `main`. **Files:** `CLAUDE.md`, `FIXES_TODO.md`.

- `CLAUDE.md` — correct the operational note: the EA kill switch now pauses only
  channel-driven actions / new entries while staged management, trailing,
  reconciliation, and retry-drain continue for an open position (M1). Update the
  `OnTimer` ordering description.
- `FIXES_TODO.md` — append entries #24–#30 (one per issue), each naming files
  touched and verification done, matching the existing audit-trail format.

> Docs may instead be folded into each phase PR; keep a single docs PR only if
> phases land far apart in time.

---

## Phase 5 — Verification & deployment

- **Python (Phase 0):** `.venv\Scripts\python.exe -m pytest -q` (full hermetic
  suite must stay green — currently 1213 passed, 21 skipped) + new `test_api.py`
  cases.
- **EA (Phases 1–3):** MetaEditor F7 — zero errors/warnings. WebRequest whitelist
  unchanged (`http://127.0.0.1:8765`).
- **Demo smoke tests** (`MaxLotsPerSignal=0.01`, demo ≥2 weeks per CLAUDE.md):
  1. **H1/L1:** stop `api.py` mid-trade → OnTimer keeps ticking, management not
     frozen, kill-switch fail-closed; restart → retry queue drains.
  2. **M1:** `/halt` with a position open → trailing SL still ratchets, reconcile
     still mirrors a broker close; new channel OPEN does not fire until un-halt.
  3. **M2:** transient API blip during a `MOVE_SL_BE` → `ct_retry_*.txt` appears
     and drains; action ends `executed`, not `failed`.
  4. **M3:** simulate a lost OPEN result (kill api between fill and POST) →
     `/positions/recover` repopulates the DB row next tick; operator gets a DM.
  5. **M4:** restart EA mid-staged-plan after a partial → no double partial
     (stage inferred correctly).
- **Deploy order:** merge Phase 0 → `pytest` green → restart `api.py` (endpoint
  live) → merge EA phases → recompile + re-attach EA (one artifact for Phases
  1–3) → merge docs. `bot.py` restart not required (no loop changes beyond #23,
  already shipped).

## PR / branch map & dependencies

| Phase | Branch | Files | Depends on | Deploys via |
|-------|--------|-------|------------|-------------|
| 0 | `fix/positions-recover-endpoint` | `src/api.py`, `src/schema.sql`, `src/db.py`, `src/api_models.py`, `tests/test_api.py` | — | restart `api.py` |
| 1 | `fix/ea-ontimer-restructure` | `ea/CopyTrades.mq5` | — | EA recompile |
| 2 | `fix/ea-result-retry-queue` | `ea/CopyTrades.mq5` | stacks on 1 (same file) | EA recompile |
| 3 | `fix/ea-reconcile-restart-safety` | `ea/CopyTrades.mq5` | Phase 0 live; stacks on 2 | EA recompile |
| 4 | `docs/ea-lifecycle-hardening` | `CLAUDE.md`, `FIXES_TODO.md` | — | docs only |

**EA artifact note:** Phases 1–3 all edit `ea/CopyTrades.mq5`. Review them as
separate PRs, but because the EA is one compiled binary they must be **merged in
order (1→2→3) and deployed in a single recompile** — you cannot run a build that
contains only Phase 1.

## Risk summary

| Phase | Risk | Mitigation |
|-------|------|------------|
| 0 | Recovered row masks a real divergence | `OR IGNORE` first-insert-wins; never resurrect `closed`; DM + `recovered` flag for visibility |
| 1 | Reorder changes first-management-tick timing | management idempotent; broker SL/TP unaffected; demo-verify |
| 2 | Retrying result POSTs spams on a true 4xx | `IsRetryableStatus` treats 4xx terminal |
| 3 | Volume-based stage inference mis-bumps | single-position model; conservative threshold (≥ one partial fraction); warmup guard |
| all EA | No MQL5 CI | F7 compile gate + scripted demo smoke tests |

## Out of scope (explicitly deferred)

- Replacing polling with sockets/WebSocket (discussed and rejected — low ROI;
  MQL5 has no async receive callback; dominant latency is AI interpretation).
- Any change to the AI prompt, triage, or the Channel Learning Loop.
