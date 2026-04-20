"""Fingerprint an OPEN signal so resent/referenced copies collapse to one id.

The orchestrator uses this to detect duplicate OPEN signals across
recently-received Telegram messages — when a channel re-posts or quotes an
earlier signal, the AI may extract it again. Matching fingerprints within a
time window let us reject the second action as a duplicate.

The fingerprint is intentionally lossy: prices are rounded to bands so small
numeric noise (e.g. a $0.50 entry tweak on a quote) still matches the original.
"""

from src.validators import OpenAction


def _bucket(price: float, band: float) -> int:
    """Round to nearest band, returning an int for stable stringification."""
    return round(price / band)


def signal_fingerprint(action: OpenAction, band: float = 5.0) -> str:
    """Return a stable identifier for the signal bucketed by `band`.

    Two OpenActions produce the same fingerprint when they target the same
    symbol+side and their entry midpoint, SL, and (sorted) TPs fall into
    identical `band`-wide buckets.
    """
    entry_mid = (action.entry_low + action.entry_high) / 2
    entry_b = _bucket(entry_mid, band)
    sl_b = _bucket(action.sl, band)
    tps_b = tuple(sorted(_bucket(t, band) for t in action.tps))
    return f"{action.symbol}|{action.side}|{entry_b}|{sl_b}|{tps_b}"
