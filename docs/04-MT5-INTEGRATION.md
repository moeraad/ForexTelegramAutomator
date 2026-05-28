# 04 — MT5 Integration

**Summary.** The MT5 side is a single MQL5 Expert Advisor (`ea/CopyTrades.mq5`, ~4100 LOC) that polls a FastAPI process on `http://127.0.0.1:8765` via `WebRequest()` every 1 second. There is no socket, no ZeroMQ, no MetaApi, no broker REST integration — the EA is the broker integration layer, and `WebRequest` over loopback HTTP is the entire bridge mechanism. The EA runs inside an MT5 terminal attached to a chart; the terminal owns the broker credentials and the MQL5 SDK does the OrderSend.

## The bridge mechanism (verified by reading code)

- **Loopback HTTP over `WebRequest()`** (`ea/CopyTrades.mq5` — `HttpPostJson`, `HttpPostJsonWithStatus` helpers).
- API URL is `http://127.0.0.1:8765` by default (EA input `ApiBaseUrl`), settable per chart attach. Must be added to MT5 → Tools → Options → Expert Advisors → "Allow WebRequest for listed URL".
- Shared-secret header `X-EA-Token: <ApiSharedToken>` on every request. When blank, the API runs unauthenticated (dev/loopback only — `_enforce_auth_bind_policy` warns if the API binds non-loopback without a token).
- **Persistent state between EA restarts**: MT5 `GlobalVariables` storage. `LoadPersistedPlans`, `LoadPersistedNaked`, `LoadPersistedPendingOrders` on `OnInit`; sister Persist* calls on every mutation.
- **Retry queue**: failed POSTs that should be safe to retry are enqueued as files under `MQL5\Files` (`EnqueueRetry`); next `OnTimer` tick attempts redelivery. Each retry filename includes `TimeCurrent() + g_retry_counter++` for uniqueness within one second.

## The EA's responsibilities

| Responsibility | Implementation | Hot path? |
|---|---|---|
| Poll work queue | `GET /actions?status=sent` on every `OnTimer` (default 1 Hz) | yes |
| Atomic claim | `POST /actions/{id}/claim` before OrderSend | yes |
| Execute action | `ExecuteOne(obj)` dispatcher → 13 `Do*` functions | yes |
| Report result | `POST /actions/{id}/result` with status + leg snapshot(s) | yes |
| Staged-partial-close + trailing SL | `ManagePlans()` every tick — see CLAUDE.md table | yes |
| Naked-position lifecycle | `ManageNakedPlans()` — OPEN_INSTANT timeout handling | yes |
| Pending-limit lifecycle | `ManagePendingOrders()` — broker BuyLimit/SellLimit fill detection + server-side cancellation | yes |
| DB reconciliation | `ReconcileClosedPositions()` every tick — close any DB-open ticket MT5 doesn't recognize | yes (cheap query) |
| Market price heartbeat | `HeartbeatMarketPrice()` every `MarketPriceHeartbeatSec` (default 15) — POSTs bid/ask | yes |
| Market snapshot | `PostMarketSnapshot()` every minute — multi-TF OHLC + ATR for the evaluator | medium |
| Evaluator fetch | `FetchLatestEvaluation()` every 5 s — dashboard widget data | medium |
| Broker compatibility check | `RunBrokerChecks(...)` once on `OnInit` (`ea/BrokerCheck.mqh`) — POSTs ALERT with results | once |
| Dashboard render | `g_dashboard.Update()` per tick, hash-gated (`ea/Dashboard.mqh`) | yes |

## Order types supported

`DoOpen` (`ea/CopyTrades.mq5:1432`) branches on `payload.pending`:

- **`pending=false`** (default): `trade.Buy` / `trade.Sell` at market. Two sub-paths:
  - Price IN entry zone (widened by `EntryPriceMargin=5.0`) → market fill.
  - Price PAST entry zone AND `ChasePriceEnabled` AND `remaining_reward/orig_reward >= ChaseMinRewardRatio` → chase, market fill at current price (`g_stats_chased++`).
  - Else → reject with `out_of_zone` (or if `SyntheticLimitEnabled`, transition to `watching` and wait for re-entry).
- **`pending=true`**: `trade.BuyLimit` / `trade.SellLimit` at entry-zone midpoint. Stored in `g_pending_orders[]`. Action stays `watching` until the limit fires or `CANCEL_PENDING` rejects it.
  - `pending_type="stop"` is **fallback-only** as of current code — logs and places a Limit, with a TODO comment ("real BuyStop/SellStop plumbing lands when a channel actually needs it"). `ea/CopyTrades.mq5:1495`.

`DoModify` (legacy `MODIFY`): modifies broker SL/TP on a specific ticket. `DoClose` / `DoCloseAll`: legacy ticket-targeted closes.

Management actions (no ticket) — `DoMoveSlBe`, `DoMoveSl`, `DoClosePartial`, `DoCloseFull`, `DoTightenSl`, `DoModifyTps`, `DoReopenLast`, `DoReinforce`: all resolve the singleton via `FindSingletonOpenTicket(Symbol_Override)` (`ea/CopyTrades.mq5:2682`); reject with `no_open_position` if none.

Naked / instant flow: `DoOpenInstant` (`ea/CopyTrades.mq5:3604`) → opens at market with an emergency SL sized to `InstantRiskPercent` of balance, no TP. `DoAttachSignal` later wires SL/TPs onto the same ticket. `ManageNakedPlans()` enforces the `InstantTimeoutMinutes` (default 5) → fallback TP at `InstantTpMultiplier × orig_sl_distance` and trailing SL at `InstantTrailPoints`.

## Staged-management policy (per signal TP count)

From `CLAUDE.md` and `ea/CopyTrades.mq5:222–238`:

| TPs | TP1 hit | TP2 hit | Final exit |
|---|---|---|---|
| 1 | — (no plan registered) | — | TP1 closes the position |
| 2 | Close 50%; SL → `TrueBreakEvenSl`; broker TP removed; trail starts | — | Trail SL hit on reversal |
| 3 | Close 25%; SL → BE; broker TP unchanged | Close another 25% of original; SL → TP1; broker TP removed; trail starts | Trail SL hit on reversal |

Trailing gap = `1.5 × ATR(M15, 14)`, with a fallback formula when the ATR handle isn't ready. Step threshold `5 × point`; ratchet-only.

`FinalStageMode` input toggles `FINAL_TRAIL` (default `FINAL_KEEP_TP`) — `FINAL_KEEP_TP` keeps the broker TP at the signal's final TP; `FINAL_TRAIL` removes broker TP and rides the trailing SL.

`ProgressiveLadderEnabled` (default true): a 1-TP signal whose entry→TP distance is ≥ `ProgressiveLadderMinR=3` × SL distance is synthesized into 3 virtual TPs at +1.5R / +3R / +final to enable the staged machinery.

## Handling adverse conditions

| Event | EA behavior | Verified at |
|---|---|---|
| Requote / market change between claim and OrderSend | `SlippagePoints=50` deviation accepted; otherwise broker retcode reported as `failed` | `trade.SetDeviationInPoints(SlippagePoints)` at OnInit |
| Partial fills | EA reports the broker's filled lots in `legs[].snapshot.volume` so the DB stores the actual filled volume; `original_volume = volume` snapshot is healed on the first /update that brings a topped-up fill (`src/api.py:617`) | api.py update_position |
| Rejected orders | `failed` result with `error="<retcode>"` posted; counted in `g_stats_rejected` | DoOpen, DoOpenInstant pending-place-failed branch |
| Connection loss to API | `HttpPostJsonWithStatus` returns false; for retryable statuses → `EnqueueRetry` to disk, retry on next tick | ea/CopyTrades.mq5 IsRetryableStatus |
| Terminal restart | `LoadPersistedPlans/Naked/PendingOrders` from MT5 GlobalVariables; reconcile on next tick | ea/CopyTrades.mq5 OnInit |
| Weekend / market closed | EA continues polling; broker reject becomes `failed`. **No explicit market-hours guard**. | inferred — no `MarketClosed` check found |
| Crashed EA between claim and result | `release_stale_claims` (bot) flips `claimed → sent` after 300 s; next EA poll re-claims and retries. Dedupe guard for pending orders prevents duplicate broker order (`ea/CopyTrades.mq5:1468`). | promoter.py + EA dedup |
| Two broker orders for one AI signal | Mitigation: API `post_result` only accepts `status IN ('claimed','watching')` so a late POST from the stale execution can't overwrite a fresher claim (REVIEW.md P0 fix). Still — the second OrderSend has already happened by then; only the audit row is protected. | src/api.py:452 |
| MT5 sees a position the DB doesn't (manual MT5 open) | EA's `ReconcileClosedPositions` only walks DB→MT5 direction; manual MT5 positions are tolerated but invisible to AI prompt | observed code |
| DB sees a position MT5 doesn't | `POST /positions/{t}/close` with `reason='mt5_not_found'` (DB-authoritative pass). | ea ReconcileClosedPositions |
| MT5-side partial deal misread as full close | Backstop in `api.close_position` refuses `reason='mt5_close'` when position is mid-partial state (`partial_close_count>0 AND volume<original_volume`) | src/api.py:723 |
| Brokers that require commission per side | `CommissionMultiplier=2.0` default; BE calculation accounts for this | EA input |

## Reconciliation logic

`ReconcileClosedPositions()` (`ea/CopyTrades.mq5:3083`) on every `OnTimer` tick:

1. **Recent-history scan** (48h): `HistorySelect` + `HistoryDealsTotal` + `DEAL_ENTRY_OUT` to detect broker-side closes.
2. **DB-authoritative pass**: `GET /positions?status=open`; any ticket MT5 doesn't recognize → `POST /positions/{t}/close` with `reason='mt5_not_found'`.

No throttle — must converge within one timer tick to avoid stale `open` rows confusing the dashboard and the AI prompt's state block.

The reconciler is the only safety net against the DB drifting from broker reality. There is no explicit nightly or weekly reconciliation report; observability is limited to the `trades.log` line per close.

## Signal-to-execution traceability

Magic number `Magic=919191` (default, overrideable per stack) tags every position opened by the EA. Combined with the `comment` field (`"ct-pending"` for pending limits, signal's `comment` for market opens), this lets `CountOurOpenPositions()` and `FindSingletonOpenTicket()` filter out unrelated orders on the same account.

The reverse direction (closed broker trade → originating Telegram message) goes through:
1. `positions.action_id` FK → `actions.id`
2. `actions.source_msg_id` FK → `messages.id`
3. `messages.tg_message_id` + `chat_id`

`GET /positions/last_closed` and `GET /positions/by_ticket/{t}` join through these and walk forward to `ATTACH_SIGNAL` if the originating action was an `OPEN_INSTANT` whose params materialized later (`src/api.py:810`, `src/position_signal.py`).

## Operational gotchas (from comments)

- MT5's `WebRequest` doesn't handle HTTP keep-alive cleanly — Uvicorn is run with `timeout_keep_alive=0` to force `Connection: close` per request (`src/api.py:1280`). Without this, WinError 10054 floods the API log and EA logs "HTTP 1003".
- `RegisterPlan` **dedupes by ticket** — without that guard, ManagePlans was firing each stage twice (over-closing 5.6 of 8.43 lots at TP1 instead of the intended 1/3). `ea/CopyTrades.mq5:1732`.
- The EA's heartbeat runs even when kill switch is on, so the AI prompt's MARKET freshness doesn't go STALE during a halt.
- Antivirus / WinHTTP friction on the loopback occasionally drops the response — visible as "HTTP 1003" in EA logs. The retry queue is the workaround.
- The 2026-05-27 incident referenced inline at `ea/CopyTrades.mq5:1461`: pending order action #25 placed two broker orders (8802699870 + 8802736674) one second apart because the API didn't get the watching POST, the action sat in `claimed`, got recycled, the EA re-fired DoOpen. The fix added a dedup loop in DoOpen scanning `g_pending_orders[]` by `action_id`. **This is a real money-impacting failure mode that recurred until very recently.**
