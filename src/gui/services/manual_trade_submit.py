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
