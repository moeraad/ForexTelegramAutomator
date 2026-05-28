from src.fingerprint import signal_fingerprint
from src.validators import OpenAction


def _mk(symbol="XAUUSD", side="BUY", low=4864.0, high=4866.0,
        tps=(4880.0,), sl=4855.0):
    return OpenAction(symbol=symbol, side=side, entry_low=low, entry_high=high,
                      tps=list(tps), sl=sl)


def test_identical_signals_same_fingerprint():
    a = _mk()
    b = _mk()
    assert signal_fingerprint(a) == signal_fingerprint(b)


def test_near_miss_within_band_same_fingerprint():
    """Prices within the same band bucket produce identical fingerprints."""
    a = _mk(low=4864.0, high=4866.0, tps=(4880.0,), sl=4855.0)
    b = _mk(low=4864.5, high=4866.5, tps=(4881.0,), sl=4854.2)
    assert signal_fingerprint(a, band=5.0) == signal_fingerprint(b, band=5.0)


def test_different_symbol_differs():
    """Bypass pydantic supported-symbols validation so this test stays
    valid even when only one symbol is configured."""
    a = OpenAction.model_construct(
        type="OPEN", symbol="XAUUSD", side="BUY",
        entry_low=4864.0, entry_high=4866.0, tps=[4880.0], sl=4855.0, comment="",
    )
    b = OpenAction.model_construct(
        type="OPEN", symbol="BTCUSD", side="BUY",
        entry_low=4864.0, entry_high=4866.0, tps=[4880.0], sl=4855.0, comment="",
    )
    assert signal_fingerprint(a) != signal_fingerprint(b)


def test_different_side_differs():
    """Same zone with opposite directions must fingerprint differently.
    Each side gets its own geometrically-valid SL/TP (post-2026-05-25
    validator rejects SL on the wrong side of entry)."""
    a = _mk(side="BUY",  tps=(4880.0,), sl=4855.0)  # BUY: SL below entry
    b = _mk(side="SELL", tps=(4850.0,), sl=4880.0)  # SELL: SL above entry
    assert signal_fingerprint(a) != signal_fingerprint(b)


def test_far_entry_differs():
    # Shift the whole geometry (entry + TP + SL) for the "far" case so
    # the moved entry doesn't put TP below the BUY entry zone.
    a = _mk(low=4864.0, high=4866.0, tps=(4880.0,), sl=4855.0)
    b = _mk(low=4900.0, high=4902.0, tps=(4920.0,), sl=4890.0)
    assert signal_fingerprint(a) != signal_fingerprint(b)


def test_different_sl_differs():
    a = _mk(sl=4855.0)
    b = _mk(sl=4800.0)
    assert signal_fingerprint(a) != signal_fingerprint(b)


def test_tp_order_does_not_matter():
    """TPs are sorted before hashing so reordering doesn't change fingerprint."""
    a = _mk(tps=(4880.0, 4890.0))
    b = _mk(tps=(4890.0, 4880.0))
    assert signal_fingerprint(a) == signal_fingerprint(b)


def test_different_tp_count_differs():
    a = _mk(tps=(4880.0,))
    b = _mk(tps=(4880.0, 4890.0))
    assert signal_fingerprint(a) != signal_fingerprint(b)


def test_custom_band_changes_bucketing():
    """With a wider band, prices that differed under $5 bands collapse."""
    a = _mk(low=4855.0, high=4857.0, tps=(4878.0,), sl=4840.0)
    b = _mk(low=4862.0, high=4864.0, tps=(4882.0,), sl=4845.0)
    assert signal_fingerprint(a, band=5.0) != signal_fingerprint(b, band=5.0)
    assert signal_fingerprint(a, band=20.0) == signal_fingerprint(b, band=20.0)
