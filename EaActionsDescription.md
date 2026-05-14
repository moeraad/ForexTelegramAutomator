# EA Actions Description

What the EA does in MT5 for each action type emitted by the AI pipeline.
Order is roughly "lifecycle".

## Opening positions

| Action | Trigger (channel says) | EA does | MT5 effect |
|---|---|---|---|
| **OPEN** | Full signal: side + entry zone + SL + 1–3 TPs (`BUY @ 4790-4792 SL 4780 TP1 4800 TP2 4810`) | If price is in/near zone → `OrderSend` immediately. If past zone but reward ≥ 50% remaining → "chase" at market. If too far past zone → `status=watching`, polls until price re-enters zone or `expires_at` passes. | New position with SL + the last TP (TP3) as MT5-side TP. Partial closes at intermediate TPs are scheduled by `RegisterPlan`. |
| **OPEN_INSTANT** | Bare directional like "BUY gold", no zone/SL/TP | Opens at current market price with magic number, **no SL or TP set** — marked "naked" until ATTACH_SIGNAL arrives | Position open with no protection. Awaits a structured signal to attach SL/TPs. Dashboard marks `[NAKED]`. |
| **ATTACH_SIGNAL** | Structured signal arrives after an OPEN_INSTANT, same side | `PositionModify` on the naked position, setting SL + last TP. Registers staged-close plan from the TPs list. | Naked position upgraded to fully-managed: now has SL/TP, partial-close stages scheduled. |

## Managing the open position

The new management types (MOVE_SL_BE, MOVE_SL, TIGHTEN_SL, CLOSE_PARTIAL,
CLOSE_FULL, MODIFY_TPS) **never carry an `mt5_ticket`** — the EA resolves the
singleton open XAUUSD position via `FindSingletonOpenTicket`. If none is open,
the EA POSTs `rejected` with reason `no_open_position`.

| Action | Trigger | EA does | MT5 effect |
|---|---|---|---|
| **MOVE_SL_BE** | "secure your entry", "أمن دخولك" | `PositionModify` with new SL = original entry price | Stop becomes risk-free. Idempotent — skipped if already at BE. |
| **MOVE_SL** | "stop at 4826", "ستوبك 26" (shorthand decoded against MARKET mid) | `PositionModify` with new SL = `price` | SL jumps to specified price. Idempotent — skipped if within 0.05 of current SL. |
| **TIGHTEN_SL** | "tighten stop", "ستوبك صغير" | Reduces SL distance by `by_fraction` (default 0.5) of remaining (current_price − SL) | SL moves halfway toward current price. |
| **CLOSE_PARTIAL** | "take half", "احجز نصف" | `OrderSend` with `Trade_Action_Close_By` for `fraction × original_volume` (default 0.5) | Half of original lots closed at market. `partial_close_count++` so reminder messages are skipped. |
| **CLOSE_FULL** | "close it", "خرجنا" | Full `OrderClose` | Position closed at market. |
| **MODIFY_TPS** | New signal arrives with same side but new TPs (no SL change) | `PositionModify` setting new MT5-side TP = last new TP. `RegisterPlan` rewrites the staged closes for the new TP list. | TPs updated; staged-close schedule reset. |

## Re-entry / flipping

| Action | Trigger | EA does | MT5 effect |
|---|---|---|---|
| **REOPEN_LAST** | "available again", "متاحة للدخول" | Only fires if NO position open. Looks up the last closed XAUUSD position (within `within_hours`, default 24), reuses its `entry_low/high/sl/tps`, calls OPEN. Rejected with `already_open` if position exists. | Brand new position with identical params to the previously-closed one. |
| **REINFORCE** | "reinforce BUY", "عزز شراء" | Closes any current position (regardless of PnL), then opens a new one in the requested `side` with params from last closed signal. | Existing position closed + new position opened. Atomic from operator's POV. |

## Non-trading

| Action | Trigger | EA does | MT5 effect |
|---|---|---|---|
| **ALERT** | Anything ambiguous; AI emits with `level` + `text` | Bot DMs the operator. EA ignores it. | Nothing in MT5. Operator sees a Telegram message. |
| **IGNORE** | Commentary, religious phrases, encouragement, schedule references | Bot suppresses notification (no DM). EA never sees it. | Nothing. |
| **UNKNOWN** | Classifier can't decide | Goes to Rejected list; operator can review. | Nothing. |

## Automated post-fill management (no action_type — runs inside the EA)

These aren't actions emitted by the AI; the EA does them on its own once a
position is open. Listed for completeness because they're part of the
lifecycle:

- **TP1 hit (2-TP signal)** → close 70%, move SL to signal anchor (entry edge of zone), remove MT5-side TP, start trailing the remaining 30%.
- **TP1 hit (3-TP signal)** → close 50%, move SL to signal anchor.
- **TP2 hit (3-TP signal)** → close 30% of original, move SL to TP1 price, remove MT5-side TP, start trailing the remaining 20%.
- **Trailing** → activates after the conditions above. Ratchet-only — never widens. Step = 5 × point.
- **Reconciliation** → every timer tick, the EA cross-checks `positions WHERE status='open'` against MT5's actual open positions; any DB row MT5 doesn't recognize is marked closed with reason `mt5_not_found`.

## Singleton invariant

The whole system is built around **one open XAUUSD position at a time**.
Management actions implicitly target that singleton. If the AI ever tries to
OPEN while a position is already open, the EA either rejects it, flips sides
(CLOSE_FULL + OPEN), or upgrades a NAKED to fully-managed via ATTACH_SIGNAL —
depending on the action_type. This is enforced both in the AI prompt and in
the EA dispatcher.
