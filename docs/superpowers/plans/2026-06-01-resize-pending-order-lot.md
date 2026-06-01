# Resize Pending Order Lot — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the operator change a pending (unfilled) XAUUSD order's lot size from the desktop GUI, with the EA re-placing the broker order at the new lot while keeping the app↔EA action lifecycle continuous.

**Architecture:** GUI POSTs `/actions/{id}/resize_pending`; the API writes `pending_lot_override` + a monotonic `resize_seq` into the action payload (status stays `watching`); the EA's existing 10s `GET /actions/{id}` poll inside `ManagePendingOrders` detects the new seq, feasibility-checks margin, then deletes and re-places the broker order at the new lot and re-POSTs `watching` with the new ticket. A small balance-heartbeat extension (piggybacked on the existing `/market/price` POST) feeds a "% of balance" risk warning; it degrades to absolute dollars when balance is unknown.

**Tech Stack:** Python 3.13, FastAPI + Pydantic, SQLite (WAL), pytest + `fastapi.testclient`, PySide6 (Qt) GUI, MQL5 EA.

**Spec:** `docs/superpowers/specs/2026-06-01-resize-pending-order-lot-design.md`

---

## File Structure

- `src/api_models.py` — add `account_balance`/`account_equity` to `MarketPriceBody`; add `ResizePendingBody`.
- `src/api.py` — extend `post_market_price` to persist balance; add `_pending_risk` helper + `POST /actions/{action_id}/resize_pending`.
- `tests/test_api.py` — hermetic tests for the above.
- `tests/test_resize_integration.py` — mock-EA lifecycle continuity test (new file).
- `ea/CopyTrades.mq5` — extract `PlacePendingLimit` helper; add balance to `HeartbeatMarketPrice`; add `appliedResizeSeq` to `PendingOrder`; add resize step in `ManagePendingOrders`.
- `src/gui/panels/resize_control.py` — new `ResizeControl` widget (spinbox + Apply + warning + threaded POST).
- `src/gui/panels/detail_panel.py` — accept `api_base`; render `ResizeControl` for `watching` OPEN actions.
- `src/gui/views/live_view.py` — pass `self._stack.api_base` into `DetailPanel`.
- `CLAUDE.md` — document the new endpoint, settings keys, and payload fields.

**Convention reminders:** timestamps are ISO-8601 UTC with explicit `+00:00` (`datetime.now(timezone.utc).isoformat()`); `api.py` stays "dumb" (records the override, EA owns broker mechanics); run tests with `.venv\Scripts\python.exe -m pytest`.

---

## Task 1: Persist account balance on the market-price heartbeat (API)

**Files:**
- Modify: `src/api_models.py:131-138` (`MarketPriceBody`)
- Modify: `src/api.py:1215-1247` (`post_market_price`)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
def test_post_market_price_persists_balance(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/market/price", json={
        "symbol": "XAUUSD", "bid": 4520.48, "ask": 4520.62,
        "account_balance": 4340.0, "account_equity": 4351.5,
    })
    assert r.status_code == 200
    rows = {row["key"]: row["value"] for row in conn.execute(
        "SELECT key, value FROM settings WHERE key IN "
        "('account_balance','account_equity','account_at')"
    ).fetchall()}
    assert rows["account_balance"] == "4340.0"
    assert rows["account_equity"] == "4351.5"
    assert "account_at" in rows and rows["account_at"].endswith("+00:00")


def test_post_market_price_without_balance_is_backward_compatible(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/market/price", json={"symbol": "XAUUSD", "bid": 4520.0, "ask": 4520.2})
    assert r.status_code == 200
    n = conn.execute(
        "SELECT COUNT(*) AS c FROM settings WHERE key='account_balance'"
    ).fetchone()["c"]
    assert n == 0  # no balance key written when EA omits the fields
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_post_market_price_persists_balance tests/test_api.py::test_post_market_price_without_balance_is_backward_compatible -v`
Expected: FAIL (balance keys not written / field ignored).

- [ ] **Step 3: Add optional fields to the model**

In `src/api_models.py`, replace the `MarketPriceBody` class body:

```python
class MarketPriceBody(BaseModel):
    """Heartbeat from the EA so the AI prompt has a current price for
    two-digit SL shorthand decoding (e.g. "ستوبك 56" -> 4856 only if we
    know gold is around 4850).

    account_balance/account_equity are optional — added 2026-06-01 so the
    GUI's pending-resize warning can express risk as a % of balance. Older
    EA builds that POST without them keep working.
    """
    symbol: str = "XAUUSD"
    bid: float
    ask: float
    account_balance: float | None = None
    account_equity: float | None = None
```

- [ ] **Step 4: Persist the fields in the handler**

In `src/api.py` `post_market_price`, inside the `for key, val in (...)` tuple (after the three market rows, before the closing `)`), the loop only covers market keys. Replace the `conn.execute("BEGIN")` block body so balance is written when present. Change the tuple-build to:

```python
        rows_to_write = [
            (f"market_{sym}_bid", str(body.bid)),
            (f"market_{sym}_ask", str(body.ask)),
            (f"market_{sym}_at", now),
        ]
        if body.account_balance is not None:
            rows_to_write.append(("account_balance", str(body.account_balance)))
            rows_to_write.append(("account_at", now))
        if body.account_equity is not None:
            rows_to_write.append(("account_equity", str(body.account_equity)))
        conn.execute("BEGIN")
        try:
            for key, val in rows_to_write:
                conn.execute(
                    "INSERT INTO settings(key, value) VALUES(?,?) "
                    "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (key, val),
                )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
```

(Delete the original `for key, val in ( ... ):` loop and its `BEGIN/COMMIT` wrapper that you are replacing.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_post_market_price_persists_balance tests/test_api.py::test_post_market_price_without_balance_is_backward_compatible -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/api_models.py src/api.py tests/test_api.py
git commit -m "feat(api): persist account balance/equity on market-price heartbeat"
```

---

## Task 2: Pending-risk estimate helper (API, pure function)

**Files:**
- Modify: `src/api.py` (module-level helper, near the other private helpers at top of file)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py` (add `from src.api import _pending_risk` near the top imports, or import inline as shown):

```python
def test_pending_risk_dollars_and_pct():
    from src.api import _pending_risk
    # SL distance 7.23, lots 0.43, XAUUSD contract 100 -> 7.23*100*0.43 = 310.89
    dollars, pct = _pending_risk(lots=0.43, entry=4501.49, sl=4494.26, balance=4340.0)
    assert round(dollars, 2) == 310.89
    assert round(pct, 2) == round(310.89 / 4340.0 * 100, 2)


def test_pending_risk_pct_none_when_no_balance():
    from src.api import _pending_risk
    dollars, pct = _pending_risk(lots=0.06, entry=4501.49, sl=4494.26, balance=None)
    assert round(dollars, 2) == round(7.23 * 100 * 0.06, 2)
    assert pct is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_pending_risk_dollars_and_pct tests/test_api.py::test_pending_risk_pct_none_when_no_balance -v`
Expected: FAIL (`ImportError: cannot import name '_pending_risk'`).

- [ ] **Step 3: Implement the helper**

In `src/api.py`, near the top (after imports / alongside `_parse_payload = parse_payload`), add:

```python
# XAUUSD contract size in ounces: a 1.00 price move on 1.0 lot = $100.
# Used only for the GUI resize warning — the EA does the broker-precise
# clamp/feasibility check at placement time.
_XAUUSD_CONTRACT_SIZE = 100.0


def _pending_risk(
    lots: float, entry: float, sl: float, balance: float | None
) -> tuple[float, float | None]:
    """Estimate the dollars-at-risk if the SL hits at `lots`, and that as a
    percent of `balance`. `pct` is None when balance is unavailable.
    """
    sl_distance = abs(entry - sl)
    dollars = sl_distance * _XAUUSD_CONTRACT_SIZE * lots
    pct = (dollars / balance * 100.0) if balance and balance > 0 else None
    return dollars, pct
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py::test_pending_risk_dollars_and_pct tests/test_api.py::test_pending_risk_pct_none_when_no_balance -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/api.py tests/test_api.py
git commit -m "feat(api): add _pending_risk estimate helper"
```

---

## Task 3: Resize-pending endpoint (API)

**Files:**
- Modify: `src/api_models.py` (add `ResizePendingBody`)
- Modify: `src/api.py` (import the model; add the endpoint inside `build_app`, near `get_action` at `:1035`)
- Test: `tests/test_api.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
def _insert_watching_open(conn, lots_entry=4501.49, sl=4494.26):
    payload = {
        "symbol": "XAUUSD", "side": "BUY",
        "entry_low": lots_entry, "entry_high": lots_entry,
        "tps": [4544.0], "sl": sl, "pending": True, "pending_type": "limit",
    }
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'watching')",
        (json.dumps(payload),),
    )
    conn.commit()
    return cur.lastrowid


def test_resize_pending_writes_override_and_bumps_seq(tmp_path):
    conn = _setup(tmp_path)
    conn.execute("INSERT INTO settings(key,value) VALUES('account_balance','4340.0')")
    conn.execute(
        "INSERT INTO settings(key,value) VALUES('account_at', ?)",
        (datetime.now(timezone.utc).isoformat(),),
    )
    aid = _insert_watching_open(conn)
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/resize_pending", json={"lots": 0.43})
    assert r.status_code == 200
    body = r.json()
    assert body["lots"] == 0.43
    assert body["resize_seq"] == 1
    assert round(body["risk_dollars"], 2) == 310.89
    assert body["risk_pct_estimate"] is not None
    row = conn.execute("SELECT payload_json, status FROM actions WHERE id=?", (aid,)).fetchone()
    payload = json.loads(row["payload_json"])
    assert payload["pending_lot_override"] == 0.43
    assert payload["resize_seq"] == 1
    assert row["status"] == "watching"  # unchanged
    # second call bumps seq monotonically
    r2 = client.post(f"/actions/{aid}/resize_pending", json={"lots": 0.30})
    assert r2.json()["resize_seq"] == 2


def test_resize_pending_rejects_non_watching(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'executed')",
        (json.dumps({"pending": True, "entry_low": 1, "entry_high": 1, "sl": 1}),),
    )
    conn.commit()
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{cur.lastrowid}/resize_pending", json={"lots": 0.1})
    assert r.status_code == 409


def test_resize_pending_rejects_non_pending_open(tmp_path):
    conn = _setup(tmp_path)
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'watching')",
        (json.dumps({"entry_low": 1, "entry_high": 1, "sl": 1}),),  # no "pending"
    )
    conn.commit()
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{cur.lastrowid}/resize_pending", json={"lots": 0.1})
    assert r.status_code == 409


def test_resize_pending_unknown_id_404(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    r = client.post("/actions/9999/resize_pending", json={"lots": 0.1})
    assert r.status_code == 404


def test_resize_pending_rejects_bad_lots(tmp_path):
    conn = _setup(tmp_path)
    aid = _insert_watching_open(conn)
    client = TestClient(build_app(conn))
    assert client.post(f"/actions/{aid}/resize_pending", json={"lots": 0}).status_code == 422
    assert client.post(f"/actions/{aid}/resize_pending", json={"lots": -1}).status_code == 422
    assert client.post(f"/actions/{aid}/resize_pending", json={"lots": 999}).status_code == 422


def test_resize_pending_pct_none_when_balance_stale(tmp_path):
    conn = _setup(tmp_path)
    stale = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
    conn.execute("INSERT INTO settings(key,value) VALUES('account_balance','4340.0')")
    conn.execute("INSERT INTO settings(key,value) VALUES('account_at', ?)", (stale,))
    aid = _insert_watching_open(conn)
    client = TestClient(build_app(conn))
    r = client.post(f"/actions/{aid}/resize_pending", json={"lots": 0.43})
    assert r.status_code == 200
    assert r.json()["risk_pct_estimate"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -k resize_pending -v`
Expected: FAIL (404/endpoint missing).

- [ ] **Step 3: Add the request model**

In `src/api_models.py`, after `MarketPriceBody`, add:

```python
class ResizePendingBody(BaseModel):
    """GUI -> API: set an explicit new lot for a pending (watching) OPEN.
    The operator value bypasses the EA's per-trade risk cap by design; the
    EA still clamps to broker VOLUME_MIN/STEP/MAX at placement.
    """
    lots: float
```

- [ ] **Step 4: Implement the endpoint**

In `src/api.py`, add `ResizePendingBody` to the model imports at the top (the `from src.api_models import (...)` block). Then, inside `build_app`, immediately after the `get_action` function (ends at `:1055`), add:

```python
    # Max lots accepted from the operator override. Pulled from
    # settings.risk_budget.max_open_lots when present; otherwise a sane
    # absolute ceiling so a fat-finger can't request 500 lots.
    _RESIZE_MAX_LOTS_FALLBACK = 100.0
    _BALANCE_STALE_SEC = 60

    def _max_open_lots() -> float:
        row = conn.execute(
            "SELECT value FROM settings WHERE key='risk_budget'"
        ).fetchone()
        if row and row["value"]:
            try:
                return float(json.loads(row["value"]).get("max_open_lots")
                             or _RESIZE_MAX_LOTS_FALLBACK)
            except (ValueError, TypeError):
                pass
        return _RESIZE_MAX_LOTS_FALLBACK

    def _fresh_balance() -> float | None:
        rows = {
            r["key"]: r["value"]
            for r in conn.execute(
                "SELECT key, value FROM settings WHERE key IN "
                "('account_balance','account_at')"
            ).fetchall()
        }
        bal, at = rows.get("account_balance"), rows.get("account_at")
        if bal is None or at is None:
            return None
        try:
            age = (datetime.now(timezone.utc)
                   - datetime.fromisoformat(at)).total_seconds()
        except ValueError:
            return None
        if age > _BALANCE_STALE_SEC:
            return None
        try:
            return float(bal)
        except ValueError:
            return None

    @app.post("/actions/{action_id}/resize_pending")
    def resize_pending(action_id: int, body: ResizePendingBody):
        """Set an explicit new lot on a pending (watching) OPEN. Writes
        `pending_lot_override` + a monotonic `resize_seq` into the action
        payload; the EA picks it up on its next /actions/{id} poll, deletes
        the broker order, and re-places at the new lot. Status is unchanged.
        """
        if body.lots <= 0 or body.lots > _max_open_lots():
            raise HTTPException(422, f"lots out of range: {body.lots}")
        row = conn.execute(
            "SELECT action_type, payload_json, status FROM actions WHERE id=?",
            (action_id,),
        ).fetchone()
        if row is None:
            raise HTTPException(404)
        payload = parse_payload(row["payload_json"]) or {}
        if (row["status"] != "watching" or row["action_type"] != "OPEN"
                or payload.get("pending") is not True):
            raise HTTPException(
                409,
                f"action {action_id} is not a pending watching OPEN "
                f"(status={row['status']}, type={row['action_type']}, "
                f"pending={payload.get('pending')})",
            )
        payload["pending_lot_override"] = body.lots
        payload["resize_seq"] = int(payload.get("resize_seq", 0)) + 1
        conn.execute(
            "UPDATE actions SET payload_json=? WHERE id=?",
            (json.dumps(payload), action_id),
        )
        conn.commit()
        entry = (float(payload.get("entry_low", 0)) + float(payload.get("entry_high", 0))) / 2.0
        dollars, pct = _pending_risk(body.lots, entry, float(payload.get("sl", 0)),
                                     _fresh_balance())
        return {
            "lots": body.lots,
            "resize_seq": payload["resize_seq"],
            "risk_dollars": dollars,
            "risk_pct_estimate": pct,
        }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -k resize_pending -v`
Expected: PASS (6 tests)

- [ ] **Step 6: Run the full API suite (no regressions)**

Run: `.venv\Scripts\python.exe -m pytest tests/test_api.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add src/api_models.py src/api.py tests/test_api.py
git commit -m "feat(api): POST /actions/{id}/resize_pending for pending lot override"
```

---

## Task 4: EA — extract `PlacePendingLimit` helper (refactor, no behavior change)

**Files:**
- Modify: `ea/CopyTrades.mq5` (`DoOpen` pending block `:1565-1647`; add helper above `DoOpen`)

This is MQL5 — no Python unit harness. Verification is **compile + manual injection**.

- [ ] **Step 1: Add the helper function**

Above `void DoOpen(...)` (before `:1498`), add:

```cpp
// Places a broker BuyLimit/SellLimit at `entryLimit` for `lots`, pushes a
// PendingOrder into g_pending_orders[], persists it, and POSTs status=
// 'watching' (retry-queued on transport failure). Returns the broker order
// ticket, or 0 on failure (caller has already had a result POSTed for the
// failure case). Shared by the initial open (DoOpen) and the resize path.
ulong PlacePendingLimit(long id, bool isBuyP, double entryLimit, double sl,
                        double &tps[], int tpCount, double lots) {
   double tpFinalP = tps[tpCount - 1];
   bool okP = isBuyP
      ? trade.BuyLimit(lots, entryLimit, Symbol_Override, sl, tpFinalP,
                       ORDER_TIME_GTC, 0, "ct-pending")
      : trade.SellLimit(lots, entryLimit, Symbol_Override, sl, tpFinalP,
                        ORDER_TIME_GTC, 0, "ct-pending");
   if(!okP) {
      PostResult(id, "failed", 0,
         "pending_place_failed:" + IntegerToString(trade.ResultRetcode()));
      g_stats_rejected++;
      return 0;
   }
   ulong order_ticket = trade.ResultOrder();
   if(order_ticket == 0) {
      PostResult(id, "failed", 0, "pending_no_order_ticket");
      g_stats_rejected++;
      return 0;
   }
   PendingOrder po;
   po.action_id = id;
   po.order_ticket = order_ticket;
   po.isBuy = isBuyP;
   po.entry = entryLimit;
   po.sl = sl;
   for(int kp = 0; kp < 3; kp++) po.tps[kp] = 0.0;
   for(int kp = 0; kp < tpCount; kp++) po.tps[kp] = tps[kp];
   po.tpCount = tpCount;
   po.placedAt = TimeCurrent();
   po.lastStatusCheck = TimeCurrent();
   po.appliedResizeSeq = 0;
   int nP = ArraySize(g_pending_orders);
   ArrayResize(g_pending_orders, nP + 1);
   g_pending_orders[nP] = po;
   PersistPendingOrder(po);

   string watchBody = StringFormat(
      "{\"status\":\"watching\",\"error\":\"pending_order_ticket=%I64u\"}",
      order_ticket);
   string watchResp; int watchStatus;
   string watchUrl = ApiBaseUrl + "/actions/" + IntegerToString(id) + "/result";
   bool watchOk = HttpPostJsonWithStatus(watchUrl, watchBody, watchResp, watchStatus);
   if(!watchOk) {
      if(IsRetryableStatus(watchStatus)) {
         Print("Watching POST failed for pending action ", id,
               " status=", watchStatus, " — queued for retry");
         EnqueueRetry(watchUrl, watchBody);
      } else {
         Print("Watching POST terminal ", watchStatus,
               " for pending action ", id, " — not retrying");
      }
   }
   Print("CT OPEN id=", id, " pending limit placed ticket=", order_ticket,
         " entry=", entryLimit, " sl=", sl, " tp=", tpFinalP);
   return order_ticket;
}
```

Note: `po.appliedResizeSeq` is set here; the struct field is added in Task 6 Step 1. If implementing strictly in order, add the struct field (Task 6 Step 1) before compiling.

- [ ] **Step 2: Replace the inline block in `DoOpen` with a call**

In `DoOpen`, replace lines `:1587-1647` (from `double tpFinalP = tps[tpCount - 1];` through the `return;` that ends the `if(isPending)` block, i.e. everything after `lotsP = ApplyEvalSizing(...)` handling) with:

```cpp
      if(PlacePendingLimit(id, isBuyP, entryLimit, sl, tps, tpCount, lotsP) == 0)
         return;  // PlacePendingLimit already POSTed the failure
      return;
```

Keep the preceding lines intact (`MaybeSynthesizeLadder`, `LotsFromRisk`, the `lotsP <= 0` reject, and `ApplyEvalSizing`). The `tpFinalP` local is now owned by the helper.

- [ ] **Step 3: Compile**

Open `ea/CopyTrades.mq5` in MetaEditor (F4 in MT5), press F7.
Expected: `0 errors, 0 warnings` (warnings about unused locals are acceptable; fix if any reference a deleted variable).

- [ ] **Step 4: Commit**

```bash
git add ea/CopyTrades.mq5
git commit -m "refactor(ea): extract PlacePendingLimit helper from DoOpen"
```

---

## Task 5: EA — include balance/equity in the heartbeat

**Files:**
- Modify: `ea/CopyTrades.mq5` `HeartbeatMarketPrice` (`:412-426`)

- [ ] **Step 1: Add balance/equity to the POST body**

Replace the `string body = StringFormat(...)` statement in `HeartbeatMarketPrice` with:

```cpp
   double bal = AccountInfoDouble(ACCOUNT_BALANCE);
   double eq  = AccountInfoDouble(ACCOUNT_EQUITY);
   string body = StringFormat(
      "{\"symbol\":\"%s\",\"bid\":%.5f,\"ask\":%.5f,"
      "\"account_balance\":%.2f,\"account_equity\":%.2f}",
      Symbol_Override, bid, ask, bal, eq
   );
```

- [ ] **Step 2: Compile**

F7 in MetaEditor. Expected: `0 errors`.

- [ ] **Step 3: Commit**

```bash
git add ea/CopyTrades.mq5
git commit -m "feat(ea): report account balance/equity on market-price heartbeat"
```

---

## Task 6: EA — resize detection in `ManagePendingOrders`

**Files:**
- Modify: `ea/CopyTrades.mq5` `PendingOrder` struct (`:270-281`); `ManagePendingOrders` (`:4316-4338`)

- [ ] **Step 1: Add `appliedResizeSeq` to the struct**

In `struct PendingOrder { ... }`, add after `datetime lastStatusCheck;`:

```cpp
   int        appliedResizeSeq; // highest resize_seq already applied (idempotency)
```

(If `PersistPendingOrder`/`LoadPendingOrders` serialize fields positionally, append this field at the END of their format strings too so persisted in-flight orders round-trip. Default a missing value to 0 on load.)

- [ ] **Step 2: Add the resize step in `ManagePendingOrders`**

In the step-3 block (`:4322-4337`), after the existing `GET /actions/{id}` fetch and the `rejected` handling, and BEFORE the closing braces of the loop, add a resize branch. Replace:

```cpp
      string statusBody;
      string statusUrl = ApiBaseUrl + "/actions/" + IntegerToString(p.action_id);
      if(!HttpGet(statusUrl, statusBody) || statusBody == "") continue;
      if(StringFind(statusBody, "\"status\":\"rejected\"") >= 0) {
```

…with:

```cpp
      string statusBody;
      string statusUrl = ApiBaseUrl + "/actions/" + IntegerToString(p.action_id);
      if(!HttpGet(statusUrl, statusBody) || statusBody == "") continue;

      // Resize request? The API writes pending_lot_override + a monotonic
      // resize_seq into the payload. Apply the latest unseen seq by
      // delete + re-place at the new lot (same entry/SL/TP, same magic).
      string payloadBlock = JsonField(statusBody, "payload");
      int newSeq = (int)StringToInteger(JsonField(payloadBlock, "resize_seq"));
      double overrideLot = StringToDouble(JsonField(payloadBlock, "pending_lot_override"));
      if(newSeq > p.appliedResizeSeq && overrideLot > 0.0) {
         double newLot = NormalizeVolume(Symbol_Override, overrideLot);
         // Feasibility: ensure free margin covers the new lot BEFORE deleting
         // the existing order, so an infeasible request never leaves us naked.
         double marginNeeded = 0.0;
         ENUM_ORDER_TYPE otype = p.isBuy ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
         bool feasible = OrderCalcMargin(otype, Symbol_Override, newLot, p.entry, marginNeeded)
                         && marginNeeded <= AccountInfoDouble(ACCOUNT_MARGIN_FREE);
         if(!feasible) {
            PostResult(p.action_id, "failed", 0, "resize_insufficient_margin");
            p.appliedResizeSeq = newSeq;            // don't retry an impossible resize
            g_pending_orders[i] = p;
            PersistPendingOrder(p);
            Print("CT resize REJECT action=", p.action_id, " newLot=", newLot,
                  " marginNeeded=", marginNeeded, " — kept existing order");
            continue;
         }
         if(!trade.OrderDelete(p.order_ticket)) {
            Print("CT resize OrderDelete FAILED action=", p.action_id,
                  " ticket=", p.order_ticket, " retcode=", trade.ResultRetcode(),
                  " — will retry next tick");
            continue;  // leave seq unbumped; retry next tick
         }
         ErasePendingOrderState(p.order_ticket);
         RemovePendingOrder(i);                     // drop the old struct entry
         double tpsArr[];
         ArrayResize(tpsArr, p.tpCount);
         for(int k = 0; k < p.tpCount; k++) tpsArr[k] = p.tps[k];
         ulong newTicket = PlacePendingLimit(p.action_id, p.isBuy, p.entry,
                                             p.sl, tpsArr, p.tpCount, newLot);
         if(newTicket == 0) {
            Print("CT resize re-place FAILED action=", p.action_id,
                  " — old order already deleted; failure POSTed by helper");
         } else {
            // Stamp appliedResizeSeq on the freshly pushed struct entry so the
            // same seq isn't applied twice.
            int last = ArraySize(g_pending_orders) - 1;
            if(last >= 0 && g_pending_orders[last].action_id == p.action_id) {
               g_pending_orders[last].appliedResizeSeq = newSeq;
               PersistPendingOrder(g_pending_orders[last]);
            }
            Print("CT resize OK action=", p.action_id, " newLot=", newLot,
                  " newTicket=", newTicket, " seq=", newSeq);
         }
         continue;
      }

      if(StringFind(statusBody, "\"status\":\"rejected\"") >= 0) {
```

Notes for the implementer:
- `NormalizeVolume(symbol, lots)` must clamp to `SYMBOL_VOLUME_MIN/STEP/MAX`. If a helper of that name does not already exist in the EA, add a small one near `LotsFromRisk`:
  ```cpp
  double NormalizeVolume(string sym, double lots) {
     double mn = SymbolInfoDouble(sym, SYMBOL_VOLUME_MIN);
     double mx = SymbolInfoDouble(sym, SYMBOL_VOLUME_MAX);
     double st = SymbolInfoDouble(sym, SYMBOL_VOLUME_STEP);
     if(st <= 0) st = 0.01;
     double v = MathFloor(lots / st) * st;
     if(v < mn) v = mn;
     if(v > mx) v = mx;
     return NormalizeDouble(v, 2);
  }
  ```
- `JsonField` is the EA's existing extractor (used throughout); nested `payload` extraction mirrors `BuildOpenPayloadFromLastClosed` which already does `JsonField(sigBlock, ...)` on a nested object.
- Because the loop iterates `i` downward and the resize path calls `RemovePendingOrder(i)` then pushes a NEW entry at the end, the `continue` is required so we don't reuse the stale local `p` for index `i`.

- [ ] **Step 3: Compile**

F7 in MetaEditor. Expected: `0 errors`. Resolve any missing-symbol errors (e.g., add `NormalizeVolume` if absent).

- [ ] **Step 4: Manual smoke test (demo terminal)**

1. Start the stack (`launch.bat`) and attach the EA to a demo XAUUSD chart with the SMC API port.
2. Inject a pending OPEN: `python scripts/test_ea_signal.py` (or a `buy limit` payload) so an order sits in `watching`.
3. `curl -X POST http://127.0.0.1:8766/actions/<id>/resize_pending -H "Content-Type: application/json" -d "{\"lots\":0.10}"`.
4. Within ~10s confirm in the MT5 Experts log: `CT resize OK action=<id> newLot=0.10 newTicket=...`, the old pending order is gone, a new one exists at 0.10, and `GET /actions/<id>` still shows `status=watching` with the new `pending_order_ticket`.

- [ ] **Step 5: Commit**

```bash
git add ea/CopyTrades.mq5
git commit -m "feat(ea): apply pending_lot_override via delete+replace in ManagePendingOrders"
```

---

## Task 7: GUI — `ResizeControl` widget

**Files:**
- Create: `src/gui/panels/resize_control.py`

- [ ] **Step 1: Create the widget**

```python
"""Inline control to resize a pending (watching) OPEN order from the DETAIL panel."""
from __future__ import annotations

import json
import urllib.request

from PySide6.QtCore import QThread, Signal
from PySide6.QtWidgets import (
    QDoubleSpinBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from src.gui.theme import current_palette


class _ResizeWorker(QThread):
    """POSTs the resize request off the UI thread."""
    done = Signal(bool, str)  # (ok, message)

    def __init__(self, url: str, lots: float) -> None:
        super().__init__()
        self._url = url
        self._lots = lots

    def run(self) -> None:
        try:
            data = json.dumps({"lots": self._lots}).encode()
            req = urllib.request.Request(
                self._url, data=data, method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=5) as resp:
                body = json.loads(resp.read().decode())
            self.done.emit(True, json.dumps(body))
        except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
            try:
                detail = json.loads(e.read().decode()).get("detail", str(e))
            except Exception:
                detail = f"HTTP {e.code}"
            self.done.emit(False, str(detail))
        except Exception as e:  # noqa: BLE001 - surface any transport error inline
            self.done.emit(False, str(e))


class ResizeControl(QWidget):
    """Current-lot label + editable lot + Apply + risk warning.

    `risk_cap_pct` is the operator's per-trade SL-risk cap (max_sl_loss_percent)
    used only to decide whether to show the amber 'above cap' warning.
    """

    def __init__(self, api_base: str, action_id: int, current_lot: float,
                 risk_cap_pct: float, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._api_base = api_base.rstrip("/")
        self._action_id = action_id
        self._risk_cap_pct = risk_cap_pct
        self._worker: _ResizeWorker | None = None

        pal = current_palette()
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 6, 0, 6)

        row = QHBoxLayout()
        row.addWidget(QLabel(f"Lot ({current_lot:.2f}):"))
        self._spin = QDoubleSpinBox()
        self._spin.setDecimals(2)
        self._spin.setSingleStep(0.01)
        self._spin.setRange(0.01, 100.0)
        self._spin.setValue(current_lot if current_lot > 0 else 0.01)
        row.addWidget(self._spin)
        self._apply = QPushButton("Apply")
        self._apply.clicked.connect(self._on_apply)
        row.addWidget(self._apply)
        row.addStretch()
        root.addLayout(row)

        self._status = QLabel("")
        self._status.setWordWrap(True)
        self._status.setStyleSheet(f"color: {pal.text_muted}; font-size: 11px;")
        root.addWidget(self._status)

    def _on_apply(self) -> None:
        lots = round(self._spin.value(), 2)
        self._apply.setEnabled(False)
        self._status.setText(f"Applying {lots:.2f}…")
        url = f"{self._api_base}/actions/{self._action_id}/resize_pending"
        self._worker = _ResizeWorker(url, lots)
        self._worker.done.connect(self._on_done)
        self._worker.start()

    def _on_done(self, ok: bool, message: str) -> None:
        self._apply.setEnabled(True)
        pal = current_palette()
        if not ok:
            self._status.setStyleSheet("color: #ef5350; font-size: 11px;")
            self._status.setText(f"Resize failed: {message}")
            return
        body = json.loads(message)
        pct = body.get("risk_pct_estimate")
        dollars = body.get("risk_dollars")
        if pct is None:
            note = f"risks ≈ ${dollars:,.0f} if SL hits (balance unknown)"
            color = pal.text_muted
        elif pct > self._risk_cap_pct:
            note = (f"~{pct:.1f}% of balance — above your {self._risk_cap_pct:.1f}% "
                    f"cap (≈ ${dollars:,.0f})")
            color = "#ff9800"
        else:
            note = f"~{pct:.1f}% of balance (≈ ${dollars:,.0f})"
            color = "#26a69a"
        self._status.setStyleSheet(f"color: {color}; font-size: 11px;")
        self._status.setText(f"Resized to {body['lots']:.2f} (seq {body['resize_seq']}). {note}")
```

- [ ] **Step 2: Import-smoke check**

Run: `.venv\Scripts\python.exe -c "from src.gui.panels.resize_control import ResizeControl; print('ok')"`
Expected: `ok` (no import errors).

- [ ] **Step 3: Commit**

```bash
git add src/gui/panels/resize_control.py
git commit -m "feat(gui): ResizeControl widget for pending-order lot override"
```

---

## Task 8: GUI — wire `ResizeControl` into DetailPanel + LiveView

**Files:**
- Modify: `src/gui/panels/detail_panel.py` (constructor + `_render_action`)
- Modify: `src/gui/views/live_view.py` (`:36` DetailPanel construction)

- [ ] **Step 1: Pass `api_base` + risk cap into DetailPanel**

In `src/gui/panels/detail_panel.py`, change the constructor signature and store the values:

```python
    def __init__(self, subscriber: DBSubscriber, db_path: Path,
                 api_base: str = "", risk_cap_pct: float = 1.0) -> None:
        super().__init__()
        self._subscriber = subscriber
        self._db_path = db_path
        self._api_base = api_base
        self._risk_cap_pct = risk_cap_pct
```

(Leave the rest of `__init__` unchanged.)

- [ ] **Step 2: Render the control for watching OPEN actions**

In `detail_panel.py`, add this import at the top:

```python
from src.gui.panels.resize_control import ResizeControl
```

Then, inside `_render_action(self, action)`, after the existing detail rows are added and before the trailing `addStretch()`, insert. `ActionRow` (`src/gui/models/actions_model.py:159`) already exposes `id: int`, `action_type: str`, `status: str`, and a parsed `payload: dict` — use them directly:

```python
        payload = action.payload or {}
        if (action.status == "watching" and action.action_type == "OPEN"
                and payload.get("pending") is True and self._api_base):
            current_lot = float(payload.get("pending_lot_override")
                                or payload.get("volume") or 0.0)
            self._content_layout.addWidget(_section_title("RESIZE PENDING LOT"))
            self._content_layout.addWidget(
                ResizeControl(self._api_base, action.id, current_lot,
                              self._risk_cap_pct)
            )
```

The pending lot is usually not stored in the payload until a resize sets `pending_lot_override`; `0.00` is an acceptable default for the "current" label (the actual broker lot shows in the positions/journal once filled).

- [ ] **Step 3: Pass `api_base` from LiveView**

In `src/gui/views/live_view.py`, change the DetailPanel construction (`:36`) to:

```python
        self._detail_panel = DetailPanel(
            self._subscriber, self._stack.db_path,
            api_base=self._stack.api_base,
        )
```

(`Stack.api_base` is defined at `src/gui/services/stack_registry.py:55`.)

- [ ] **Step 4: Import-smoke + launch check**

Run: `.venv\Scripts\python.exe -c "from src.gui.views.live_view import LiveView; from src.gui.panels.detail_panel import DetailPanel; print('ok')"`
Expected: `ok`.

Then manually: launch the GUI (`launch_gui.bat`), open the Live tab, select a `watching` OPEN action, confirm the "RESIZE PENDING LOT" row appears with a spinbox + Apply, enter a new lot, click Apply, and confirm the status line shows the resized lot + risk note.

- [ ] **Step 5: Commit**

```bash
git add src/gui/panels/detail_panel.py src/gui/views/live_view.py
git commit -m "feat(gui): show ResizeControl for pending OPEN actions in DETAIL panel"
```

---

## Task 9: Integration test — lifecycle continuity

**Files:**
- Create: `tests/test_resize_integration.py`

- [ ] **Step 1: Write the test**

```python
"""End-to-end-ish: a watching OPEN gets resized; a mock EA loop polls the
action, observes the override, and 're-places' by POSTing watching with a new
ticket. Asserts the action row stays a single continuous 'watching' lifecycle.
"""
import json
from datetime import datetime, timezone
from fastapi.testclient import TestClient
from src.db import connect, init_schema
from src.api import build_app


def _setup(tmp_path):
    conn = connect(str(tmp_path / "api.db"))
    init_schema(conn)
    return conn


def test_resize_then_mock_ea_replace_keeps_single_lifecycle(tmp_path):
    conn = _setup(tmp_path)
    client = TestClient(build_app(conn))
    payload = {
        "symbol": "XAUUSD", "side": "BUY",
        "entry_low": 4501.49, "entry_high": 4501.49,
        "tps": [4544.0], "sl": 4494.26, "pending": True, "pending_type": "limit",
    }
    cur = conn.execute(
        "INSERT INTO actions(action_type, payload_json, status) "
        "VALUES('OPEN', ?, 'watching')",
        (json.dumps(payload),),
    )
    conn.commit()
    aid = cur.lastrowid

    # Operator resizes.
    r = client.post(f"/actions/{aid}/resize_pending", json={"lots": 0.43})
    assert r.status_code == 200 and r.json()["resize_seq"] == 1

    # Mock EA poll: read the action, see the override.
    got = client.get(f"/actions/{aid}").json()
    assert got["status"] == "watching"
    assert got["payload"]["pending_lot_override"] == 0.43
    assert got["payload"]["resize_seq"] == 1

    # Mock EA re-places: POST watching with a NEW broker ticket.
    rep = client.post(
        f"/actions/{aid}/result",
        json={"status": "watching", "error": "pending_order_ticket=222222"},
    )
    assert rep.status_code == 200

    # Still exactly one row, still watching, ticket updated.
    rows = conn.execute("SELECT status, ea_response FROM actions WHERE id=?", (aid,)).fetchall()
    assert len(rows) == 1
    assert rows[0]["status"] == "watching"
    assert "222222" in (rows[0]["ea_response"] or "")
```

- [ ] **Step 2: Run it**

Run: `.venv\Scripts\python.exe -m pytest tests/test_resize_integration.py -v`
Expected: PASS. (If `POST /actions/{id}/result` rejects a `watching`→`watching` transition, adjust the assertion to match the lifecycle CHECK constraint, or use the documented re-watching path — confirm against `schema.sql` and `api.py post_result`.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_resize_integration.py
git commit -m "test: pending resize keeps a single continuous action lifecycle"
```

---

## Task 10: Documentation

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Document the endpoint, settings keys, and payload fields**

In `CLAUDE.md`, under the `src/api.py` bullet (the Phase-1 endpoints sentence), add a sentence:

```
`POST /actions/{id}/resize_pending` sets an explicit `pending_lot_override` + monotonic `resize_seq` on a `watching` pending OPEN (operator override that bypasses the EA's risk cap); the EA applies it on its next `/actions/{id}` poll by delete+replace. The `/market/price` heartbeat now also persists `account_balance`/`account_equity`/`account_at` (settings keys) so the GUI's resize warning can express risk as a % of balance (STALE after 60s → falls back to absolute dollars).
```

Under the EA bullet (`ExecuteOne` dispatcher / `ManagePendingOrders` mention), add:

```
`ManagePendingOrders` also honors `pending_lot_override`/`resize_seq` from the action payload: it feasibility-checks margin, deletes the broker pending order, and re-places at the new lot via the shared `PlacePendingLimit` helper, keeping the same action row (only the broker ticket swaps).
```

- [ ] **Step 2: Run the full hermetic suite (final regression gate)**

Run: `.venv\Scripts\python.exe -m pytest -q`
Expected: all pass (168 + new tests).

- [ ] **Step 3: Commit**

```bash
git add CLAUDE.md
git commit -m "docs: document pending-order resize endpoint and balance heartbeat"
```

---

## Self-Review Notes (coverage map)

| Spec section | Task |
|---|---|
| Balance heartbeat extension (API + EA) | 1, 5 |
| `_pending_risk` helper | 2 |
| `POST /actions/{id}/resize_pending` + validation | 3 |
| EA `PlacePendingLimit` refactor (DRY) | 4 |
| EA struct `appliedResizeSeq` + resize step + feasibility/error handling | 6 |
| GUI resize control + warning (bypass-cap-but-warn) | 7, 8 |
| Lifecycle continuity (single row, ticket swap) | 9 |
| Edge cases (fill-mid-resize handled by step ordering; monotonic seq; 409 after expiry) | 3, 6, 9 |
| Docs / conventions | 10 |

**Known limitation (documented in spec):** EA MQL5 logic (Tasks 4–6) has no Python unit harness — verified by compile + manual injection. Keep EA deltas minimal and well-logged.
