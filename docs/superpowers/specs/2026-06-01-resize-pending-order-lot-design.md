# Resize a Pending Order's Lot Size from the GUI

**Date:** 2026-06-01
**Status:** Approved (design)
**Scope:** Pending (unfilled) XAUUSD orders only — `actions.status = 'watching'`, `action_type = 'OPEN'`, `payload.pending = true`.

## Problem

When the EA places a pending limit order, the lot size is computed deterministically by
`LotsFromRisk()` (balance policy × `LotsPer100Balance`, then capped by `max_sl_loss_percent`).
A tight signal SL can shrink the lot far below what the operator wants (observed: a 7.23-point
SL produced **0.06 lots** on a ~$4,300 account where the operator expected ~0.43).

MT5 cannot modify a pending order's volume in place — the only mechanism is *delete + re-place*.
Doing this by hand in the MT5 terminal **breaks app↔EA tracking**: a hand-placed order carries
magic `0`, but the EA only adopts magic `919191` (`FindSingletonOpenTicket`, reverse-reconcile
recover pass). The result is an orphaned position the app never sees.

We need an **application-driven** way to change a pending order's lot that keeps the action
lifecycle and EA tracking continuous.

## Goals

- Operator can set an explicit new lot for a `watching` OPEN from the desktop GUI.
- The broker order is re-placed at the new lot with the **same** entry / SL / TPs and the EA's
  magic, so tracking is never lost.
- The override **bypasses the per-trade risk cap** (that is the operator's intent), but is
  clamped to broker constraints and accompanied by a non-blocking risk warning.
- Single action row, single continuous lifecycle — only the broker ticket swaps.

## Non-Goals (YAGNI)

- Resizing an already-**filled** position (separate future feature — harder: MT5 can't grow a
  position in place; netting/hedging divergence; staged-TP `original_volume` re-basing).
- Modifying entry / SL / TP of a pending order (lot only).
- A Telegram surface (GUI only for v1).

## Architecture & Data Flow

```
DetailPanel (a 'watching' OPEN action is selected)
  → POST {Stack.api_base}/actions/{id}/resize_pending  {lots: 0.43}
  → API validates; writes payload.pending_lot_override + bumps payload.resize_seq
     (status stays 'watching'); returns {lots, risk_pct_estimate}
  → EA ManagePendingOrders() 10s poll — GET /actions/{id} (already runs):
       step 1. filled?      → handle fill, ignore resize (a position now exists)
       step 2. disappeared? → POST rejected ("pending_order_disappeared")
       step 3. rejected?    → delete broker order
       step 4 (NEW). resize_seq > appliedResizeSeq?
                → feasibility-check new lot (OrderCalcMargin)
                → if infeasible: POST failed reason, LEAVE existing order intact
                → if feasible: delete broker order, re-place at new lot
                  (same entry/SL/TP) via PlacePendingLimit(), update
                  g_pending_orders[i].order_ticket + appliedResizeSeq,
                  re-POST 'watching' with the new ticket
```

The EA already reads the full parsed `payload` on its existing `GET /actions/{id}` poll
(`api.py:get_action` returns `payload`), so **no new EA request** is introduced — only new
fields parsed from the response it already fetches.

## Components & Changes

### 1. `src/api.py` — new endpoint

`POST /actions/{action_id}/resize_pending`, body `ResizePendingBody { lots: float }`.

Validation:
- Action exists → else `404`.
- `status == 'watching'` AND `action_type == 'OPEN'` AND `payload.get('pending') is True`
  → else `409` (with a clear detail string).
- `lots > 0` AND `lots <= max_open_lots` (from `settings.risk_budget.max_open_lots`,
  fallback to a constant ceiling) → else `422`.

Effect (atomic write of `payload_json`):
- `payload['pending_lot_override'] = lots`
- `payload['resize_seq'] = int(payload.get('resize_seq', 0)) + 1`
- `status` unchanged (stays `watching`).

Response: `{ "lots": lots, "resize_seq": n, "risk_dollars": d, "risk_pct_estimate": pct | null }`.
`risk_dollars` is always computable from the action's `sl`/`entry` and the contract size
(XAUUSD: `$/lot ≈ slDistance × 100`, mirroring the EA's `LotsFromRisk` dollar-at-risk math).
`risk_pct_estimate` = `risk_dollars / balance × 100` **only when** a fresh `account_balance`
is available (see heartbeat extension below); otherwise `null` and the GUI shows absolute dollars
plus "balance unknown".

Helper: `_pending_risk(lots, entry, sl, balance) -> (dollars, pct | None)` (pure, unit-tested).

### 1b. `src/api.py` + EA — balance heartbeat extension (prerequisite for the % warning)

The DB currently stores no account balance — the EA reads `ACCOUNT_BALANCE` locally and the
`/market/price` heartbeat posts only bid/ask. To express the warning as "% of balance / above
the cap", piggyback balance/equity onto that **existing unconditional 15s heartbeat**:

- EA `HeartbeatMarketPrice()`: add `account_balance` + `account_equity`
  (`AccountInfoDouble`) to the POST body.
- `POST /market/price` handler: persist them to `settings` keys `account_balance`,
  `account_equity`, `account_at` (ISO-8601 UTC), alongside the existing market keys.
- The resize endpoint reads `account_balance`; if `account_at` is older than the market STALE
  window (60s) or missing, treat balance as unavailable → `risk_pct_estimate = null`.

This is a minimal, broadly useful addition (the Risk view can surface live balance too) and keeps
the EA's "heartbeat runs even while halted" property.

### 2. EA `ea/CopyTrades.mq5`

- **Refactor:** extract the pending-placement block currently inline in `DoOpen` (limit-price
  resolution, `MaybeSynthesizeLadder`, `OrderSend` BuyLimit/SellLimit, `g_pending_orders` push,
  `watching` POST) into:
  `bool PlacePendingLimit(long id, bool isBuy, double entryLimit, double sl, double &tps[], int tpCount, double lots, ulong &outTicket)`.
  `DoOpen` calls it for the initial placement; the resize path reuses it. Lot is clamped to
  `SYMBOL_VOLUME_MIN/STEP/MAX` and run through existing broker checks inside the helper.
- **Struct:** add `int appliedResizeSeq;` to `PendingOrder` (init `0` on push).
- **`ManagePendingOrders` step 4:** after the existing fill/disappeared/rejected checks, parse
  `pending_lot_override` and `resize_seq` from the polled payload. If
  `resize_seq > p.appliedResizeSeq`:
  - Compute the broker-clamped target lot; feasibility-check via `OrderCalcMargin` against free
    margin.
  - Infeasible → `PostResult(id, "failed", 0, "resize_insufficient_margin")`, set
    `appliedResizeSeq = resize_seq` (so it isn't retried forever), keep the existing order.
  - Feasible → `trade.OrderDelete(old)`; on success call `PlacePendingLimit(... newLot ...)`;
    update `order_ticket`, set `appliedResizeSeq = resize_seq`, re-POST `watching` with the new
    ticket. If `OrderDelete` fails → log + alert, leave state, retry next tick (don't bump seq).
    If place fails *after* a successful delete → enqueue/retry from stored struct params next
    tick + alert (don't bump seq).

### 3. `src/gui/panels/detail_panel.py`

- When the selected action is a `watching` OPEN, render a **Resize** row: current lot label,
  an editable lot input (broker-step aware), an **Apply** button, and a warning label.
- Warning label uses the endpoint's `risk_dollars` / `risk_pct_estimate`: turns amber and shows
  "~X% of balance (above the Y% cap)" when `risk_pct_estimate` exceeds `max_sl_loss_percent`;
  falls back to "risks ≈ $D if SL hits (balance unknown)" when `risk_pct_estimate` is null.
  Non-blocking either way.
- **Apply** POSTs to `{Stack.api_base}/actions/{id}/resize_pending`; button disabled while the
  request is in flight; on success shows the returned lot/seq; on API error shows the detail
  string inline. The row hides/disables itself once the action leaves `watching` (e.g., fills).
- Use the same HTTP client already used elsewhere in the GUI/services layer for API calls.

## Lot Semantics

Explicit operator value, **bypasses the 1% per-trade risk cap**. Guards:
- API: reject `≤ 0` and `> max_open_lots`.
- EA: clamp to broker `VOLUME_MIN` / `VOLUME_STEP` / `VOLUME_MAX`; broker checks enforced in
  `PlacePendingLimit`.
- GUI: non-blocking risk warning above the cap.

## Error Handling

| Failure | Behavior |
|---|---|
| Unknown action id | API `404` |
| Action not a `watching` pending OPEN | API `409` + detail |
| `lots ≤ 0` or `> max_open_lots` | API `422` |
| New lot infeasible (margin) | EA POSTs `failed` reason, **existing order untouched** |
| `OrderDelete` fails | EA logs/alerts, leaves order, retries next tick (seq not bumped) |
| Place fails after delete | EA retries from stored struct params next tick + alert |
| Re-applying same `resize_seq` | No-op (idempotent) |

## Edge Cases

- **Fill mid-resize:** fill check (step 1) precedes resize (step 4); a just-filled order is
  handled as a fill, resize ignored (pending-only scope). GUI disables the row once filled.
- **Rapid re-resizes:** monotonic `resize_seq`; EA applies the latest.
- **Resize after expiry/rejected:** API `409`.

## Testing

Hermetic (API):
- happy path: override + seq written, `status` stays `watching`, response includes lots/seq/risk.
- rejects non-`watching`, non-`OPEN`, missing `payload.pending`.
- rejects `lots ≤ 0` and `lots > max_open_lots`.
- `resize_seq` increments monotonically across repeated calls.
- `_pending_risk` unit tests: known dollar math; `pct` populated with fresh balance, `null` when
  `account_balance` missing or `account_at` stale.
- `POST /market/price` persists `account_balance`/`account_equity`/`account_at` when present and
  is backward-compatible when those fields are absent (older EA build).

Integration:
- Mock-EA loop: poll `GET /actions/{id}`, observe override, simulate re-place by POSTing
  `watching` with a new ticket; assert single-row lifecycle continuity (one `actions` row, ticket
  changed, status still `watching`).

EA (MQL5): validated by code review + manual `scripts/test_ea_signal.py`-style injection. No
MQL5 unit harness exists — documented limitation; keep the EA delta minimal and well-logged.

## Conventions

- Timestamps remain ISO-8601 UTC with explicit `+00:00`.
- `api.py` stays "dumb": it records the override; the EA owns the broker mechanics.
- Reuse `PlacePendingLimit` for both initial and resize placement (DRY) — `RegisterPlan`'s
  ticket-dedupe and the post-fill plan registration are unchanged.
