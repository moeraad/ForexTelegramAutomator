"""Tests for src.macro — the directional-bias evaluator's macro feed.

yfinance is mocked at the module level (sys.modules) so these run
hermetically without network. We assert:
  - chg_pct math against a known 2-row close series
  - missing 2nd row -> per-ticker None
  - all tickers fail -> snapshot None (not crash)
  - one ticker fails -> snapshot still produced for the rest
"""
from __future__ import annotations

import sys
import types
from unittest.mock import MagicMock

import pytest

# ---- yfinance shim ------------------------------------------------------
#
# We don't want a real yfinance install (heavy dep, network-touching) on
# the test path. Build a fake module that lives in sys.modules BEFORE
# src.macro is imported. Each test sets `_yf_stub.download` to whatever
# canned response it wants.

_yf_stub = types.ModuleType("yfinance")
_yf_stub.download = MagicMock()  # type: ignore[attr-defined]
sys.modules["yfinance"] = _yf_stub

# Now safe to import.
from src.macro import _fetch_one_sync, fetch_macro_snapshot  # noqa: E402


def _frame(closes: list[float]):
    """Build a minimal pandas-DataFrame-like object that exposes
    `df["Close"].iloc[i]` and `len(df)`. Avoids requiring real pandas."""
    class _Series:
        def __init__(self, values): self._v = values
        @property
        def iloc(self): return self
        def __getitem__(self, i): return self._v[i]
    class _Frame:
        def __init__(self, vals):
            self._closes = _Series(vals)
            self._len = len(vals)
        def __len__(self): return self._len
        def __getitem__(self, key):
            if key == "Close":
                return self._closes
            raise KeyError(key)
    return _Frame(closes)


# ---- _fetch_one_sync ----------------------------------------------------


def test_fetch_one_sync_chg_pct_math():
    """prev=100, last=102 -> +2.0% change."""
    _yf_stub.download.return_value = _frame([100.0, 102.0])
    res = _fetch_one_sync("dxy", "DX-Y.NYB")
    assert res is not None
    label, last, chg = res
    assert label == "dxy"
    assert last == 102.0
    assert abs(chg - 2.0) < 1e-9


def test_fetch_one_sync_negative_change():
    _yf_stub.download.return_value = _frame([4.50, 4.41])  # 10Y dropped 9bp
    res = _fetch_one_sync("tnx", "^TNX")
    assert res is not None
    _, _, chg = res
    assert chg < 0
    assert abs(chg - ((-0.09 / 4.50) * 100.0)) < 1e-9


def test_fetch_one_sync_returns_none_on_short_frame():
    """yfinance returned only 1 row (market just opened) -> None."""
    _yf_stub.download.return_value = _frame([100.0])
    assert _fetch_one_sync("dxy", "DX-Y.NYB") is None


def test_fetch_one_sync_returns_none_on_exception():
    _yf_stub.download.side_effect = RuntimeError("yahoo down")
    try:
        assert _fetch_one_sync("dxy", "DX-Y.NYB") is None
    finally:
        _yf_stub.download.side_effect = None


def test_fetch_one_sync_returns_none_on_zero_prev():
    """Avoid div-by-zero when prev close is 0 (corrupt data)."""
    _yf_stub.download.return_value = _frame([0.0, 100.0])
    assert _fetch_one_sync("dxy", "DX-Y.NYB") is None


# ---- fetch_macro_snapshot ----------------------------------------------


@pytest.mark.asyncio
async def test_fetch_macro_snapshot_all_succeed():
    """Happy path — yfinance returns the same shape for every ticker;
    snapshot dict has all 5 + 5 fields plus fetched_at."""
    _yf_stub.download.side_effect = None
    _yf_stub.download.return_value = _frame([100.0, 101.0])
    snap = await fetch_macro_snapshot()
    assert snap is not None
    for k in ("dxy", "tnx", "vix", "jpy", "oil"):
        assert k in snap
        assert f"{k}_chg_pct" in snap
        assert snap[k] == 101.0
        assert abs(snap[f"{k}_chg_pct"] - 1.0) < 1e-9
    assert "fetched_at" in snap
    # ISO-8601 UTC convention used across the project.
    assert "+00:00" in snap["fetched_at"] or snap["fetched_at"].endswith("Z")


@pytest.mark.asyncio
async def test_fetch_macro_snapshot_partial_succeeds():
    """Some tickers fail (alternating), others succeed -> partial dict."""
    counter = {"i": 0}
    def side(*args, **kwargs):
        counter["i"] += 1
        # Fail every other call.
        if counter["i"] % 2 == 0:
            raise RuntimeError("transient")
        return _frame([100.0, 100.5])
    _yf_stub.download.side_effect = side
    try:
        snap = await fetch_macro_snapshot()
    finally:
        _yf_stub.download.side_effect = None
    assert snap is not None
    # At least one but not all five tickers should be present.
    present = [k for k in ("dxy", "tnx", "vix", "jpy", "oil") if k in snap]
    assert 0 < len(present) < 5


@pytest.mark.asyncio
async def test_fetch_macro_snapshot_returns_none_when_all_fail():
    _yf_stub.download.side_effect = RuntimeError("network out")
    try:
        snap = await fetch_macro_snapshot()
    finally:
        _yf_stub.download.side_effect = None
    assert snap is None
