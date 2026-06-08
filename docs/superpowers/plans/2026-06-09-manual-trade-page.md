# Manual Trade Page Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Manual Trade tab to the PySide6 GUI with a broker-fed candlestick chart where the operator places entry/TP/SL lines, sizes the lot from balance + risk cap, and fires the trade into the existing execution pipeline as a manual OPEN.

**Architecture:** EA publishes OHLC candles to a new API endpoint (stored as a JSON blob in `settings`, no migration). A new `POST /actions/manual` validates an OPEN via the existing `validators.OpenAction` and inserts it at `status='sent'` with `source_msg_id=NULL` and `manual:true`. The GUI tab draws candles with pyqtgraph, computes the lot with a pure unit-tested function, and submits through the API. The EA honors an explicit `lot` in the payload for manual trades.

**Tech Stack:** Python 3.13, FastAPI, Pydantic, SQLite, PySide6 + pyqtgraph, MQL5 (EA), pytest.

---

## Design reference

Spec: `docs/superpowers/specs/2026-06-09-manual-trade-page-design.md`

### Existing helpers this plan reuses (do not re-create)
- `src/api.py`: `build_app(conn)` (test factory), `_XAUUSD_CONTRACT_SIZE = 100.0`, `_pending_risk(lots, entry, sl, balance)`, `_fresh_balance()`, `_max_open_lots()`, `parse_payload(json_str)`.
- `src/validators.py`: `OpenAction` Pydantic model (validates side/SL/TP geometry, symbol). Construct it to validate manual OPENs.
- `src/config.py`: `SUPPORTED_SYMBOLS`, `DB_PATH`.
- `src/api_models.py`: add new request bodies here (Pydantic).
- Settings storage convention: `market_snapshot_{SYM}` JSON blob + `market_snapshot_{SYM}_at` ISO-8601 UTC string. Candles mirror this with `market_candles_{SYM}_{TF}` + `_at`.
- All timestamps: `datetime.now(timezone.utc).isoformat()` (never `utcnow()`).

### Conventions
- Candle storage keys: `market_candles_{SYM}_{TF}` (e.g. `market_candles_XAUUSD_M15`) and `market_candles_{SYM}_{TF}_at`.
- Valid timeframes (GUI dropdown + API): `M15`, `H1`, `H4`.
- Manual OPEN payload shape:
  ```json
  {"type":"OPEN","symbol":"XAUUSD","side":"BUY","entry_low":4500.0,
   "entry_high":4500.0,"tps":[4530.0],"sl":4490.0,"comment":"manual",
   "pending":false,"lot":0.05,"manual":true,"source":"manual_gui"}
  ```

---

## Phase 1 — Candle feed (API + storage)

### Task 1: `MarketCandlesBody` request model

**Files:**
- Modify: `src/api_models.py`
- Test: `tests/test_api_candles.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_candles.py
import pytest
from pydantic import ValidationError
from src.api_models import MarketCandlesBody, CandleBar


def test_candle_bar_parses_minimal_fields():
    bar = CandleBar(t="2026-06-09T10:00:00+00:00", o=4500.0, h=4505.0, l=4498.0, c=4502.0, v=123)
    assert bar.o == 4500.0 and bar.v == 123


def test_market_candles_body_defaults_symbol_xauusd():
    body = MarketCandlesBody(
        timeframe="M15",
        bars=[CandleBar(t="2026-06-09T10:00:00+00:00", o=1, h=2, l=0.5, c=1.5, v=1)],
    )
    assert body.symbol == "XAUUSD"
    assert body.timeframe == "M15"
    assert len(body.bars) == 1


def test_market_candles_body_rejects_bad_timeframe():
    with pytest.raises(ValidationError):
        MarketCandlesBody(
            timeframe="M5",
            bars=[CandleBar(t="2026-06-09T10:00:00+00:00", o=1, h=2, l=0.5, c=1.5, v=1)],
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_candles.py -v`
Expected: FAIL with `ImportError: cannot import name 'MarketCandlesBody'`.

- [ ] **Step 3: Add the models**

Append to `src/api_models.py`:

```python
from typing import Literal


class CandleBar(BaseModel):
    """One OHLC bar. `t` is the bar-open time, ISO-8601 UTC. `v` is tick volume."""
    t: str
    o: float
    h: float
    l: float
    c: float
    v: int = 0


class MarketCandlesBody(BaseModel):
    """EA -> API: a bounded series of OHLC bars for one symbol+timeframe.
    Stored as a JSON blob in settings (no schema migration); the GUI chart
    polls GET /market/candles to redraw."""
    symbol: str = "XAUUSD"
    timeframe: Literal["M15", "H1", "H4"]
    bars: list[CandleBar] = Field(default_factory=list)
```

(If `Literal` is already imported at the top of the file, do not duplicate the import.)

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_candles.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/api_models.py tests/test_api_candles.py
git commit -m "feat(api): MarketCandlesBody request model for candle feed"
```

---

### Task 2: `POST /market/candles` and `GET /market/candles`

**Files:**
- Modify: `src/api.py` (add both endpoints inside `build_app`, near the other `/market/*` routes ~line 1259)
- Modify: `src/api.py` imports (add `MarketCandlesBody`)
- Test: `tests/test_api_candles.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_candles.py`:

```python
import json
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _app(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn, build_app(conn)


def _bars():
    return [
        {"t": "2026-06-09T10:00:00+00:00", "o": 4500, "h": 4505, "l": 4498, "c": 4502, "v": 10},
        {"t": "2026-06-09T10:15:00+00:00", "o": 4502, "h": 4508, "l": 4501, "c": 4507, "v": 12},
    ]


def test_post_candles_then_get_round_trip(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/market/candles", json={"timeframe": "M15", "bars": _bars()})
    assert r.status_code == 200, r.text
    g = client.get("/market/candles", params={"symbol": "XAUUSD", "timeframe": "M15"})
    assert g.status_code == 200
    body = g.json()
    assert body["timeframe"] == "M15"
    assert len(body["bars"]) == 2
    assert body["bars"][1]["c"] == 4507
    assert body["stale"] is False


def test_get_candles_absent_returns_empty_stale(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    g = client.get("/market/candles", params={"symbol": "XAUUSD", "timeframe": "H1"})
    assert g.status_code == 200
    body = g.json()
    assert body["bars"] == []
    assert body["stale"] is True


def test_post_candles_rejects_unsupported_symbol(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/market/candles", json={"symbol": "EURUSD", "timeframe": "M15", "bars": _bars()})
    assert r.status_code == 400
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_candles.py -k "round_trip or absent or unsupported" -v`
Expected: FAIL with 404 (route not found).

- [ ] **Step 3: Add the import**

In `src/api.py`, add `MarketCandlesBody` to the `from src.api_models import (...)` block (keep alphabetical-ish ordering near `MarketSnapshotBody`).

- [ ] **Step 4: Add the endpoints**

Inside `build_app`, near the other `/market/*` routes, add:

```python
    _CANDLE_STALE_SEC = 120

    @app.post("/market/candles")
    def post_market_candles(body: MarketCandlesBody):
        """EA pushes a bounded OHLC series for one symbol+timeframe. Stored
        as a JSON blob in settings (mirrors /market/snapshot) so there is no
        schema migration; the GUI chart polls GET /market/candles."""
        if body.symbol.upper() not in config.SUPPORTED_SYMBOLS:
            raise HTTPException(400, f"unsupported symbol: {body.symbol}")
        sym = body.symbol.upper()
        tf = body.timeframe
        now = datetime.now(timezone.utc).isoformat()
        bars_json = json.dumps([b.model_dump() for b in body.bars])
        conn.execute("BEGIN")
        try:
            for key, val in (
                (f"market_candles_{sym}_{tf}", bars_json),
                (f"market_candles_{sym}_{tf}_at", now),
            ):
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return {"ok": True, "recorded_at": now, "count": len(body.bars)}

    @app.get("/market/candles")
    def get_market_candles(symbol: str = "XAUUSD", timeframe: str = "M15"):
        sym = symbol.upper()
        tf = timeframe
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN (?,?)",
                (f"market_candles_{sym}_{tf}", f"market_candles_{sym}_{tf}_at"),
            ).fetchall()
        }
        raw = rows.get(f"market_candles_{sym}_{tf}")
        at = rows.get(f"market_candles_{sym}_{tf}_at")
        if raw is None or at is None:
            return {"symbol": sym, "timeframe": tf, "bars": [], "at": None, "stale": True}
        try:
            bars = json.loads(raw)
        except (ValueError, TypeError):
            bars = []
        stale = True
        try:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(at)).total_seconds()
            stale = age > _CANDLE_STALE_SEC
        except (ValueError, TypeError):
            stale = True
        return {"symbol": sym, "timeframe": tf, "bars": bars, "at": at, "stale": stale}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_candles.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add src/api.py tests/test_api_candles.py
git commit -m "feat(api): POST/GET /market/candles candle feed endpoints"
```

---

## Phase 2 — Manual action injection (API)

### Task 3: `ManualOpenBody` request model

**Files:**
- Modify: `src/api_models.py`
- Test: `tests/test_api_manual.py`

- [ ] **Step 1: Write the failing test**

```python
# tests/test_api_manual.py
import pytest
from pydantic import ValidationError
from src.api_models import ManualOpenBody


def test_manual_open_body_minimal_market():
    b = ManualOpenBody(side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05)
    assert b.symbol == "XAUUSD"
    assert b.pending is False
    assert b.comment == "manual"


def test_manual_open_body_rejects_nonpositive_lot():
    with pytest.raises(ValidationError):
        ManualOpenBody(side="BUY", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.0)


def test_manual_open_body_rejects_bad_side():
    with pytest.raises(ValidationError):
        ManualOpenBody(side="LONG", entry=4500.0, sl=4490.0, tp=4530.0, lot=0.05)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_manual.py -v`
Expected: FAIL with `ImportError: cannot import name 'ManualOpenBody'`.

- [ ] **Step 3: Add the model**

Append to `src/api_models.py`:

```python
class ManualOpenBody(BaseModel):
    """GUI -> API: a manually placed OPEN. The GUI has already computed the
    lot and chosen the levels on the chart; the server validates geometry
    via validators.OpenAction and inserts at status='sent', flagged manual."""
    symbol: str = "XAUUSD"
    side: Literal["BUY", "SELL"]
    entry: float
    sl: float
    tp: float
    lot: float = Field(gt=0)
    pending: bool = False
    comment: str = "manual"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_manual.py -v`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/api_models.py tests/test_api_manual.py
git commit -m "feat(api): ManualOpenBody request model"
```

---

### Task 4: `POST /actions/manual`

**Files:**
- Modify: `src/api.py` (add endpoint inside `build_app` near `/actions/{action_id}/resize_pending`; add `ManualOpenBody` import; add `from src.validators import OpenAction` if not present)
- Test: `tests/test_api_manual.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_api_manual.py`:

```python
import json
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _app(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn, build_app(conn)


def test_post_manual_inserts_sent_open_with_flag(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/actions/manual", json={
        "side": "BUY", "entry": 4500.0, "sl": 4490.0, "tp": 4530.0, "lot": 0.05,
    })
    assert r.status_code == 200, r.text
    action_id = r.json()["action_id"]
    assert r.json()["status"] == "sent"
    row = conn.execute(
        "SELECT action_type, status, source_msg_id, payload_json FROM actions WHERE id=?",
        (action_id,),
    ).fetchone()
    assert row["action_type"] == "OPEN"
    assert row["status"] == "sent"
    assert row["source_msg_id"] is None
    p = json.loads(row["payload_json"])
    assert p["manual"] is True
    assert p["source"] == "manual_gui"
    assert p["lot"] == 0.05
    assert p["entry_low"] == 4500.0 and p["entry_high"] == 4500.0
    assert p["tps"] == [4530.0]
    assert p["pending"] is False


def test_post_manual_rejects_sl_wrong_side(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    # BUY with SL above entry -> OpenAction geometry validator rejects.
    r = client.post("/actions/manual", json={
        "side": "BUY", "entry": 4500.0, "sl": 4510.0, "tp": 4530.0, "lot": 0.05,
    })
    assert r.status_code == 422


def test_post_manual_rejects_bad_lot(tmp_path):
    conn, app = _app(tmp_path)
    client = TestClient(app)
    r = client.post("/actions/manual", json={
        "side": "BUY", "entry": 4500.0, "sl": 4490.0, "tp": 4530.0, "lot": 0.0,
    })
    assert r.status_code == 422
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_manual.py -k "inserts or wrong_side or bad_lot" -v`
Expected: FAIL with 404 (route not found).

- [ ] **Step 3: Add imports**

In `src/api.py`: add `ManualOpenBody` to the `from src.api_models import (...)` block, and add near the other `src.*` imports:

```python
from src.validators import OpenAction
```

- [ ] **Step 4: Add the endpoint**

Inside `build_app`, near `resize_pending`, add:

```python
    @app.post("/actions/manual")
    def post_manual_open(body: ManualOpenBody):
        """Inject a manually-placed OPEN straight into the pipeline at
        status='sent'. Validates geometry with the same OpenAction model the
        AI path uses, so a wrong-side SL or >2% SL distance is rejected with
        422. source_msg_id stays NULL and the payload is flagged manual."""
        from pydantic import ValidationError
        try:
            action = OpenAction(
                symbol=body.symbol.upper(),
                side=body.side,
                entry_low=body.entry,
                entry_high=body.entry,
                tps=[body.tp],
                sl=body.sl,
                comment=body.comment,
                pending=body.pending,
            )
        except ValidationError as e:
            raise HTTPException(422, f"invalid manual open: {e.errors()}")
        if body.lot > _max_open_lots():
            raise HTTPException(422, f"lot exceeds max open lots: {body.lot}")
        payload = action.model_dump()
        payload["lot"] = body.lot
        payload["manual"] = True
        payload["source"] = "manual_gui"
        now = datetime.now(timezone.utc).isoformat()
        conn.execute("BEGIN")
        try:
            cur = conn.execute(
                "INSERT INTO actions(source_msg_id, action_type, payload_json, "
                "status, execute_after, created_at) "
                "VALUES(NULL, 'OPEN', ?, 'sent', ?, ?)",
                (json.dumps(payload), now, now),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        action_id = int(cur.lastrowid)
        trades.info(
            "manual_action_inserted action_id=%s side=%s lot=%s entry=%s sl=%s tp=%s pending=%s",
            action_id, body.side, body.lot, body.entry, body.sl, body.tp, body.pending,
        )
        return {"action_id": action_id, "status": "sent"}
```

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_api_manual.py -v`
Expected: PASS (6 passed).

- [ ] **Step 6: Commit**

```bash
git add src/api.py tests/test_api_manual.py
git commit -m "feat(api): POST /actions/manual injects a manual OPEN at status=sent"
```

---

## Phase 3 — Pure GUI logic (sizing + submit helpers)

### Task 5: Lot-sizing function

**Files:**
- Create: `src/gui/services/manual_trade_sizing.py`
- Test: `tests/gui/test_manual_trade_sizing.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_sizing.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
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
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_sizing.py -v`
Expected: PASS (5 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gui/services/manual_trade_sizing.py tests/gui/test_manual_trade_sizing.py
git commit -m "feat(gui): pure lot-sizing for manual trades (per-100 + risk cap)"
```

---

### Task 6: SL/TP assignment, order-type inference, payload builder

**Files:**
- Create: `src/gui/services/manual_trade_submit.py`
- Test: `tests/gui/test_manual_trade_submit.py`

- [ ] **Step 1: Write the failing test**

```python
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
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_submit.py -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the implementation**

```python
# src/gui/services/manual_trade_submit.py
"""Pure helpers for turning chart lines + form choices into a manual OPEN
request body, plus the HTTP submit call.

assign_sl_tp: given the trade side and the two non-entry line prices, decide
which is SL and which is TP. BUY -> TP is the line above entry, SL below;
SELL -> reversed. Raises ValueError when the two lines don't straddle entry.
"""
from __future__ import annotations

import json
import urllib.request


def assign_sl_tp(side: str, *, entry: float, line_a: float, line_b: float) -> tuple[float, float]:
    above = max(line_a, line_b)
    below = min(line_a, line_b)
    if not (below < entry < above):
        raise ValueError(
            f"the two levels must straddle entry ({entry}); got {line_a} and {line_b}"
        )
    if side == "BUY":
        return below, above   # sl, tp
    if side == "SELL":
        return above, below   # sl, tp
    raise ValueError(f"side must be BUY or SELL, got {side!r}")


def infer_pending(*, entry: float, live_price: float, tol: float) -> bool:
    """True -> place a pending limit (entry away from price). False -> market."""
    return abs(entry - live_price) > tol


def build_manual_open_body(
    *, side: str, entry: float, sl: float, tp: float, lot: float,
    pending: bool, symbol: str = "XAUUSD",
) -> dict:
    return {
        "symbol": symbol, "side": side, "entry": entry, "sl": sl, "tp": tp,
        "lot": lot, "pending": pending, "comment": "manual",
    }


def submit_manual_open(api_base: str, body: dict, *, timeout: float = 5.0) -> dict:
    """POST the body to /actions/manual. Raises urllib.error.HTTPError on
    non-2xx so the caller can surface the server's validation message."""
    url = api_base.rstrip("/") + "/actions/manual"
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_submit.py -v`
Expected: PASS (6 passed).

- [ ] **Step 5: Commit**

```bash
git add src/gui/services/manual_trade_submit.py tests/gui/test_manual_trade_submit.py
git commit -m "feat(gui): manual-trade SL/TP assignment, order-type inference, submit"
```

---

## Phase 4 — GUI view (chart + form + wiring)

> The chart and form are interactive Qt widgets; they get a headless smoke test (instantiate without a live event loop), matching the existing `tests/gui/*` pattern. Behavior logic already lives in the Phase-3 pure modules.

### Task 7: Candlestick chart panel

**Files:**
- Create: `src/gui/views/manual_trade_chart.py`
- Test: `tests/gui/test_manual_trade_view_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

```python
# tests/gui/test_manual_trade_view_smoke.py
import pytest

pytest.importorskip("PySide6")


@pytest.fixture(scope="module")
def qapp():
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    yield app


def test_chart_panel_instantiates_and_sets_candles(qapp):
    from src.gui.views.manual_trade_chart import ChartPanel
    panel = ChartPanel()
    bars = [
        {"t": "2026-06-09T10:00:00+00:00", "o": 4500, "h": 4505, "l": 4498, "c": 4502, "v": 10},
        {"t": "2026-06-09T10:15:00+00:00", "o": 4502, "h": 4508, "l": 4501, "c": 4507, "v": 12},
    ]
    panel.set_candles(bars)          # must not raise
    panel.set_live_price(4506.0)     # must not raise
    panel.arm_order_lines(entry=4503.0, line_a=4530.0, line_b=4490.0)
    entry, a, b = panel.line_values()
    assert entry == 4503.0
    assert {a, b} == {4530.0, 4490.0}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_view_smoke.py::test_chart_panel_instantiates_and_sets_candles -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the chart panel**

```python
# src/gui/views/manual_trade_chart.py
"""pyqtgraph candlestick chart for the Manual Trade tab.

Draws OHLC candles, a live price line, and three draggable horizontal lines
(entry / TP-or-SL / TP-or-SL). The view above reads line_values() to size and
submit the trade; the lines emit `lines_changed` while dragging.
"""
from __future__ import annotations

from datetime import datetime

import pyqtgraph as pg
from PySide6.QtCore import Signal
from PySide6.QtGui import QPicture, QPainter
from PySide6.QtWidgets import QWidget, QVBoxLayout


class _CandleItem(pg.GraphicsObject):
    """Minimal candlestick item. bars: list of (x, open, high, low, close)."""

    def __init__(self) -> None:
        super().__init__()
        self._picture = QPicture()
        self._bars: list[tuple[float, float, float, float, float]] = []

    def set_bars(self, bars: list[tuple[float, float, float, float, float]]) -> None:
        self._bars = bars
        self._rebuild()

    def _rebuild(self) -> None:
        self._picture = QPicture()
        p = QPainter(self._picture)
        up = pg.mkBrush("#26a69a")
        down = pg.mkBrush("#ef5350")
        up_pen = pg.mkPen("#26a69a")
        down_pen = pg.mkPen("#ef5350")
        width = 0.6
        for (x, o, h, l, c) in self._bars:
            bullish = c >= o
            p.setPen(up_pen if bullish else down_pen)
            p.setBrush(up if bullish else down)
            p.drawLine(pg.QtCore.QPointF(x, l), pg.QtCore.QPointF(x, h))
            top, bot = (c, o) if bullish else (o, c)
            p.drawRect(pg.QtCore.QRectF(x - width / 2, bot, width, max(top - bot, 1e-6)))
        p.end()
        self.prepareGeometryChange()

    def paint(self, painter, *args) -> None:
        painter.drawPicture(0, 0, self._picture)

    def boundingRect(self):
        return pg.QtCore.QRectF(self._picture.boundingRect())


class ChartPanel(QWidget):
    lines_changed = Signal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._plot = pg.PlotWidget()
        self._plot.showGrid(x=True, y=True, alpha=0.2)
        self._candles = _CandleItem()
        self._plot.addItem(self._candles)
        self._live_line = pg.InfiniteLine(angle=0, movable=False,
                                          pen=pg.mkPen("#888", style=pg.QtCore.Qt.DashLine))
        self._plot.addItem(self._live_line)
        self._entry_line: pg.InfiniteLine | None = None
        self._line_a: pg.InfiniteLine | None = None
        self._line_b: pg.InfiniteLine | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._plot)

    def set_candles(self, bars: list[dict]) -> None:
        parsed: list[tuple[float, float, float, float, float]] = []
        for i, b in enumerate(bars):
            parsed.append((float(i), float(b["o"]), float(b["h"]),
                           float(b["l"]), float(b["c"])))
        self._candles.set_bars(parsed)

    def set_live_price(self, price: float) -> None:
        self._live_line.setValue(price)

    def arm_order_lines(self, *, entry: float, line_a: float, line_b: float) -> None:
        for ln in (self._entry_line, self._line_a, self._line_b):
            if ln is not None:
                self._plot.removeItem(ln)
        self._entry_line = pg.InfiniteLine(pos=entry, angle=0, movable=True,
                                           pen=pg.mkPen("#42a5f5", width=2),
                                           label="ENTRY {value:.2f}")
        self._line_a = pg.InfiniteLine(pos=line_a, angle=0, movable=True,
                                       pen=pg.mkPen("#66bb6a", width=2),
                                       label="{value:.2f}")
        self._line_b = pg.InfiniteLine(pos=line_b, angle=0, movable=True,
                                       pen=pg.mkPen("#ef5350", width=2),
                                       label="{value:.2f}")
        for ln in (self._entry_line, self._line_a, self._line_b):
            ln.sigPositionChanged.connect(lambda: self.lines_changed.emit())
            self._plot.addItem(ln)

    def is_armed(self) -> bool:
        return self._entry_line is not None

    def line_values(self) -> tuple[float, float, float]:
        if not self.is_armed():
            raise RuntimeError("order lines not armed")
        return (float(self._entry_line.value()),
                float(self._line_a.value()),
                float(self._line_b.value()))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_view_smoke.py::test_chart_panel_instantiates_and_sets_candles -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/gui/views/manual_trade_chart.py tests/gui/test_manual_trade_view_smoke.py
git commit -m "feat(gui): candlestick ChartPanel with draggable order lines"
```

---

### Task 8: Manual trade view (form + wiring + nav registration)

**Files:**
- Create: `src/gui/views/manual_trade_view.py`
- Modify: `src/gui/windows/main_window.py` (register the view in `self._views` ~line 203 and add a nav item)
- Modify: `src/gui/panels/nav_rail.py` (add a "manual" nav entry — match the existing item pattern)
- Test: `tests/gui/test_manual_trade_view_smoke.py`

- [ ] **Step 1: Write the failing smoke test**

Append to `tests/gui/test_manual_trade_view_smoke.py`:

```python
def _fake_stack(tmp_path):
    from src.db import connect, init_schema
    db = tmp_path / "stack.db"
    conn = connect(str(db))
    init_schema(conn)
    conn.execute("INSERT INTO settings(key,value) VALUES('account_balance','1000')")
    conn.commit()
    conn.close()

    class _Stack:
        db_path = db
        api_url = "http://127.0.0.1:8766"
        name = "TEST"
    return _Stack()


def test_manual_trade_view_instantiates(qapp, tmp_path):
    from src.gui.views.manual_trade_view import ManualTradeView
    view = ManualTradeView(_fake_stack(tmp_path))
    assert view is not None
    # recompute with no armed lines must not raise
    view._recompute()


def test_manual_trade_view_computes_lot_when_armed(qapp, tmp_path):
    from src.gui.views.manual_trade_view import ManualTradeView
    view = ManualTradeView(_fake_stack(tmp_path))
    view._chart.arm_order_lines(entry=4500.0, line_a=4530.0, line_b=4490.0)
    view._side = "BUY"
    view._recompute()
    # balance 1000, default lot_per_100 0.01 -> base 0.10, well above min
    assert view._last_sizing is not None
    assert view._last_sizing.final_lot > 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_view_smoke.py -k manual_trade_view -v`
Expected: FAIL with `ModuleNotFoundError`.

- [ ] **Step 3: Write the view**

```python
# src/gui/views/manual_trade_view.py
"""Manual Trade tab: candlestick chart + order form. Places entry/TP/SL
lines, sizes the lot from balance + risk cap, and POSTs a manual OPEN."""
from __future__ import annotations

import sqlite3

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel, QMessageBox,
    QPushButton, QVBoxLayout, QWidget,
)

from src.gui.services.manual_trade_sizing import compute_lot, SizingResult
from src.gui.services.manual_trade_submit import (
    assign_sl_tp, build_manual_open_body, infer_pending, submit_manual_open,
)
from src.gui.views.manual_trade_chart import ChartPanel

_CONTRACT_SIZE = 100.0     # XAUUSD: $100 per 1.0 price move per 1.0 lot
_LOT_STEP = 0.01
_LOT_MIN = 0.01
_LOT_MAX = 100.0
_ENTRY_TOL = 0.5           # within this of live price -> market order


class ManualTradeView(QWidget):
    def __init__(self, stack) -> None:
        super().__init__()
        self._stack = stack
        self._side = "BUY"
        self._last_sizing: SizingResult | None = None
        self._live_price: float | None = None
        self._timeframe = "M15"
        self._build_ui()
        self._poll = QTimer(self)
        self._poll.setInterval(3000)
        self._poll.timeout.connect(self._refresh_market)
        self._poll.start()
        self._refresh_market()

    # ---- UI ----------------------------------------------------------
    def _build_ui(self) -> None:
        self._chart = ChartPanel()
        self._chart.lines_changed.connect(self._recompute)

        self._tf_combo = QComboBox()
        self._tf_combo.addItems(["M15", "H1", "H4"])
        self._tf_combo.currentTextChanged.connect(self._on_tf_changed)

        self._side_combo = QComboBox()
        self._side_combo.addItems(["BUY", "SELL"])
        self._side_combo.currentTextChanged.connect(self._on_side_changed)

        self._lot_per_100 = QDoubleSpinBox()
        self._lot_per_100.setDecimals(4)
        self._lot_per_100.setRange(0.0001, 100.0)
        self._lot_per_100.setValue(0.01)
        self._lot_per_100.valueChanged.connect(self._recompute)

        self._risk_cap = QDoubleSpinBox()
        self._risk_cap.setDecimals(2)
        self._risk_cap.setRange(0.01, 100.0)
        self._risk_cap.setValue(1.0)
        self._risk_cap.setSuffix(" %")
        self._risk_cap.valueChanged.connect(self._recompute)

        self._arm_btn = QPushButton("Place order (arm lines)")
        self._arm_btn.clicked.connect(self._arm)
        self._summary = QLabel("Arm the lines to size a trade.")
        self._summary.setWordWrap(True)
        self._exec_btn = QPushButton("Execute")
        self._exec_btn.setEnabled(False)
        self._exec_btn.clicked.connect(self._execute)

        form = QFormLayout()
        form.addRow("Timeframe", self._tf_combo)
        form.addRow("Direction", self._side_combo)
        form.addRow("Lot per $100", self._lot_per_100)
        form.addRow("Risk cap", self._risk_cap)
        form.addRow(self._arm_btn)
        form.addRow(self._summary)
        form.addRow(self._exec_btn)

        form_box = QWidget()
        form_box.setLayout(form)
        form_box.setMaximumWidth(360)

        root = QHBoxLayout(self)
        root.addWidget(self._chart, stretch=1)
        root.addWidget(form_box)

    # ---- data --------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        c = sqlite3.connect(str(self._stack.db_path))
        c.row_factory = sqlite3.Row
        return c

    def _balance(self) -> float:
        try:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT value FROM settings WHERE key='account_balance'"
                ).fetchone()
            finally:
                conn.close()
            return float(row[0]) if row and row[0] is not None else 0.0
        except (sqlite3.Error, ValueError, TypeError):
            return 0.0

    def _refresh_market(self) -> None:
        import json
        import urllib.request
        base = self._stack.api_url.rstrip("/")
        try:
            with urllib.request.urlopen(
                f"{base}/market/candles?symbol=XAUUSD&timeframe={self._timeframe}", timeout=3
            ) as r:
                data = json.loads(r.read().decode("utf-8"))
            self._chart.set_candles(data.get("bars", []))
        except Exception:
            pass
        try:
            with urllib.request.urlopen(
                f"{base}/market/price?symbol=XAUUSD", timeout=3
            ) as r:
                p = json.loads(r.read().decode("utf-8"))
            bid = p.get("bid")
            ask = p.get("ask")
            if bid is not None and ask is not None:
                self._live_price = (float(bid) + float(ask)) / 2.0
                self._chart.set_live_price(self._live_price)
        except Exception:
            pass

    # ---- events ------------------------------------------------------
    def _on_tf_changed(self, tf: str) -> None:
        self._timeframe = tf
        self._refresh_market()

    def _on_side_changed(self, side: str) -> None:
        self._side = side
        self._recompute()

    def _arm(self) -> None:
        ref = self._live_price if self._live_price is not None else 4500.0
        self._chart.arm_order_lines(entry=ref, line_a=ref + 10.0, line_b=ref - 10.0)
        self._recompute()

    def _recompute(self) -> None:
        if not self._chart.is_armed():
            self._exec_btn.setEnabled(False)
            return
        entry, a, b = self._chart.line_values()
        try:
            sl, tp = assign_sl_tp(self._side, entry=entry, line_a=a, line_b=b)
        except ValueError as e:
            self._summary.setText(f"⚠ {e}")
            self._exec_btn.setEnabled(False)
            self._last_sizing = None
            return
        balance = self._balance()
        sizing = compute_lot(
            balance=balance, lot_per_100=self._lot_per_100.value(),
            risk_cap_pct=self._risk_cap.value(), entry=entry, sl=sl,
            contract_size=_CONTRACT_SIZE, lot_step=_LOT_STEP,
            lot_min=_LOT_MIN, lot_max=_LOT_MAX,
        )
        self._last_sizing = sizing
        pending = infer_pending(entry=entry,
                                live_price=self._live_price or entry, tol=_ENTRY_TOL)
        otype = "LIMIT" if pending else "MARKET"
        if sizing.blocked:
            self._summary.setText(f"⚠ {sizing.blocked}")
            self._exec_btn.setEnabled(False)
            return
        self._summary.setText(
            f"{self._side} {otype}  lot={sizing.final_lot}\n"
            f"entry={entry:.2f}  SL={sl:.2f}  TP={tp:.2f}\n"
            f"risk=${sizing.risk_at_final:.2f} ({sizing.risk_pct_of_balance}% of bal)"
        )
        self._exec_btn.setEnabled(True)

    def _execute(self) -> None:
        if not self._chart.is_armed() or self._last_sizing is None:
            return
        entry, a, b = self._chart.line_values()
        sl, tp = assign_sl_tp(self._side, entry=entry, line_a=a, line_b=b)
        pending = infer_pending(entry=entry,
                                live_price=self._live_price or entry, tol=_ENTRY_TOL)
        lot = self._last_sizing.final_lot
        confirm = QMessageBox.question(
            self, "Confirm manual trade",
            f"{self._side} {'LIMIT' if pending else 'MARKET'} XAUUSD\n"
            f"lot={lot}  entry={entry:.2f}  SL={sl:.2f}  TP={tp:.2f}\n"
            f"risk=${self._last_sizing.risk_at_final:.2f}",
        )
        if confirm != QMessageBox.StandardButton.Yes:
            return
        body = build_manual_open_body(
            side=self._side, entry=entry, sl=sl, tp=tp, lot=lot, pending=pending,
        )
        try:
            res = submit_manual_open(self._stack.api_url, body)
        except Exception as e:  # noqa: BLE001 - surface any transport/validation error
            QMessageBox.critical(self, "Manual trade failed", str(e))
            return
        QMessageBox.information(
            self, "Manual trade sent",
            f"action_id={res.get('action_id')} status={res.get('status')}",
        )

    def closeEvent(self, event) -> None:  # type: ignore[override]
        self._poll.stop()
        super().closeEvent(event)
```

- [ ] **Step 4: Run the smoke test**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/test_manual_trade_view_smoke.py -v`
Expected: PASS (all tests in file).

- [ ] **Step 5: Register the view in main_window**

In `src/gui/windows/main_window.py`:
- Add the import near the other view imports (~line 29):
  ```python
  from src.gui.views.manual_trade_view import ManualTradeView
  ```
- Add to the `self._views` dict (~line 203), after `"live"`:
  ```python
  "manual": ManualTradeView(self._stack),
  ```

In `src/gui/panels/nav_rail.py`: add a `"manual"` nav item labeled "Manual Trade" following the exact pattern used for the existing items (match how `"live"`/`"risk"` entries are declared — same icon/label tuple structure).

- [ ] **Step 6: Verify GUI still imports and smoke tests pass**

Run: `.venv/Scripts/python.exe -m pytest tests/gui/ -q`
Expected: PASS (existing GUI smoke tests + new ones).

- [ ] **Step 7: Commit**

```bash
git add src/gui/views/manual_trade_view.py src/gui/windows/main_window.py src/gui/panels/nav_rail.py tests/gui/test_manual_trade_view_smoke.py
git commit -m "feat(gui): Manual Trade tab — chart + order form + nav registration"
```

---

## Phase 5 — EA changes (candle publish + manual lot honor)

> MQL5 isn't unit-testable in this harness. These tasks are implementation +
> manual verification on a **demo account**. Compile in MetaEditor (F7).

### Task 9: EA publishes candles

**Files:**
- Modify: `ea/CopyTrades.mq5`

- [ ] **Step 1: Add inputs** (near the other `input` declarations, e.g. by the heartbeat inputs):

```cpp
input bool             CandlePublishEnabled = true;        // push OHLC candles to the GUI chart
input ENUM_TIMEFRAMES  CandleTimeframe      = PERIOD_M15;  // timeframe to publish
input int              CandleCount          = 200;         // bars per push
input int              CandlePublishSec     = 5;           // publish cadence (seconds)
```

- [ ] **Step 2: Add the publisher function** (near `HeartbeatMarketPrice`):

```cpp
string TimeframeName(ENUM_TIMEFRAMES tf) {
   if(tf == PERIOD_M15) return "M15";
   if(tf == PERIOD_H1)  return "H1";
   if(tf == PERIOD_H4)  return "H4";
   return "M15";  // GUI only offers M15/H1/H4
}

datetime g_last_candle_publish = 0;

void PublishCandles() {
   if(!CandlePublishEnabled) return;
   if(TimeGameInt() - g_last_candle_publish < CandlePublishSec) return;  // throttle
   g_last_candle_publish = (datetime)TimeGameInt();

   MqlRates rates[];
   int got = CopyRates(Symbol_Override, CandleTimeframe, 0, CandleCount, rates);
   if(got <= 0) return;

   string bars = "[";
   for(int i = 0; i < got; i++) {
      if(i > 0) bars += ",";
      // bar-open time as ISO-8601 UTC (server stores verbatim)
      string t = TimeToString(rates[i].time, TIME_DATE|TIME_SECONDS);
      StringReplace(t, ".", "-");
      StringReplace(t, " ", "T");
      bars += StringFormat(
         "{\"t\":\"%s+00:00\",\"o\":%.2f,\"h\":%.2f,\"l\":%.2f,\"c\":%.2f,\"v\":%d}",
         t, rates[i].open, rates[i].high, rates[i].low, rates[i].close, (int)rates[i].tick_volume);
   }
   bars += "]";

   string body = StringFormat(
      "{\"symbol\":\"%s\",\"timeframe\":\"%s\",\"bars\":%s}",
      Symbol_Override, TimeframeName(CandleTimeframe), bars);
   string resp;
   PostJson("/market/candles", body, resp);   // reuse the existing POST helper
}
```

> Use the EA's existing time helper for the throttle (whatever
> `HeartbeatMarketPrice` uses — e.g. `TimeCurrent()`/`GetTickCount()`); the
> `TimeGameInt()` name above is a placeholder for that existing helper. Use the
> EA's existing JSON-POST helper (the same one `HeartbeatMarketPrice` calls)
> rather than `PostJson` if it has a different name.

- [ ] **Step 3: Call it from `OnTimer`** unconditionally (alongside `HeartbeatMarketPrice()`, before `PollAndExecute`, so it runs even when halted):

```cpp
   HeartbeatMarketPrice();
   PublishCandles();          // <-- add this line
```

- [ ] **Step 4: Compile**

In MetaEditor: F7 on `ea/CopyTrades.mq5`. Expected: 0 errors.

- [ ] **Step 5: Manual verification (demo)**

Attach EA on a demo chart. Then:
```bash
# with the stack running, confirm candles arrive:
curl "http://127.0.0.1:8766/market/candles?symbol=XAUUSD&timeframe=M15"
```
Expected: JSON with ~200 bars and `"stale": false`.

- [ ] **Step 6: Commit**

```bash
git add ea/CopyTrades.mq5
git commit -m "feat(ea): publish OHLC candles to /market/candles for the GUI chart"
```

---

### Task 10: EA honors explicit `lot` on a manual OPEN

**Files:**
- Modify: `ea/CopyTrades.mq5` (`DoOpen`, ~line 1579)

- [ ] **Step 1: Read the lot override at the top of `DoOpen`** (after `side`/`entry`/`sl`/`tps` are parsed, before lot computation):

```cpp
   // Manual trades carry an explicit, GUI-computed lot. When present (>0),
   // it overrides LotsFromRisk/LotsFromBalance sizing. The broker
   // min/step/max + free-margin clamp still applies downstream.
   double manualLot = StringToDouble(JsonField(payload, "lot"));
   bool   hasManualLot = (manualLot > 0.0);
```

- [ ] **Step 2: Apply it at each sizing site in `DoOpen`.**
  - Pending path (~line 1655) where `double lotsP = LotsFromRisk(sl, entryLimit);`:
    ```cpp
    double lotsP = hasManualLot ? manualLot : LotsFromRisk(sl, entryLimit);
    ```
  - Market path (~line 1745) where `double lotsTotal = LotsFromRisk(sl, entry);`:
    ```cpp
    double lotsTotal = hasManualLot ? manualLot : LotsFromRisk(sl, entry);
    ```
  Leave the existing `NormalizeVolume` / broker clamp / free-margin cap calls
  in place — they run on `lotsP`/`lotsTotal` regardless of source.

- [ ] **Step 3: Compile**

In MetaEditor: F7. Expected: 0 errors.

- [ ] **Step 4: Manual verification (demo)**

With the stack + EA running on demo, place a manual market BUY from the GUI at a known lot (e.g. 0.05). Expected: the filled position's volume equals the GUI lot (clamped only if below broker min), SL/TP match the placed lines, `logs/trades.log` shows `manual_action_inserted` then `position_opened`.

- [ ] **Step 5: Commit**

```bash
git add ea/CopyTrades.mq5
git commit -m "feat(ea): DoOpen honors explicit payload lot for manual trades"
```

---

## Phase 6 — Display marking

### Task 11: MANUAL badge in the actions table

**Files:**
- Modify: `src/gui/models/actions_model.py`
- Test: `tests/test_actions_model_manual_badge.py`

- [ ] **Step 1: Inspect the model** to find how a display string/column is produced per action row (look for where `action_type` or payload is rendered). The badge attaches to the type/summary cell when `payload.manual` is truthy.

- [ ] **Step 2: Write the failing test**

```python
# tests/test_actions_model_manual_badge.py
import json
from src.gui.models.actions_model import manual_badge_text


def test_manual_badge_for_manual_payload():
    payload = json.dumps({"type": "OPEN", "manual": True, "source": "manual_gui"})
    assert manual_badge_text(payload) == "MANUAL"


def test_no_badge_for_telegram_payload():
    payload = json.dumps({"type": "OPEN"})
    assert manual_badge_text(payload) == ""


def test_no_badge_for_bad_json():
    assert manual_badge_text("not json") == ""
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_actions_model_manual_badge.py -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Add the helper** to `src/gui/models/actions_model.py` (module level):

```python
import json as _json


def manual_badge_text(payload_json: str | None) -> str:
    """Return 'MANUAL' when the action payload is a GUI-placed manual trade."""
    if not payload_json:
        return ""
    try:
        p = _json.loads(payload_json)
    except (ValueError, TypeError):
        return ""
    return "MANUAL" if isinstance(p, dict) and p.get("manual") else ""
```

Then use `manual_badge_text(row["payload_json"])` where the model builds the
type/summary display cell, appending the badge (e.g. `f"{type_str}  [{badge}]"`
when non-empty). Match the model's existing display-string construction.

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_actions_model_manual_badge.py -v`
Expected: PASS (3 passed).

- [ ] **Step 6: Commit**

```bash
git add src/gui/models/actions_model.py tests/test_actions_model_manual_badge.py
git commit -m "feat(gui): MANUAL badge for manually-placed actions"
```

---

### Task 12: MANUAL prefix in notification rendering

**Files:**
- Modify: `src/telegram_format.py`
- Test: `tests/test_telegram_format.py` (append)

- [ ] **Step 1: Inspect `src/telegram_format.py`** to find the function that renders an action into the DM text (the one tested by the existing `tests/test_telegram_format.py`). Identify how it receives the payload.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_telegram_format.py`:

```python
def test_manual_trade_gets_prefix():
    from src.telegram_format import render_manual_prefix
    assert render_manual_prefix({"manual": True}) == "🛠 MANUAL "
    assert render_manual_prefix({}) == ""
    assert render_manual_prefix({"manual": False}) == ""
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_telegram_format.py::test_manual_trade_gets_prefix -v`
Expected: FAIL with `ImportError`.

- [ ] **Step 4: Add the helper** to `src/telegram_format.py`:

```python
def render_manual_prefix(payload: dict) -> str:
    """Prefix manual (GUI-placed) trades so DMs are unambiguous."""
    return "🛠 MANUAL " if isinstance(payload, dict) and payload.get("manual") else ""
```

Then prepend `render_manual_prefix(payload)` to the action's rendered DM
headline where the formatter builds it (use the already-parsed payload dict in
that function).

- [ ] **Step 5: Run test to verify it passes**

Run: `.venv/Scripts/python.exe -m pytest tests/test_telegram_format.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/telegram_format.py tests/test_telegram_format.py
git commit -m "feat(notify): prefix manual trades in Telegram DM rendering"
```

---

## Final verification

- [ ] **Run the full hermetic suite**

Run: `.venv/Scripts/python.exe -m pytest -q --ignore=tests/test_replay.py --ignore=tests/test_management_replay.py`
Expected: all pass (existing 1263 + new tests).

- [ ] **Manual end-to-end on demo** (EA attached, stack running):
  1. Open the GUI → Manual Trade tab. Confirm candles render and update, and the live price line tracks.
  2. Pick BUY, arm lines, drag entry to live price (→ MARKET), TP above, SL below. Confirm the summary shows a sane lot + $ risk.
  3. Execute → confirm dialog → OK. Confirm `action_id` toast.
  4. Watch `logs/trades.log`: `manual_action_inserted` → `action_claimed` → `position_opened` with the GUI lot and correct SL/TP.
  5. Confirm the Live tab shows the action with a **MANUAL** badge and the DM (if bot DMs) has the 🛠 prefix.
  6. Repeat with a limit (entry away from price) and a SELL.

---

## Self-review notes (coverage vs spec)

- Candle feed (EA push + API store + GUI poll): Tasks 1,2,9, view `_refresh_market`. ✓
- New Qt tab, pyqtgraph: Tasks 7,8. ✓
- Lot math (per-100 + risk cap brake, % of balance): Task 5. ✓
- Direction in form, TP/SL auto-label, order-type inference: Task 6 + view. ✓
- Execution path (confirm dialog → status='sent'): Tasks 4,8. ✓
- Manual marker (source_msg_id NULL + payload flag): Task 4; display Tasks 11,12. ✓
- Timeframe M15 default + M15/H1/H4 dropdown: Tasks 1,2,8. ✓
- Risk cap % of balance: Task 5 + form. ✓
- EA lot-override (open item resolved): payload field `"lot"`, Task 10. ✓
- contract_size: GUI uses constant 100 (`_CONTRACT_SIZE`); spec tolerates EA push absence. ✓
- Staged-close note (single TP → no TradePlan): inherent, no code needed. ✓
