# tests/gui/test_manual_trade_sizing.py
import pytest
from src.gui.services.manual_trade_sizing import compute_lot, SizingResult


def test_per_100_governs_when_under_cap():
    # balance 1000, 0.01 lot/$100 -> base 0.10. SL 10 away, contract 100 ->
    # risk at base = 0.10 * 100 * 10 = $100. Cap 50% -> $500. base wins.
    r = compute_lot(balance=1000.0, lot_per_100=0.01, risk_cap_pct=50.0,
                    entry=4500.0, sl=4490.0, contract_size=100.0,
                    lot_step=0.01, lot_min=0.01, lot_max=100.0)
    assert r.final_lot == 0.10
    assert r.blocked is None
    assert round(r.risk_at_final, 2) == 100.0


def test_risk_cap_limits_when_base_too_big():
    # base 0.10 risks $100; cap 5% of 1000 = $50 -> cap_lot = 50/(100*10)=0.05.
    r = compute_lot(balance=1000.0, lot_per_100=0.01, risk_cap_pct=5.0,
                    entry=4500.0, sl=4490.0, contract_size=100.0,
                    lot_step=0.01, lot_min=0.01, lot_max=100.0)
    assert r.final_lot == 0.05
    assert round(r.risk_at_final, 2) == 50.0


def test_rounds_down_to_lot_step():
    # cap_lot computes to 0.037 -> round down to 0.03 at step 0.01.
    r = compute_lot(balance=1000.0, lot_per_100=1.0, risk_cap_pct=3.7,
                    entry=4500.0, sl=4490.0, contract_size=100.0,
                    lot_step=0.01, lot_min=0.01, lot_max=100.0)
    assert r.final_lot == 0.03


def test_blocked_when_below_min_lot():
    # tiny balance: base way under min, cap also under -> blocked.
    r = compute_lot(balance=50.0, lot_per_100=0.01, risk_cap_pct=1.0,
                    entry=4500.0, sl=4499.0, contract_size=100.0,
                    lot_step=0.01, lot_min=0.01, lot_max=100.0)
    assert r.final_lot == 0.0
    assert r.blocked is not None and "min" in r.blocked.lower()


def test_zero_sl_distance_blocked():
    r = compute_lot(balance=1000.0, lot_per_100=0.01, risk_cap_pct=50.0,
                    entry=4500.0, sl=4500.0, contract_size=100.0,
                    lot_step=0.01, lot_min=0.01, lot_max=100.0)
    assert r.final_lot == 0.0
    assert r.blocked is not None
