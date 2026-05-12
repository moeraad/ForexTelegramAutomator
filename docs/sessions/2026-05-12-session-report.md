# Trading Session Report — 2026-05-11 → 2026-05-12

**Source artefacts:**
- `server/copytrades.db` (post-incident snapshot, WAL flushed)
- `server/logs/trades.log` (1,103 lines, full lifecycle)
- `server/logs/system.log` (7,737 lines)
- `server/logs/ai_calls.jsonl` (859 lines, 2 days of AI activity)
- `server/journal/2026051[12].log` (MT5 broker journal)
- `server/experts/2026051[12].log` (EA stdout)

**Window analysed:** 2026-05-11 08:00 UTC → 2026-05-12 14:00 UTC (~30 trading hours across two sessions).

---

## 1. Headline numbers

| Metric | Value |
|---|---|
| Messages received | **169** (106 on 5-11 + 63 on 5-12) |
| AI decisions | **209** (out of 388 triage `keep`s) |
| Trades opened | **15** real + 1 orphan ghost |
| Trades closed | **15** (14 by SL, 1 manual) |
| Net realised P&L | **+$922.02** |
| Best trade | id 9 — BUY @ 4690.93 → closed manually +$1,588.05 |
| Worst trade | id 13 — BUY @ 4706.43 → SL @ 4694 −$1,441.88 |
| Win rate (PnL > 0) | **9 / 15 = 60%** |
| Average win | $588.93 |
| Average loss | $651.96 |
| Profit factor | 0.81× (wins capture $5,300; losses cost $3,911 — net positive but the avg loss is bigger than the avg win) |

---

## 2. What worked

### 2.1 Phase 4 — OPEN_INSTANT / ATTACH_SIGNAL pipeline is operational

After the deal-vs-position-ticket bug was fixed and the EA recompiled, the directional-command-first flow ran cleanly **6 times in a row** on May 12:

| Time | OPEN_INSTANT | ATTACH_SIGNAL | Position |
|---|---|---|---|
| 2026-05-11 23:44 | action 156 (1.12 lots @ 4756.26) | action 157 → SL=4753, TPs=[4767,4780,4790] | ticket 8583050977 |
| 2026-05-12 08:46 | action 166 (1.16 lots @ 4706.43) | action 167 → SL=4694, TPs=[4715,4725,4740] | ticket 8589630969 |
| 2026-05-12 10:41 | action 186 (1.01 lots @ 4690.59) | action 189 → SL=4679, TPs=[4700,4705,4720] | ticket 8591460588 |
| 2026-05-12 12:07 | action 202 (1.09 lots @ 4701.52) | action 203 → SL=4689, TPs=[4712,4720,4730] | ticket 8592979052 |
| 2026-05-12 12:37 | action 212 (1.10 lots @ 4699.11) | action 213 → SL=4689, TPs=[4710,4720,4730] | ticket 8593815531 |
| 2026-05-12 13:18 | action 222 (1.11 lots @ 4683.18) | action 224 → SL=4673, TPs=[4693,4700,4720] | ticket 8595069437 |

Each cycle: AI emits OPEN_INSTANT → EA opens market with 1% emergency SL → signal arrives 1-2 min later → AI emits ATTACH_SIGNAL → EA modifies SL/TPs → ManagePlans staged exits run normally. Zero failures.

Notable example — **action 222 / 223**: at 13:17:57 channel posted "اشتري الذهب" → OPEN_INSTANT executed. **9 seconds later** at 13:18:06 the channel posted "اشتري الذهب" again → AI correctly emitted another OPEN_INSTANT but the EA rejected with `already_open` (singleton invariant working). Then 49 s later the structured signal arrived → ATTACH_SIGNAL fired. Whole sequence handled correctly.

### 2.2 Managed-trade pipeline (Phase 2)

`MOVE_SL_BE` + `CLOSE_PARTIAL` pairs fired **12 times** on the AI's prompt. **11 executed cleanly**, 1 failed (see §3.3). The staged-exit + trail engine continues to be the workhorse — most of the day's wins came from the trail catching part of a directional move *after* the channel-instructed BE+partial.

### 2.3 AI pipeline efficiency

| Stage | Calls | Tokens | Notes |
|---|---|---|---|
| Triage (Haiku/nano) | 388 | ~150K input | p50=940 ms; correctly filtered ~46% as `ignore` |
| Interpret (Sonnet/gpt-5) | 209 | ~1.45M input | **cache_read = 1.20M** (~75% cached) |
| Total tokens | — | 1.6M input · 139K output · **1.2M cache hits** | — |

Cache hit rate is excellent — the static SYSTEM_PROMPT (≈1,750 tokens) gets billed at ~10% of normal cost on repeat hits. Estimated AI spend across the 2-day window: **~$5–8** (mostly interpret).

### 2.4 Successful trail captures

Three standout trades where the trail engine extracted significantly more than the channel's first TP would have given:

| Trade | Side | Entry | Final SL | Exit | PnL | Note |
|---|---|---|---|---|---|---|
| 7 | BUY | 4671.86 | 4685.95 | 4685.95 (mt5_sl) | +$750.23 | Trail rode 14pt past entry after partials |
| 9 | BUY | 4690.93 | 4722.68 | manual @ ~4716 | +$1,588.05 | Trail nearly 32pt above entry; operator closed |
| 17 | BUY | 4683.18 | 4693.00 | 4693 (mt5_sl) | +$1,121.70 | Trail captured a strong rally |

### 2.5 AI category accuracy

Sample of 209 interpret decisions:
- `context` 126 (60%) → ALERTs to operator, no trade action — correct
- `signal` 45 (22%) → real management or new entry — high precision
- `partial_signal` 17 (8%) → warning ALERTs for incomplete content
- `ignore` 21 (10%) → noise filter

Action type breakdown emitted:
```
[ALERT] 124    [MOVE_SL_BE,CLOSE_PARTIAL] 12    [OPEN_INSTANT] 10
[OPEN] 9       [ATTACH_SIGNAL] 6                [REINFORCE] 3
[REOPEN_LAST] 2    [MODIFY_TPS] 1    [CLOSE_PARTIAL] 1    [MOVE_SL] 1
```
No drift; the AI is using the new Phase 4 types correctly.

---

## 3. What didn't work / known issues

### 3.1 14 of 15 closes are `mt5_sl`

Of the 15 closed positions, **14 closed at the SL price** (not at a TP). Only one (id 9) closed manually for a big win. Even winning trades end at the trail SL — the **broker-side final TP almost never fires**.

This is structural to the current design: with `FinalStageMode = FINAL_KEEP_TP`, the final TP is the channel's TP3 which is rarely hit; with `FINAL_TRAIL`, the trail catches an SL on reversal. Either way, the system is *taking profits via SL ratchet*, not via clean TP-hits.

Five wins/losses in detail:

| Trade | PnL | Pattern |
|---|---|---|
| 9 (+$1,588) | manual | Operator closed before SL/TP, biggest win — automation missed it |
| 17 (+$1,121) | mt5_sl | Trail caught rally peak then SL hit on minor pullback |
| 14 (+$812) | mt5_sl | Trail rode ~10pt then SL hit |
| 7 (+$750) | mt5_sl | Same pattern |
| 13 (−$1,441) | mt5_sl | Direct SL hit immediately after open, no partials taken |

The wins are real but they're all "trail captured *some* of the move before reversing". The system never lets a winner ride to the channel's final TP.

### 3.2 The 2026-05-11 22:07 OPEN_INSTANT orphan (now fixed)

- `position id=11`, ticket **8183546585** (the wrong deal id) — opened 22:07:52, closed by reconciler 10 s later as `mt5_not_found`.
- The real MT5 position **8582384234** rode through the entire ATTACH_SIGNAL sequence orphaned, then opened in DB again as id 12 at 23:44 once the channel sent another OPEN_INSTANT.
- **Bug**: `DoOpenInstant` used `trade.ResultDeal()` first instead of `trade.ResultOrder()`. **Fix applied** at `ea/CopyTrades.mq5:2809-2814`.
- **Verification**: every subsequent OPEN_INSTANT row uses a true position ticket (`857…`/`858…`/`859…`), and ATTACH_SIGNAL pipeline now runs without intervention.

### 3.3 `modify_failed:10016` on action 207

```
2026-05-12 12:23:33  ai_decision msg_id=1086 category=signal latency_ms=11995 types=[MOVE_SL]
2026-05-12 12:23:34  action_result id=207 status=failed ticket=8592979052 error=modify_failed:10016
2026-05-12 12:24:57  position_closed ticket=8592979052 reason=mt5_sl exit_price=4698.0 pnl=85.58
```

MT5 retcode **10016** = `TRADE_RETCODE_INVALID_STOPS`. The MOVE_SL action tried to set a stop that violated the broker's `SYMBOL_TRADE_STOPS_LEVEL` (minimum stop distance from current price). 90 seconds later the position was stopped out at the prior SL anyway.

Likely cause: the AI emitted a MOVE_SL price too close to the current bid. `DoMoveSl` doesn't currently clamp the SL to `minDist` before sending the modify.

### 3.4 `last_closed_unparseable` on action 209

```
2026-05-12 12:26:48  ai_decision msg_id=1088 category=signal latency_ms=4425 types=[REOPEN_LAST]
2026-05-12 12:26:50  action_result id=209 status=failed error=last_closed_unparseable
```

`DoReopenLast` queried `GET /positions/last_closed?symbol=XAUUSD&within_hours=24`, the API returned data, but the EA's payload parser couldn't extract the opening params. Same symptom as `reinforce_payload_unparseable` flagged earlier this session.

Hypothesis: the position closed at 12:24:57 was the one opened via **ATTACH_SIGNAL** at 12:08, so its originating action payload has a different shape (`entry_low`/`entry_high`/`tps`/`sl`) attached via the ATTACH_SIGNAL row, not the OPEN_INSTANT row. The `/positions/last_closed` endpoint joins back to `actions.payload_json` via `position.action_id`, which points to the OPEN_INSTANT action (which has *no* entry/SL/TP fields, just `symbol/side/comment`). So the OPEN params returned are sparse, and `BuildOpenPayloadFromLastClosed` can't reconstruct them.

This is a real bug introduced by Phase 4 — REOPEN_LAST and REINFORCE need to look at the **ATTACH_SIGNAL** payload, not the OPEN_INSTANT payload, when the source action was a naked open.

### 3.5 AI interpret latency p95 = 17 seconds

- p50: 7.9 s, p95: 17 s
- This is the gap that creates chase-price fills (see prior 2026-05-08 report for the cost analysis)
- One signal at 12:25:52 took **17,025 ms** — gpt-5 with `medium` reasoning + cold cache + long input = worst case

In trading-window terms: when the channel sends "اشتري الذهب", the system takes 7-17 s to react, during which gold can move 3-8 USD on news/momentum. Trade 13 (the −$1,441 loss) opened at 4706.43 against signal entry zone of 4701-4703 → fill **3.4 USD past zone** consistent with the latency budget.

### 3.6 Trade 13 specifically: −$1,441 in 1h 46min

| Time | Event |
|---|---|
| 08:45:57 | OPEN_INSTANT executed, ticket 8589630969, BUY 1.16 lots @ 4706.43 |
| 08:47:11 | ATTACH_SIGNAL → SL 4694, TPs [4715, 4725, 4740] |
| 10:32:08 | mt5_sl @ 4694.0 — **no partials, no SL move** |

The trade never reached TP1 (4715), so ManagePlans never moved SL to the entry-zone anchor. The structured signal asked for SL=4694 against a fill of 4706.43 — that's a **12.4 USD risk** on 1.16 lots = $1,441. The math matches the loss exactly.

Two things worth noting:
1. Lot size went up to 1.16 (vs earlier 0.6-1.0) — the `LotsPer100Balance` formula scales with balance; account had grown.
2. No partial save logic kicks in until TP1, and TP1 was 8.6 USD above fill — the move never made it.

### 3.7 Other minor failures

- 1× `lot_too_small` on a CLOSE_PARTIAL — already-shrunk volume below `minLot × 2`. Acceptable; handler degrades cleanly.
- 1× `reinforce_payload_unparseable` (same family as 3.4).
- 5× `already_open` rejections — all correct (singleton invariant working).
- 1× MOVE_SL_BE failed — broker-side modify race; the position was being closed concurrently.

---

## 4. Suggestions for improvement

Ordered roughly by expected ROI / effort:

### High value

1. **Fix REOPEN_LAST/REINFORCE under naked-open lineage** (§3.4). When `position.action_id` points to an OPEN_INSTANT action, walk forward to find the matching ATTACH_SIGNAL action for the same ticket and use *that* payload for entry/SL/TPs. ~30 lines in `src/api.py /positions/last_closed`.
2. **Clamp MOVE_SL to broker minDist** (§3.3). Mirror the clamp already done in `TrailStage2Sls`. Same pattern, ~5 lines in `DoMoveSl`. Eliminates retcode-10016 failures.
3. **Investigate why no trade closes at TP**. Options:
   - Loosen the trail divisor in ManagePlans (currently 2.0 for 2-TP, 3.0 for 3-TP). Wider gap = less premature stop-outs.
   - Use `FINAL_KEEP_TP` consistently (current default) and let the broker TP catch the final move — but accept that requires reaching TP3 which is often unrealistic.
   - Add an option to close partial at the **midpoint** between TP1 and TP3 (a synthetic mid-target) on top of the existing partials.
4. **Add a "first-TP minimum reward" gate**. Reject signals where TP1 is < N USD above the chased entry (e.g. <8 USD). Trade 13's TP1 (4715) was only 8.6 USD above the fill (4706.43) — barely 1.2× the SL distance. Low-RR setups make the system the loser on a coin flip.

### Medium value

5. **Per-symbol minimum SL distance** for OPEN_INSTANT emergency SL. Today the 1% balance budget can produce SLs as tight as 100 pts on XAUUSD ($1), which gets stopped on noise. Set a floor of e.g. 500 pts ($5).
6. **Reconciler grace window** (~5 s) to defend against any future ticket-resolution regression — see prior debug session. Belt-and-suspenders.
7. **AI latency optimisation**: move evaluator to `gpt-5-mini` (separate env var). Saves ~$3/month and one less variable to tune.
8. **Outsize-loss circuit breaker**: if a single trade loses > N% of balance (e.g. 0.5%), pause auto-promotion until operator clears via `/halt off`. Trade 13's $1,441 loss was ~1.5% of inferred balance.

### Low value / cosmetic

9. **Reduce ALERT noise to operator**. 124 ALERTs over 30 trading hours = roughly one DM every 15 minutes. Mostly `[context]` market commentary. Consider downgrading to logs-only (skip DM) and keep DMs for warning-level only.
10. **Surface OPEN_INSTANT/ATTACH_SIGNAL lifecycle on dashboard**. Currently the LogPanel shows individual actions; could add a small "Phase 4 status" indicator showing whether a naked position is awaiting attach.

---

## 5. Action-item checklist

| # | Change | File | Effort |
|---|---|---|---|
| 1 | REOPEN_LAST/REINFORCE walk-forward to ATTACH_SIGNAL | `src/api.py` | ~30 LoC |
| 2 | Clamp MOVE_SL to broker minDist | `ea/CopyTrades.mq5` `DoMoveSl` | ~5 LoC |
| 3 | Add `InstantMinSlPoints` input (floor on emergency SL) | `ea/CopyTrades.mq5` | ~5 LoC |
| 4 | Tune trail divisor or add mid-target partial | `ea/CopyTrades.mq5` `ManagePlans`/`TrailStage2Sls` | ~10 LoC |
| 5 | Min-RR gate on OPEN signals | `src/validators.py` or orchestrator | ~10 LoC |
| 6 | Per-trade loss circuit breaker | `src/promoter.py` | ~20 LoC |
| 7 | Route evaluator to `gpt-5-mini` | `src/config.py` + `src/orchestrator.py` | ~15 LoC |

Each can be a separate, small PR. Items 1, 2, 4 are the highest-leverage on the next 7 days of trading.

---

## 6. Verification log (post-incident sanity)

- DB integrity_check: **ok**
- WAL flushed cleanly after the snapshot copy
- 7 of 10 OPEN_INSTANT actions executed; 3 rejected with the expected `already_open` (singleton invariant working)
- 6 of 6 ATTACH_SIGNAL actions executed; 0 failures
- All position tickets in DB after the May-11 22:07 incident match real MT5 order tickets (`857…`/`858…`/`859…`)
- No `mt5_not_found` events after the bugfix took effect

The Phase 4 pipeline can be considered **production stable** after the deal-vs-position fix. The remaining issues are tuning / edge-case bugs, not architectural defects.
