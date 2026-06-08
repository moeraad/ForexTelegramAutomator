# Manual Trade Page — Design

**Date:** 2026-06-09
**Status:** Approved (design) — pending implementation plan
**Author:** brainstorming session

## Goal

Add a **Manual Trade** tab to the PySide6 GUI that shows a live candlestick
chart mirroring the broker feed in MT5. The operator arms an order, places three
draggable lines on the chart (entry / TP / SL), fills a small form (direction,
lot-per-$100, risk cap), and clicks Execute. The trade is validated, lot-sized,
and injected into the **existing execution pipeline** as a `status='sent'` OPEN
action flagged as **manual**, so the EA picks it up on its next poll exactly like
a Telegram-derived signal — but without the AI/orchestrator in the loop.

## Hard constraints / context

- **Single symbol (XAUUSD), single open position** invariants still apply. A
  manual OPEN is subject to the same EA-side guards as any other OPEN.
- **No OHLC candle history exists in the system today.** The EA only POSTs live
  bid/ask every 15s (`/market/price`) and a single latest M15/H1/H4 aggregate
  snapshot (`/market/snapshot`). A candle *series* must be added.
- GUI is **PySide6 (Qt)** desktop; `pyqtgraph` and `QtCharts` are both available.
  `pyqtgraph` is used for the interactive chart (better crosshair / click /
  draggable-line support).
- `api.py` is the validated SQL gateway ("api.py is dumb" — HTTP↔SQL only). All
  writes go through it; no business logic added there.

## Decisions (from brainstorming)

| Decision | Choice |
|---|---|
| Chart data source | **EA pushes candles** (most faithful — same broker feed as MT5) |
| Platform | **New Qt tab** in the existing GUI, pyqtgraph chart |
| Lot math | **Per-$100 sizes it, risk cap limits it** (cap is a safety brake) |
| Direction | **Operator picks BUY/SELL in the form**; TP/SL lines auto-labeled |
| Order type | **Inferred from entry vs live price** (at price → market; away → limit) |
| Execution path | **Confirm dialog → insert at `status='sent'`** (immediate, no promoter delay) |
| Manual marker | **`source_msg_id=NULL` + `manual:true` payload flag** (no schema migration) |
| Candle timeframe | **Default M15**, dropdown to choose M15 / H1 / H4 |
| Risk cap units | **% of balance** (consistent with existing `max_sl_loss_percent`) |

## Architecture

### Data flow

```
EA CopyRates(symbol, tf, 0, N)
   → POST /market/candles            (settings JSON blob per symbol+tf)
   → GUI poll GET /market/candles    (QTimer)
   → ChartPanel draws candlesticks + live price line

Operator: arm → place 3 lines + form → compute lot → Execute → confirm dialog
   → POST /actions/manual            (validate OpenAction, insert status='sent')
   → EA polls GET /actions?status=sent
   → DoOpen → normal lifecycle (claim → result → position)
   → single TP ⇒ no TradePlan registered; broker SL+TP ride to closure
```

### 1. EA side — candle feed (new)

`ea/CopyTrades.mq5`:

- New `PublishCandles()` called on `OnTimer`, **unconditionally** (like
  `HeartbeatMarketPrice` — chart stays live even while halted).
- `CopyRates(Symbol_Override, tf, 0, CandleCount, rates)` → POST the bar array.
- New inputs:
  - `CandlePublishEnabled` (default `true`)
  - `CandleTimeframe` (default `PERIOD_M15`)
  - `CandleCount` (default `200`)
  - `CandlePublishSec` (default `5`)
- The published timeframe must cover the GUI's selectable set (M15/H1/H4). v1:
  the EA publishes a **single configured timeframe**; when the operator changes
  the GUI dropdown to a timeframe the EA isn't currently publishing, the chart
  shows a "feed is on <TF>; switch EA `CandleTimeframe`" notice rather than
  silently showing nothing. (Multi-timeframe simultaneous publish is a v2
  fast-follow — see Open items.)
- Price heartbeat (`HeartbeatMarketPrice`) extended to also push
  `contract_size` and `tick_value` (from `SymbolInfoDouble`) so GUI lot-sizing
  is broker-accurate; GUI falls back to constant `100` for XAUUSD if absent.

### 2. API side (`src/api.py`, `src/api_models.py`)

- **`POST /market/candles`**
  Body: `{symbol, timeframe, bars:[{t,o,h,l,c,v}, ...]}` (Pydantic model in
  `api_models.py`). Validates `symbol in SUPPORTED_SYMBOLS`. Stores as a JSON
  blob in `settings`: `market_candles_{SYM}_{TF}` = JSON bars, plus
  `market_candles_{SYM}_{TF}_at` = ISO-8601 UTC timestamp. Matches the
  `/market/snapshot` storage pattern — **no schema migration**, bounded array.
- **`GET /market/candles?symbol=XAUUSD&timeframe=M15`**
  Returns `{symbol, timeframe, bars:[...], at, stale: bool}` (`stale` when
  `at` older than a threshold, default 120s). Empty/absent → `bars:[]`,
  `stale:true`.
- **`POST /actions/manual`**
  Body: a manual OPEN request (side, entry, sl, tp, lot, pending, comment).
  - Builds an OPEN payload and validates via the existing
    `validators.OpenAction` / `validate_action`.
  - Inserts into `actions` with `action_type='OPEN'`, `status='sent'`,
    `source_msg_id=NULL`, `execute_after=now`, `created_at=now`, and
    `payload_json` carrying `manual:true`, `source:"manual_gui"`, and an
    explicit `lot` (the GUI-computed final lot — bypasses the EA's own risk
    sizing, like `resize_pending` does for pending orders).
  - Returns `{action_id, status:"sent"}`.
  - Rejects malformed payloads with 422 (reusing the existing validation
    error path).

> Lot override: the OPEN payload already supports an explicit lot in the resize
> path. The manual action sets the lot directly so the chart-driven sizing is
> authoritative; the EA must honor a payload `lot` on a manual OPEN rather than
> recomputing from its own risk inputs. (Confirm exact EA field name during
> planning; align with the `pending_lot_override` precedent.)

### 3. GUI — new tab

New files under `src/gui/views/` and a small sizing helper, each kept <800 lines:

- **`manual_trade_view.py`** — top-level `ManualTradeView(QWidget)`: composes the
  chart + form, owns the poll `QTimer`, wires line-drag → form recompute.
- **`manual_trade_chart.py`** — `ChartPanel`:
  - Candlesticks from `GET /market/candles` (custom pyqtgraph
    `GraphicsObject`, themed via `_chart_theme` palette).
  - Live price line from `/market/price` heartbeat.
  - Crosshair + price/time readout.
  - Timeframe dropdown (M15 / H1 / H4); changing it re-fetches.
  - **3 draggable labeled `InfiniteLine`s** (entry / TP / SL) that appear when
    "Place order" is armed, positioned at click points, colored distinctly,
    each emitting value changes to the form.
- **`manual_trade_form.py`** — `OrderFormPanel`:
  - Inputs: direction toggle (BUY/SELL), `lot per $100`, `risk cap (% balance)`.
  - Read-only computed: entry, SL, TP, SL distance, order type (market/limit),
    **final lot**, **$ risk at SL**, **risk as % of balance**.
  - Execute button (disabled until valid).
- **`manual_trade_sizing.py`** — pure, unit-tested:
  ```
  risk_cap$     = balance × risk_cap_pct / 100
  base_lot      = (balance / 100) × lot_per_100
  sl_distance   = |entry − sl|                       # must be > 0
  cap_lot       = risk_cap$ / (contract_size × sl_distance)
  final_lot     = clamp(round_down(min(base_lot, cap_lot), lot_step),
                        lot_min, lot_max)
  risk_at_final = final_lot × contract_size × sl_distance
  ```
  Returns a dataclass with `final_lot`, `base_lot`, `cap_lot`, `risk_at_final`,
  `risk_pct_of_balance`, and a `blocked` reason string when `final_lot < lot_min`.
- **`manual_trade_submit.py`** — pure helpers:
  - `assign_sl_tp(side, entry, line_a, line_b)` → `(sl, tp)`:
    BUY ⇒ TP = line above entry, SL = line below; SELL ⇒ reversed. Raises if the
    two lines don't straddle entry.
  - `infer_order_type(entry, live_price, tol)` → `pending: bool`.
  - `build_manual_open_payload(...)` → dict.
  - `submit(api_base, payload)` → `POST /actions/manual`.

Tab registered in `main_window` nav alongside the existing views.

### 4. Marking & display

- Payload: `{type:"OPEN", symbol, side, entry_low=entry_high=entry, sl,
  tps:[tp], comment:"manual", pending:<bool>, lot:<float>, manual:true,
  source:"manual_gui"}`.
- `actions_table` / `actions_model`: render a **MANUAL** badge when
  `payload.manual` is set.
- `telegram_format` / notification rendering: prefix manual trades (e.g.
  "🛠 MANUAL") so DMs are unambiguous.
- `source_msg_id IS NULL` already distinguishes manual rows in reporting queries.

### 5. Lifecycle / staged-close

A manual trade carries **one TP** (one line). Per the EA staged-close policy,
1-TP signals register **no `TradePlan`** — the broker SL+TP ride to closure.
Expected behavior; multi-TP manual trades are out of scope (v2).

## Error handling

- **Validation (form/submit):**
  - The two non-entry lines must straddle entry for the chosen side → else block.
  - `sl_distance > 0` required.
  - `final_lot ≥ lot_min` required → else block with
    "risk cap too low / balance too small for min lot".
- **Freshness:** stale `account_balance`, stale/missing candle feed, or stale
  live price → visible warning and Execute disabled.
- **Execute:** confirm dialog summarizes side, lot, entry, SL, TP, $ risk,
  risk %; on cancel, nothing is sent.
- **API failure:** error toast; the form and placed lines are preserved.

## Testing

- **TDD unit (core logic):**
  - `manual_trade_sizing`: per-100 vs cap selection, lot-step rounding,
    min-lot clamp/block, zero/!straddle SL distance, % conversions.
  - `manual_trade_submit`: `assign_sl_tp` for BUY/SELL incl. non-straddle error;
    `infer_order_type` boundaries; payload shape (manual flag, single TP,
    entry_low==entry_high).
- **API:**
  - `POST/GET /market/candles` round-trip + staleness flag.
  - `POST /actions/manual` inserts a row with `status='sent'`,
    `source_msg_id IS NULL`, `manual:true`, explicit lot; rejects bad payloads
    (422); rejects non-XAUUSD symbol.
- **GUI smoke** test in `tests/gui/` (instantiate the view headless, matches
  existing GUI smoke tests).
- **EA:** manual verification on a **demo account** (MQL isn't unit-testable):
  candle publish lands, manual OPEN at `status='sent'` fills with the
  GUI-specified lot, SL/TP correct, single-TP closure rides broker SL/TP.

## Open items / v2 fast-follows

- **Multi-timeframe candle publish:** v1 EA publishes one configured timeframe;
  GUI dropdown beyond it shows a notice. v2: EA publishes M15+H1+H4 so the
  dropdown is fully live without an EA input change.
- **Multi-TP manual trades** (staged closes) — v2.
- **EA lot-override field name** for manual OPEN — confirm/align with
  `pending_lot_override` precedent during planning.
- **`contract_size`/`tick_value` push** from the EA — if deferred, GUI uses the
  XAUUSD constant `100`; design tolerates absence.
