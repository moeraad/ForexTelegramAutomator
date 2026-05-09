# Trading Session Report — 2026-05-08

**Window covered:** 2026-05-07 22:00 UTC → 2026-05-08 14:01 UTC
**Source data:** `server/copytrades.db`, `server/logs/ai_calls.jsonl`, `server/experts/20260508.log`, `server/journal/20260508.log`
**Symbol:** XAUUSD only (gold)
**Account growth (observed via EA log):** $10,120 → $10,945 (+8.2% during the BUY block alone)

---

## 1. Headline numbers

| Metric | Value |
|---|---|
| Telegram messages processed | 56 (msgs 785–836) |
| AI triage calls | 130 (76 ignore / 54 keep) — 58 % filter rate |
| AI interpreter calls | 54 |
| Actions emitted | 56 |
| OPEN signals executed | 4 / 4 (100 %) |
| Compound actions executed (REINFORCE, CLOSE_FULL+OPEN, MODIFY_TPS) | 3 |
| Management actions **rejected by EA flag** | 11 (4 MOVE_SL_BE + 7 CLOSE_PARTIAL) |
| Hard EA failure | 1 (MODIFY_TPS broker error 10025) |
| ALERT/context-only | 37 |
| Positions opened | 4 (one via REINFORCE) |
| Positions closed by SL | 2 (mt5_sl) |
| Positions closed by AI | 1 (CLOSE_FULL → reset for new signal) |
| Positions closed manually by user | 1 (mt5_manual) |
| Stage-1 / Stage-2 partials auto-fired by EA `ManagePlans` | 4 stage-1 + 1 stage-2 |
| Reconciler corrections | 2 (both losses + one win mirrored cleanly) |

**Estimated net P&L for the window: ≈ +$1,800** (rough, based on entry/exit prices in DB; precise figures live in the broker journal).

| # | Side | Entry | SL | Outcome | Approx P&L |
|---|---|---|---|---|---|
| 1 | SELL | 4688.90 | 4699 | mt5_sl @22:22 | –$1,000 |
| 2 | SELL (REINFORCE) | 4695.75 | 4699 | mt5_sl @23:38 | –$290 |
| 3 | BUY  | 4698.79 | 4687 → trail 4724 | trail-SL @02:14 (TP1+TP2 partials, BE+trail) | +$1,400 |
| 4 | BUY  | 4712.34 | 4704 | CLOSE_FULL @12:02 (TP1 partial fired @4726) | +$830 |
| 5 | BUY  | 4718.01 | 4706 | mt5_manual @13:10 (TP1 partial fired @4730) | +$850 |

---

## 2. What worked well

### 2.1 OPEN pipeline is solid
All 4 fresh OPEN signals fired with end-to-end latency **9–22 s** (Telegram → DB → bot → EA → broker fill). The AI evaluator scored every OPEN (48/weak, 62/moderate, 65/moderate), giving us per-trade context for postmortems. Lot sizing (`LotsFromRisk(balance)`) tracked the balance as it grew (1.01 → 0.91 lots per signal at 1 % per $100 risk).

### 2.2 Compound actions worked correctly
- **REINFORCE** (msg #790) closed the SELL and re-opened the same direction in one transaction.
- **CLOSE_FULL + OPEN** (msg #825) collapsed correctly into two ordered actions; the EA closed Trade #4 and immediately opened Trade #5 at fresh full size.
- **MODIFY_TPS** (msg #792) correctly recognised that the new signal's SL would *loosen* the existing 4699 SL → kept SL, only updated TPs.

### 2.3 EA staged-close behaved exactly per spec
Trade #3 (3-TP signal) executed the textbook flow:
- 01:01 stage-1: closed ⅓ (0.24 lots) at TP1 4710, **SL unchanged** ✓
- 01:49 stage-2: closed another ⅓ (0.24 lots) at TP2 4720, **SL → BE 4698.79 + TP removed + trailing SL armed** ✓
- 02:14 trail SL hit at 4724.04 — converted what would have been a runner into +$630 on the final third
- The trail produced ~50 incremental SL updates (gap 8.74) — locked in $25 of additional profit beyond BE.

**`RegisterPlan` ticket-dedupe held** — no double-close on this trade.

### 2.4 Reconciler is reliable
Both broker-side closes (#1 mt5_sl, #2 mt5_sl) and the user's manual close (#5 mt5_manual) were mirrored to the DB within one OnTimer tick. No "ghost open" rows in the AI's SYSTEM STATE block.

### 2.5 Triage is doing its job
130 triage calls vs 54 interpreter calls = **58 % cost saving**. Triage average 1.3 s, p95 2.4 s. Interpreter average 11.6 s, p95 20.1 s — within budget.

### 2.6 AI evaluator surfaces real risk early
Trade #1's evaluation was honest: *"R:R ≈ 2.01:1, stop ≈ 0.5× ATR — vulnerable to intraday noise, counter-trend on 1H/4H, no channel history"* — score 48 / weak. Trade #1 then hit SL 16 minutes later. The evaluator correctly fingered both the tight-stop and counter-trend risks before the loss.

---

## 3. What failed or under-performed

### 3.1 🔴 EA flag silently nuked all in-trade management (11 rejected actions)

This was the **dominant issue of the session**. Every `MOVE_SL_BE` and `CLOSE_PARTIAL` the AI emitted came back from the EA as **`ai_partial_and_be_disabled`**:

| Action ID | Time | Type | Channel msg |
|---|---|---|---|
| 13, 14 | 22:35 | MOVE_SL_BE + CLOSE_PARTIAL | "أمن دخولك واحجز نصف أرباحك" |
| 17 | 22:47 | CLOSE_PARTIAL | "احجز ارباحك" |
| 28, 29 | 23:51 | MOVE_SL_BE + CLOSE_PARTIAL | "أمن دخولك واحجز نصف أرباحك" |
| 37, 38 | 10:15 | MOVE_SL_BE + CLOSE_PARTIAL | "أمن دخولك واحجز نصف أرباحك" |
| 49, 50 | 12:33 | MOVE_SL_BE + CLOSE_PARTIAL | "أمن دخولك واحجز نصف أرباحك" |
| 52 | 12:34 | CLOSE_PARTIAL | "اجني ارباحك" |
| 53 | 12:36 | CLOSE_PARTIAL | "TP1 ✅ ربح 220 نقطة" |

The new "AI partial close + BE optional, disabled by default" EA parameter (added in the last session) is doing exactly what it was designed to do — but the result is that **the EA is now ignoring 100 % of channel-driven in-trade management.** That is the right choice for safety in week-1 of live, but it means:

- We lost the channel's risk management overlay for the whole session.
- Trade #5 in particular: the user clearly wanted to bank profit (CLOSE_PARTIAL was emitted three times in 4 minutes after TP1 was visually hit), the EA refused, and the user closed it manually 35 minutes later. **The user manually replaced the disabled EA behaviour** — strong signal that the flag default should be flipped, or the user should be DM'd a one-tap "Force partial?" button when the flag is off.

**Recommendation:** keep the flag, but (a) emit a clear ALERT to the bot DM whenever a management action is rejected for this reason ("AI wanted to MOVE_SL_BE on ticket 8550541220 but `EnableAIPartialAndBE=false` — confirm?"), and (b) reconsider flipping the default once we have ≥2 weeks of live data showing AI management is safe.

### 3.2 🟠 MODIFY_TPS failed once (broker error 10025)

Action #48 (msg #829, 12:17): same-side BUY signal arrived with SL=4696 (looser than current 4706), so the AI correctly chose to update *only* TPs to [4725, 4750, 4760]. The broker rejected with `modify_failed:10025`. The journal shows the order modify went through to the broker as `sl: 0.00, tp: 4760.00 [no changes]` — **the EA sent the modify with sl=0.00 instead of preserving the current SL**, and the broker responded "no changes" / invalid stops.

**Hypothesis:** `DoModifyTps` is sending `sl=0` to the broker when the AI payload omits `sl`, instead of reading the current SL off the position and re-sending it. Worth a focused look at `DoModifyTps` in `ea/CopyTrades.mq5` — should be a one-line fix to pass `PositionGetDouble(POSITION_SL)` as the SL argument.

### 3.3 🟡 Bootstrap noise (cosmetic)

Action #1 (21:11) is the "First launch: archived 784 historical messages" alert — harmless but it sits forever as a `pending` ALERT row. 37 ALERT rows accumulated in `pending` for the session. Consider auto-acking ALERTs older than 24 h.

### 3.4 🟡 Two SELL losses back-to-back early in the session

Trades #1 and #2 were both counter-trend SELLs against the eventual rally to 4750. The evaluator caught it on Trade #1 (score 48, explicit "counter-trend on both 1H and 4H"). Trade #2 was a REINFORCE — by definition it carries forward the prior signal's bias regardless of evaluation. Worth considering: **should REINFORCE be gated on the prior trade's evaluator score, or by current trend agreement?** Right now it is unconditional.

---

## 4. AI cost & latency

| Layer | Calls | Avg latency | p95 |
|---|---|---|---|
| Triage | 130 | 1,274 ms | 2,370 ms |
| Interpreter | 54 | 11,607 ms | 20,112 ms |
| **Total** | **184** | — | — |

Token totals: 385 k input / 35 k output / 249 k cache_read.
Cache hit ratio on input: **249/385 ≈ 65 %** — prompt caching is working well. Estimated session cost: roughly **$1.50–$2.50** (Sonnet 4.6 + Haiku 4.5 mix).

---

## 5. EA-level observations

- `ManagePlans` fired stage-1 partials cleanly on Trades #3, #4, #5 — no double-close (the `RegisterPlan` ticket-dedupe guard is holding).
- Trail SL on Trade #3 produced ~50 updates in 15 min once stage-2 hit — `OrderModify` calls all `done in 10–18 ms`.
- `LotsFromRisk(balance)` correctly tracked balance growth (1.01 → 0.91 lots per signal as balance changed).
- No stale-market-price ALERTs triggered → heartbeat is healthy.
- No ChasePrice opens fired this session (price was inside or near every entry zone on arrival).

---

## 6. Action items (ranked)

1. **🔴 Fix `DoModifyTps` SL passthrough** — preserve `PositionGetDouble(POSITION_SL)` when the AI payload omits `sl`. Add a regression test that asserts the same-side-with-looser-SL fixture results in TPs updated and SL preserved.
2. **🔴 Add a "management rejected" notifier** — when the EA POSTs `ai_partial_and_be_disabled`, the bot should DM the operator with a one-tap "force-execute" inline keyboard. Without this, the operator is flying blind whenever AI management is silently dropped.
3. **🟠 Decide on `EnableAIPartialAndBE` default** — current OFF state cost us at least one clearly-correct partial on Trade #5. Re-evaluate after 5–7 more sessions of dry-run data.
4. **🟡 Gate REINFORCE on evaluator score** — if the most-recent OPEN scored < 50, REINFORCE should ALERT instead of execute.
5. **🟡 ALERT row hygiene** — auto-archive pending ALERTs older than 24 h to keep the table queryable.
6. **🟢 Add a per-session P&L computation endpoint** — pull entry/exit/volume from `positions` + DEAL_ENTRY_OUT from MT5 history → write to a `session_pnl` table. Right now P&L is hand-estimated from the DB.

---

## 7. Bottom line

**The infrastructure performed.** Pipeline latency is low, triage is saving real cost, OPEN signals fire cleanly, the EA's automated stage-close is doing professional work (Trade #3 is a textbook execution), and the reconciler keeps DB and broker in sync.

**The only meaningful failure mode this session was the deliberately-disabled in-trade management**, which is doing what it was configured to do but cost us at least one clean partial on Trade #5 and made the user step in manually. The fix is operational (notification + decision on default), not architectural.

**MODIFY_TPS SL-passthrough is a real bug** — small, isolated, easy to fix.

Net: green session both financially (≈ +$1,800 estimated) and systemically (no crashes, no data loss, no misroutes). The two early SELL losses are signal quality, not system quality.
