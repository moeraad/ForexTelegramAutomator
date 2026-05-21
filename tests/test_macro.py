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
from src.macro import _fetch_one_sync, _fetch_fred_csv_sync, fetch_macro_snapshot  # noqa: E402
import src.macro as _macro_mod  # for monkeypatching the FRED helper


@pytest.fixture(autouse=True)
def _no_real_fred(monkeypatch):
    """Default: FRED helper returns None in every test. Tests that need
    real_yield to be present override _fetch_fred_csv_sync explicitly.
    Keeps the suite hermetic — no real network calls to FRED."""
    monkeypatch.setattr(_macro_mod, "_fetch_fred_csv_sync", lambda *a, **kw: None)


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


# ---- FRED real-yield helper --------------------------------------------

def _stub_urlopen(csv_text: str):
    """Build a fake context manager that mimics urlopen().

    Accepts **kwargs so the stub stays compatible with extra arguments
    the production caller passes (e.g. `context=` for the certifi SSL
    fix). Without this, adding any new urlopen kwarg breaks every test
    that mocks the helper, even though the new kwarg is irrelevant to
    what the test is exercising.
    """
    class _Resp:
        def __init__(self, text): self._b = text.encode("utf-8")
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self): return self._b
    return lambda url, *args, **kwargs: _Resp(csv_text)


def test_fred_csv_parses_last_two_values(monkeypatch):
    csv = (
        "DATE,DFII10\n"
        "2026-05-08,1.85\n"
        "2026-05-09,1.82\n"
        "2026-05-10,1.90\n"
    )
    monkeypatch.setattr("urllib.request.urlopen", _stub_urlopen(csv))
    r = _fetch_fred_csv_sync("real_yield", "DFII10")
    assert r is not None
    label, last, chg = r
    assert label == "real_yield"
    assert last == 1.90
    # chg_pct: (1.90 - 1.82) / 1.82 * 100 ≈ 4.395%
    assert abs(chg - 4.3956) < 0.01


def test_fred_csv_skips_dot_rows(monkeypatch):
    """FRED encodes missing weekend/holiday observations as '.' — those
    rows must be skipped so chg_pct compares the two latest REAL values."""
    csv = (
        "DATE,DFII10\n"
        "2026-05-08,1.85\n"
        "2026-05-09,.\n"          # weekend skip
        "2026-05-10,.\n"          # weekend skip
        "2026-05-11,1.88\n"
    )
    monkeypatch.setattr("urllib.request.urlopen", _stub_urlopen(csv))
    r = _fetch_fred_csv_sync("real_yield", "DFII10")
    assert r is not None
    _, last, chg = r
    assert last == 1.88
    # chg vs 1.85 (the prior REAL value), not vs "."
    assert abs(chg - ((1.88 - 1.85) / 1.85 * 100.0)) < 0.001


def test_fred_csv_returns_none_on_error(monkeypatch):
    def boom(*a, **kw):
        raise RuntimeError("DNS")
    monkeypatch.setattr("urllib.request.urlopen", boom)
    r = _fetch_fred_csv_sync("real_yield", "DFII10")
    assert r is None


@pytest.mark.asyncio
async def test_fetch_macro_snapshot_includes_real_yield_when_fred_succeeds(monkeypatch):
    """When FRED responds and yfinance also responds, the snapshot
    contains both real_yield and the yfinance tickers."""
    _yf_stub.download.side_effect = None
    _yf_stub.download.return_value = _frame([100.0, 100.5])
    monkeypatch.setattr(
        _macro_mod, "_fetch_fred_csv_sync",
        lambda label, series_id: (label, 1.85, 1.21),
    )
    snap = await fetch_macro_snapshot()
    assert snap is not None
    assert snap.get("real_yield") == 1.85
    assert abs(snap.get("real_yield_chg_pct") - 1.21) < 0.01
    assert "dxy" in snap  # yfinance side still present
