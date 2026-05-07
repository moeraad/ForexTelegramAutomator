# Trading-Session Report — 2026-05-07

Cross-referenced from `server/logs/trades.log` (system view),
`server/journal/20260507.log` (broker view), `server/expert/20260507.log`
(EA view), and the live `server/copytrades.db`.

---

## Trade-by-trade results

| # | Ticket | Open | Side / Size | Entry | SL | TP-final | Eval score | Outcome | P&L (est.) |
|---|---|---|---|---|---|---|---|---|---|
| 1 | 8526102573 | 08:29 | BUY 1.00 | 4741.50 | 4730 | 4780 | **74 / moderate** | Full SL hit at 4729.60 | **−$1,190** |
| 2 | 8526804766 | 09:09 | BUY 0.88 | 4732.63 | 4720→4740 | 4770 | **73 / moderate** | AI partial 0.44 @ 4737, stage-2 partial 0.29 @ 4752, trail SL caught remainder 0.15 @ 4739.84 | **+$869** |
| 3 | 8531279417 | 13:03 | BUY 0.97 | 4741.49 | 4731 | 4770 | **38 / avoid** | **Manually closed by operator** at 4749.92 (13:32) | **+$818** |
| 4 | 8532532660 | 13:47 | BUY 1.05 | 4747.52 | 4736→4747 | 4780 | **48 / weak** | AI partial 0.52 @ 4751, remainder 0.53 stopped at SL 4742.70 | **−$70** |
| 5 | 8535148131 | 15:32 | BUY 1.04 (REOPEN_LAST) | 4745.86 | 4736→**4732** | 4780→4770 | (REOPEN, not scored) | Full SL hit at 4731.91 | **−$1,451** |

**Net day P&L: ≈ −$1,024** (saved entirely by Trade 2's staged exit and
Trade 3's operator override).

EA balance log corroborates: 10057 → 8867 → ~9700 → 10554 → 10483 →
~9000.

---

## What worked ✅

### 1. Evaluator scoring is alive and provably useful

For the first time, all four OPEN actions got scored
(`data_quality=full` — the EA's `/market/snapshot` push is feeding the
evaluator real OHLC + ATR context).

The most consequential calibration data point of the day: **Trade 3
scored 38 / avoid with key_factor "Counter-trend buy with a tight SL
versus 1H volatility"** — the operator saw it on the dashboard and
manually closed at +$818 (13:32). If left to run, that trade was on the
wrong side of a falling H1; the AI's read appears to have been correct.
**Sample size 1, but the system did its job.**

### 2. Trade 2 stage-2 trail logic worked exactly as designed

Expert log walks the entire flow:

```
12:45:02  CT plan stage2 partial ok ticket=8526804766 lots=0.29 (vol 0.44 -> 0.15)
12:45:02  CT plan stage2 SL->BE+removeTp ok (broker TP removed; trailing SL now active)
12:45:02  CT plan stage2 trail SL ok 4732.63 -> 4740.06 (gap=12.46)
12:49:58  CT plan stage2 trail SL ok 4740.06 -> 4740.11 (gap=12.46)
13:01:31  CT reconcile: ticket=8526804766 absent from MT5  POSTed close (mt5_not_found)
```

TP2 hit → 1/3 partial + broker TP removed + trailing SL armed → trail
ticked up as price advanced → caught reversal at 4739.84, locking
~$108/oz on the remaining 0.15. **Two new features from yesterday's
session validated on a real trade.**

### 3. Plan-stage advancement on AI override

On both Trade 2 (09:26) and Trade 4 (14:13): `CT plan stage advanced
0->1 on AI MOVE_SL_BE — TP2 partial close still armed`. The change from
yesterday that preserves the staged plan after AI's half-and-BE override
fired correctly on both eligible trades.

### 4. AI prompt classification quality

- 5 OPENs correctly identified, all with structured `GOLD ❇️BUY❇️@…`
  blocks.
- Compound emits `[MOVE_SL_BE, CLOSE_PARTIAL]` on the right messages.
- One REOPEN_LAST correctly fired (Trade 5, after Trade 4's close).
- Two `MOVE_SL + MODIFY_TPS` compound emits on Trade 2 (the new
  "OPEN with position open" RULE C from the prior session) — both
  targeting the same already-open ticket.

### 5. Operator manual close cleanly tracked

Trade 3 closed manually at 13:32:17. Reconciler caught it, posted
`mt5_not_found`. No ghost positions.

---

## What's broken or concerning ❌

### 1. CRITICAL — `partial_did_not_execute retcode=10009 vol_unchanged=X` is back

Twice today on real trades:

- 09:26:57 Trade 2: `failed partial_did_not_execute retcode=10009
  vol_unchanged=0.88` — but the journal shows `market sell 0.44 XAUUSD,
  close #8526804766 ... done`, position genuinely went **0.88 → 0.44**.
- 14:13:05 Trade 4: same pattern. `vol_unchanged=1.05`, but journal
  shows `market sell 0.52 ... done`, **1.05 → 0.53**.

**This is the verify-then-advance race.** `PositionGetDouble(POSITION_VOLUME)`
after `PositionClosePartial` returns *stale* volume — MT5's local
position cache hasn't been updated with the fill yet. The previous fix
(commit `5be95bf`) handled the case where ResultRetcode lied; today's
symptom is a different race where the *volume read* is the lie.

**Concrete damage:**

- DB never increments `partial_close_count` for these trades.
- `state_summary` reports the AI sees `vol=0.88 of 0.88 orig,
  partials_taken=0` when the actual position is 0.44 with one partial
  done.
- Subsequent AI emits attempt to take "another half" and get
  `lot_too_small: close=0.44 remain=0.00` — **happened 6 times today**
  on Trade 2 (09:27, 09:43, 09:58, 10:44, 11:13, 11:26, 11:47, 11:48 —
  operator/AI repeatedly retrying).
- Dashboard SIGNAL QUALITY widget would have shown the wrong
  "remaining ride" expectations.

The actual money was made (broker partials filled) — but state tracking
is lying.

**Fix:** add a small retry loop in `DoClosePartial`, e.g., poll
`PositionGetDouble(POSITION_VOLUME)` up to 5× with 100 ms gaps before
declaring failure. Or: extract the deal ticket from `trade.ResultDeal()`
and verify via `HistoryDealGetDouble(deal, DEAL_VOLUME)`.

### 2. HIGH — Reconciler false-close races against position-open

Trade 3 (8531279417) timeline from logs:

```
13:03:08.800  position_opened ticket=8531279417 (orchestrator)
13:03:08.808  deal buy 0.97 XAUUSD at 4741.49 done (journal — order completed)
13:03:08.811  position_closed ticket=8531279417 reason=mt5_not_found  ← 11 ms after open!
13:03:08.815  CT reconcile: ticket=8531279417 absent from MT5 (expert log)
```

The EA's authoritative reconcile pass ran and `PositionSelectByTicket(ticket)`
returned false **7 ms after the deal completed**. The position table
hadn't been refreshed yet. DB marked it closed. Then the operator
manually closed the (still-actually-open) position at 13:32 → Telegram
REOPEN_LAST signals at 13:06 and 13:18 were rejected as `already_open`
because the EA's `CountOurOpenPositions()` correctly saw the position
still active.

**This is a new race introduced by the previous session's fix.** When
we removed the history-deal scan and relied solely on the authoritative
pass, we exposed the timing gap between MT5 fill confirmation and
position-table refresh.

**Fix:** in `ReconcileClosedPositions`, skip tickets whose DB
`opened_at` is < N seconds old (e.g., 10 s). Tiny code change,
eliminates the race.

### 3. HIGH — AI loosened the SL on Trade 5, which then hit

Trade 5 opened at 15:32 via REOPEN_LAST with SL=4736. At 15:33:23 the AI
emitted `MOVE_SL + MODIFY_TPS` (RULE C — new signal arrived while
position open):

```
position_sl_moved ticket=8535148131 sl_before=4736.0 sl_after=4732.0
```

**SL was loosened by $4** ($416 of additional risk on a 1.04-lot
position). Price then hit 4731.91, closing for **−$1,451** loss.

Had the SL not been loosened, the original 4736 would have closed at
≈ −$1,037. **The AI override cost $414.**

This is the SL-ratchet issue flagged at the end of the prior session
("RULE C should refuse to loosen — only tighten") and decided to keep
as-is. **First concrete evidence of the cost.** Sample size 1, but the
trade-off is now real, not hypothetical.

### 4. MEDIUM — Mystery SL modify to 4632.63 on Trade 2 (09:34)

Journal:

```
09:34:10  modify #8526804766 ... sl: 4732.63 -> sl: 4632.63   (-$100!)
09:34:20  modify #8526804766 ... sl: 4632.63 -> sl: 4720.00   (back near entry)
```

No corresponding entry in `trades.log` or expert log explaining where
4632.63 came from. Stage was 1, not 2 (so trail wasn't active). No AI
action between 09:27 and 10:09. The 10-second gap suggests something
programmatic, not manual. Without clear log breadcrumbs the source is
not traceable — recommend adding `Print` calls to every
`trade.PositionModify` call site so this can't happen silently again.

Net effect: brief SL displacement, then corrected. No money lost (price
didn't hit 4632.63), but it's an unexplained code path.

### 5. MEDIUM — Latency outliers

| msg_id | latency_ms | Type |
|---|---|---|
| 520 | 28,487 | MOVE_SL+MODIFY_TPS |
| 553 | 23,798 | REOPEN_LAST |
| 555 | 27,212 | partial_signal ALERT |
| **561** | **44,850** | context ALERT |

44.8 s for a context message is extreme. Likely OpenAI rate limit +
retry. Worth checking `ai_calls.jsonl` for the corresponding stage
entries — if it's the interpreter retrying, the retry policy may need
a tighter ceiling.

### 6. LOW — Lost close-reason granularity

All 5 today close as `mt5_not_found`. After yesterday's fix removing
the history-deal scan, `mt5_close` is no longer used; everything routes
through "not found". This is **safer** (no false-closes from partial
deals) but **loses information**: we can no longer tell SL hit vs TP
hit vs manual close vs stop-out from the close_reason alone.

Could be enhanced: in the reconciler, when posting `mt5_not_found`, do a
`HistoryDealSelect` for the position's most recent closing deal and
pass `DEAL_REASON` through to the API. ~15 lines.

---

## Calibration data — evaluator scores vs outcomes (n=4)

| Score | Verdict | Outcome | P&L per lot (rough) |
|---|---|---|---|
| 74 | moderate | Full SL | **−$1.19/oz × 1.0** |
| 73 | moderate | Multi-stage win | **+$0.99/oz × 0.88** |
| 48 | weak | Partial+SL | **−$0.07/oz × 1.05** |
| 38 | avoid | Manual close (saved) | **+$0.84/oz × 0.97** (counterfactual: likely loss if held) |

Sample is too small to validate calibration but the **38 →
operator-saved-trade** pairing is encouraging. After 2 more weeks, the
calibration query in `AI_EVALUATOR_ROADMAP.md:Phase 1.1` will tell you
whether the score-to-outcome correlation is real or noise.

---

## Recommended priority order

1. **Fix #1 (partial-detection race)** — the most damaging because it
   corrupts state for downstream AI decisions. Quick fix: 5 × 100 ms
   retry loop or use deal-ticket verification.
2. **Fix #2 (reconciler-vs-open race)** — easy fix, eliminates the
   false-close that bit Trade 3.
3. **Fix #6 (close-reason granularity)** — small enhancement that pays
   off forensically forever.
4. **Reconsider #3 (SL ratchet in RULE C)** — opted out previously;
   today is one data point of cost. Up to you whether one example
   moves the needle.
5. **Investigate #4 (mystery SL modify)** — add `Print` calls so it's
   traceable next time.
6. **#5 (latency)** — only worth chasing if it correlates with missed
   fills, which it didn't today.
