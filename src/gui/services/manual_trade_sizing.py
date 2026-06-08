# src/gui/services/manual_trade_sizing.py
"""Pure lot-sizing for manual trades.

Per-$100 sizes the base lot; the risk cap shrinks it so the SL loss never
exceeds the cap. Final lot is rounded DOWN to the broker step and clamped to
[lot_min, lot_max]; below the minimum the trade is blocked.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


@dataclass(frozen=True)
class SizingResult:
    final_lot: float
    base_lot: float
    cap_lot: float
    risk_at_final: float
    risk_pct_of_balance: float | None
    blocked: str | None


def _round_down(value: float, step: float) -> float:
    if step <= 0:
        return value
    return math.floor(value / step) * step


def compute_lot(
    *, balance: float, lot_per_100: float, risk_cap_pct: float,
    entry: float, sl: float, contract_size: float,
    lot_step: float, lot_min: float, lot_max: float,
) -> SizingResult:
    sl_distance = abs(entry - sl)
    if sl_distance <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, None, "SL distance is zero")
    if balance <= 0:
        return SizingResult(0.0, 0.0, 0.0, 0.0, None, "balance unavailable")

    risk_cap_dollars = balance * risk_cap_pct / 100.0
    base_lot = (balance / 100.0) * lot_per_100
    cap_lot = risk_cap_dollars / (contract_size * sl_distance)

    chosen = min(base_lot, cap_lot)
    final = _round_down(chosen, lot_step)
    final = min(final, lot_max)
    final = round(final, 2)

    if final < lot_min:
        return SizingResult(
            0.0, round(base_lot, 4), round(cap_lot, 4), 0.0, None,
            f"computed lot {final:.4f} below broker min {lot_min}",
        )

    risk_at_final = final * contract_size * sl_distance
    risk_pct = (risk_at_final / balance * 100.0) if balance > 0 else None
    return SizingResult(
        final, round(base_lot, 4), round(cap_lot, 4),
        round(risk_at_final, 2),
        round(risk_pct, 3) if risk_pct is not None else None,
        None,
    )
