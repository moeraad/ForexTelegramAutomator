# tests/gui/test_manual_trade_submit.py
import pytest
from src.gui.services.manual_trade_submit import (
    assign_sl_tp, infer_pending, build_manual_open_body,
)


def test_assign_sl_tp_buy():
    # BUY: TP above entry, SL below.
    sl, tp = assign_sl_tp("BUY", entry=4500.0, line_a=4530.0, line_b=4490.0)
    assert sl == 4490.0 and tp == 4530.0


def test_assign_sl_tp_sell():
    # SELL: TP below entry, SL above.
    sl, tp = assign_sl_tp("SELL", entry=4500.0, line_a=4530.0, line_b=4470.0)
    assert sl == 4530.0 and tp == 4470.0


def test_assign_sl_tp_rejects_non_straddle():
    # Both lines above entry for a BUY -> cannot tell SL from TP.
    with pytest.raises(ValueError):
        assign_sl_tp("BUY", entry=4500.0, line_a=4530.0, line_b=4520.0)


def test_infer_pending_market_when_near_price():
    # entry within tolerance of live price -> market (pending False).
    assert infer_pending(entry=4500.0, live_price=4500.2, tol=0.5) is False


def test_infer_pending_limit_when_far():
    assert infer_pending(entry=4480.0, live_price=4500.0, tol=0.5) is True


def test_build_manual_open_body_shape():
    body = build_manual_open_body(
        side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05, pending=False,
    )
    assert body == {
        "symbol": "XAUUSD", "side": "BUY", "entry": 4500.0,
        "sl": 4490.0, "tp": 4530.0, "lot": 0.05, "pending": False,
        "comment": "manual",
    }
