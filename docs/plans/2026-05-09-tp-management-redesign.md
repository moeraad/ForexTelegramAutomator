# TP Management Redesign — Implementation Plan

**Date:** 2026-05-09
**Owner:** EA (`ea/CopyTrades.mq5`) — Python side untouched
**Goal:** Replace the current 1/2/3-TP staged-close policy with a more aggressive front-loaded close + earlier trailing.

---

## 1. Behaviour spec (the new policy)

| TPs | TP1 hit | TP2 hit | Final exit |
|---|---|---|---|
| **1** | TP1 closes the position (broker-side TP) | — | TP1 |
| **2** | Close **70 %**; SL → **signal-anchor** (see §2); **start trailing** on remaining 30 %; remove broker TP | — (broker TP gone) | Trail SL hit on reversal |
| **3** | Close **50 %**; SL → **signal-anchor**; broker TP unchanged (still at TP3) | Close **30 %** of original; SL → **TP1 price**; remove broker TP; **start trailing** on remaining 20 % | Trail SL hit on reversal |

Lot rounding: each close-fraction is computed against `origLots`, floored to `SYMBOL_VOLUME_STEP`, then validated against `SYMBOL_VOLUME_MIN`. Same retry/giveup machinery as today.

---

## 2. "Signal-anchor" SL price (the key new concept)

Per the user's confirmation: **the symmetric "behind-the-chase edge" of the signal entry zone**, not the chased fill price.

- BUY: `slAnchor = entry_low` — the lower of `payload.entry_low/entry_high`. For zone 4711–4713 → `slAnchor = 4711`. If chase filled at 4715, the new SL sits 4 points behind the chase; if it filled inside the zone at 4712, SL sits 1 point behind.
- SELL: `slAnchor = entry_high` — for zone 4697–4700 → `slAnchor = 4700`. Symmetric to BUY.

**Edge cases:**
- Chase filled *very far* past the zone: `slAnchor` may end up _worse_ than the original fill (BE-relative loss territory). **Guard:** if `slAnchor` is past `currentBid/Ask` in the unfavourable direction (i.e., would trigger an immediate stop-out) OR the resulting SL would be looser than `slOrig`, fall back to the broker stops-level minimum behind the fill (same clamp logic the existing `TrailStage2Sls` uses).
- Missing entry zone in the plan (legacy positions restored from `GlobalVariables` pre-upgrade): fall back to `entry` (chased fill) — preserves old behaviour for in-flight trades during the upgrade window.

---

## 3. Trailing logic

Reuse `TrailStage2Sls()` with two changes:

1. Activate it for **stage-1 of 2-TP plans** in addition to **stage-2 of 3-TP plans**.
2. Trail-gap formula stays `|finalTp - entry| / 3` where `finalTp` is the last TP in the signal (TP2 for 2-TP, TP3 for 3-TP). On gold (~4700) this is typically $5–$15/oz. **Open question — see §7-Q1.**

Step threshold (5 × point), stops-level clamp, ratchet-only direction — all reused as-is. Trail starts from whatever SL was set at the stage transition (`slAnchor` for 2-TP / TP1 price for 3-TP) and walks toward the favourable side.

---

## 4. Affected code

All changes are in `ea/CopyTrades.mq5`. No DB schema, no Python, no validators, no AI prompt change.

| Touchpoint | File:line (approx) | Change |
|---|---|---|
| `TradePlan` struct | `CopyTrades.mq5:130–143` | Add `double entryLow`, `double entryHigh`. Bumps persisted-plan format → see §6 migration. |
| `RegisterPlan` signature | `CopyTrades.mq5:978` | Take `entryLow, entryHigh`. Caller (`DoOpen`) already has them in the action payload. |
| `DoOpen` call site | `CopyTrades.mq5:952` | Pass the two new args. |
| `PersistPlan` / `LoadPersistedPlans` | `CopyTrades.mq5:1036–1095` | Persist + restore `entryLow/entryHigh` to/from `GlobalVariables`. |
| `ManagePlans` stage 0 (TP1) — 2-TP branch | `CopyTrades.mq5:1244–1322` | Close 70 %, SL → `slAnchor`, **remove broker TP** (was: kept at TP2 for auto-close). Mark stage=1, advance to trail. |
| `ManagePlans` stage 0 (TP1) — 3-TP branch | same | Close 50 %, SL → `slAnchor`. Broker TP stays at TP3. Mark stage=1. |
| `ManagePlans` stage 1 — 3-TP only | `CopyTrades.mq5:1323–1411` | Close 30 % of `origLots`, SL → TP1 price, remove broker TP. Mark stage=2. (2-TP plans skip stage 1 — they hand off directly to trail after stage 0.) |
| `TrailStage2Sls` | `CopyTrades.mq5:1436–1500` | Loosen the `if(p.stage != 2) continue` / `if(p.tpCount != 3) continue` gates. Trail when `(tpCount==2 && stage>=1) \|\| (tpCount==3 && stage>=2)`. |
| `RegisterPlan` 1-TP gate | `DoOpen` ~`CopyTrades.mq5:935` (existing) | Unchanged — 1-TP signals continue to skip plan registration. |

---

## 5. Implementation steps

Discrete, ordered, each leaves the EA in a working state.

1. **Add `entryLow/entryHigh` to `TradePlan`, `RegisterPlan`, `DoOpen`, persistence.** No behaviour change yet — fields are stored, not consumed. Compile, smoke-test on demo with one fresh OPEN to confirm persistence round-trips through `GlobalVariables`.
2. **Add helper `double SignalAnchorSl(const TradePlan &p, double currentExit)`** — returns the clamped `slAnchor` per §2. Pure function, easy to unit-test by inspection.
3. **Rewrite `ManagePlans` stage-0 logic** to switch on `tpCount`:
   - `tpCount == 2`: close 70 %, SL → `SignalAnchorSl`, **PositionModify with TP=0** to remove broker TP. Stage→1.
   - `tpCount == 3`: close 50 %, SL → `SignalAnchorSl`. Broker TP unchanged. Stage→1.
4. **Rewrite `ManagePlans` stage-1 logic** (3-TP only):
   - Close 30 % of `origLots`, SL → `tps[0]` (TP1 price), TP=0. Stage→2.
5. **Update `TrailStage2Sls` activation** to cover both `(tpCount==2, stage>=1)` and `(tpCount==3, stage>=2)`.
6. **Update header comments** in `CopyTrades.mq5` (the policy summary block ~line 1233–1242 + the file-top docstring) and in `CLAUDE.md` (the "EA staged-close policy" table).
7. **Update / replace tests in `tests/test_ea_*` (if any apply)** — most ManagePlans logic is exercised live, but the lot-fraction math and `SignalAnchorSl` clamp belong in a unit test if one of the existing test files covers them. Otherwise, document the new behaviour in `docs/sessions/` after the first live verification.
8. **Live smoke-test sequence on demo (small lots, e.g. 0.01):**
   - 3-TP signal, drive price through TP1 → confirm 50 % close + SL @ `slAnchor`.
   - Continue through TP2 → confirm 30 % close + SL @ TP1 + TP removed + trail starts.
   - 2-TP signal, drive through TP1 → confirm 70 % close + SL @ `slAnchor` + TP removed + trail starts.
   - 1-TP signal, drive through TP1 → confirm broker auto-close (no plan touched).
   - Restart EA mid-stage → confirm `entryLow/entryHigh` restored from `GlobalVariables`.

---

## 6. Migration / backward compatibility

- **In-flight positions opened before the upgrade** have no `entryLow/entryHigh` in their persisted plan. `LoadPersistedPlans` initialises missing fields to `0.0`. `SignalAnchorSl` falls back to `entry` (chased fill) when `entryLow == 0.0`, so legacy plans get the old BE-on-fill behaviour for their remaining stages — no surprises.
- **No DB schema migration** — the change lives entirely in EA-side memory + `GlobalVariables`. Python sees the same actions, same payloads, same `positions` rows.
- **GlobalVariables key naming**: add `_eL`, `_eH` keys via the existing `PlanKey(ticket, suffix)` helper. Old keys remain valid.

---

## 7. Open questions (sensible defaults proposed; flag if you disagree)

**Q1 — Trail gap formula.**
Current: `|finalTp - entry| / 3`. Should we keep this for both 2-TP and 3-TP plans? On a 2-TP signal where finalTp = TP2 ≈ 15 points above entry, gap = $5 — pretty tight. On 3-TP where finalTp = TP3 ≈ 30+ points, gap = $10+ — looser. **Default: keep the formula as-is**; if it stops out too early on 2-TP plans we tune later.

**Q2 — Should we also remove the broker TP at stage 0 for 3-TP plans?**
The new spec only mentions removing TP at stage 2 (after TP2). Default: **keep broker TP at TP3 through stage 1** — it stays as a worst-case safety ceiling in case the EA dies between stage 0 and stage 1. Removed only when trail activates at stage 2.

**Q3 — `slAnchor` clamp behaviour when fill is *better* than the signal zone.**
For BUY: if chase didn't trigger and fill was below the zone (price moved down through the entry, broker matched at e.g. 4708 for zone 4711–4713), `entry_low = 4711` is *above* the fill — would lock immediate profit. That's fine: SL at 4711 is favourable and below current price. **Default: no clamp on the favourable side**, only on the adverse side (per §2 edge cases).

**Q4 — `EnableAiPartialAndBE` interaction.**
This new policy is the EA-internal `ManagePlans` flow, which is gated separately from the AI's reactive `MOVE_SL_BE` / `CLOSE_PARTIAL`. **Default: unchanged** — the new staged policy fires regardless of `EnableAiPartialAndBE` (matches the comment at `CopyTrades.mq5:69-72`).

---

## 8. Risk assessment

- **70 % close on TP1 (2-TP)** vs current 50 % — bigger immediate book, smaller residual to trail. On the $1,800 win sessions this would have been ~+$200/trade more booked, ~$200/trade less captured if the runner extends. Net depends on hit rate; for choppy gold sessions this is the better trade-off.
- **Removing broker TP at stage 1 (2-TP)** means EA-death between stage 0 and SL hit leaves the position uncapped. Trade #3 of 2026-05-08 ran for 25 minutes in stage 2 with no broker TP — no incidents, but the risk exists. Mitigation: keep the existing 2-min OnTimer plan-recovery loop solid.
- **SL → `slAnchor` instead of fill** can move SL adversely when the chase landed deep past the zone. The Q3 clamp prevents auto-stop-out, but a deep-chase BUY where `entry_low` is close to current price will stop out faster on a small retest. Acceptable — chasing > 5 points past zone is already flagged by `ChaseMinRewardRatio`.

---

## 9. Bottom line

Pure EA-side change. ~80 lines of MQL5, no Python, no schema, no AI prompt. One new helper (`SignalAnchorSl`), edits to four functions (`ManagePlans` stages 0 + 1, `TrailStage2Sls` activation gates, `RegisterPlan` signature), plus the persistence round-trip update. Backward-compatible for in-flight positions via the `entryLow == 0.0` fallback path.

Smoke test ladder in §5.8 covers all four signal shapes (1/2/3 TPs + restart) so we can ship behind a feature toggle if desired (`input bool UseNewStagedPolicy = true;` gating the new branches with a fall-through to the old code) — recommended for the first 2–3 live sessions.
