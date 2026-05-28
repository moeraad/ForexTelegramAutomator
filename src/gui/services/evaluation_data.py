"""Queries for the Evaluation view.

Joins closed positions with their originating action's `evaluation`
payload so the GUI can render: per-trade rows (with score + verdict +
realized P&L), calibration buckets (score band -> actual win rate),
regime breakdown (per regime tag -> win rate + sample count).

All functions read-only; the view is a forensic surface, not an editor.
"""
from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


# Action types the evaluator runs on. Must match the orchestrator's
# kick gates and the api.py post-execution kick gate — keeping this in
# one place is awkward (it's also defined in api.py) but the GUI must
# not silently surface trades the evaluator never scored.
_EVAL_ACTION_TYPES = ("OPEN", "OPEN_INSTANT", "REOPEN_LAST", "REINFORCE")


@dataclass(frozen=True)
class EvaluatedTrade:
    """One closed position joined with its originating evaluation.

    `evaluation` is None when the action has no evaluation key (legacy
    rows from before the evaluator was wired, or a worker crash). The
    view shows these as "no eval" rather than dropping them, because
    knowing how many fired without a score is a useful signal.
    """
    ticket: int
    action_id: int
    action_type: str
    side: str
    original_volume: float
    entry_price: float
    exit_price: float
    realized_pnl: float
    close_reason: str
    opened_at: str
    closed_at: str
    hold_minutes: int | None
    evaluation: dict[str, Any] | None
    # v2 per-channel attribution (Step 11). Empty string on pre-tagging
    # rows so the view renders "—" rather than failing the dataclass.
    source_channel_id: str = ""


@dataclass(frozen=True)
class CalibrationBucket:
    """Aggregate stats for one score band."""
    band_label: str           # e.g. "60-69"
    band_lo: int
    band_hi: int
    n: int                    # trades in this band
    wins: int                 # n with realized_pnl > 0
    win_rate: float           # wins / n (0..1)
    avg_pnl: float
    p_profit_avg: float | None  # mean p_profit_used from sizing block


@dataclass(frozen=True)
class CalibrationStatus:
    """Summary of how mature the evaluator's calibration data is.

    `n_scored` — closed trades with any evaluation dict (the X-axis of
    the calibration view).
    `n_with_p_profit` — closed trades whose synthesizer also wrote a
    `sizing.p_profit_used` (the subset where claimed-vs-realized can be
    compared). Always <= n_scored.
    `band` — a coarse state label the GUI banner colors by:
        "no_data" | "uncalibrated" | "partial" | "calibrating"
    `message` — one-line operator-facing summary.
    """
    n_scored: int
    n_with_p_profit: int
    band: str
    message: str


# Thresholds for the calibration band. Chosen as rules of thumb:
#   < 20 trades  -> a single 5-trade band is statistical noise
#   20-49        -> bands are forming but per-band counts are <10 each
#   >= 50        -> at least ~8/band on average; bars carry signal
_CALIBRATION_BAND_THRESHOLDS = (20, 50)


def calibration_status(trades: list[EvaluatedTrade]) -> CalibrationStatus:
    """Summarize evaluator-calibration maturity for the banner widget.

    Counts trades with an evaluation dict, and the subset with a
    synthesizer-claimed p_profit_used (used by the calibration chart's
    'claimed vs realized' overlay). Pure function: same trades -> same
    status, lets the GUI banner re-render without re-querying the DB.
    """
    n_scored = sum(1 for t in trades if t.evaluation is not None)
    n_with_p = 0
    for t in trades:
        if t.evaluation is None:
            continue
        sizing = t.evaluation.get("sizing")
        if isinstance(sizing, dict) and sizing.get("p_profit_used") is not None:
            n_with_p += 1
    lo, hi = _CALIBRATION_BAND_THRESHOLDS
    if n_scored == 0:
        band = "no_data"
        message = (
            "No closed v2-evaluated trades yet. The Calibration tab will "
            "fill in once trades start closing."
        )
    elif n_scored < lo:
        band = "uncalibrated"
        message = (
            f"Only {n_scored} closed evaluated trade(s). Probabilities "
            f"are LLM priors, not statistical estimates — treat the "
            f"score as a filter, not a predictor."
        )
    elif n_scored < hi:
        band = "partial"
        message = (
            f"{n_scored} closed evaluated trades ({n_with_p} with a "
            f"claimed p_profit). Bands are forming but each is still "
            f"under ~10 samples — directionally informative, not yet "
            f"statistically calibrated."
        )
    else:
        band = "calibrating"
        message = (
            f"{n_scored} closed evaluated trades ({n_with_p} with a "
            f"claimed p_profit). The calibration chart's bands are "
            f"statistically meaningful — compare the bars to the "
            f"diagonal to see whether the evaluator is calibrated, "
            f"overconfident, or pessimistic."
        )
    return CalibrationStatus(
        n_scored=n_scored,
        n_with_p_profit=n_with_p,
        band=band,
        message=message,
    )


@dataclass(frozen=True)
class RegimeBucket:
    """Aggregate stats for one regime tag."""
    regime_summary: str       # full pipe-joined tag from RegimeTag.summary
    n: int
    wins: int
    win_rate: float
    avg_pnl: float
    avg_score: float


def _since_iso(days: int | None) -> str | None:
    if days is None:
        return None
    if days == 0:
        now = datetime.now(timezone.utc)
        return now.replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def _safe_dt(raw: str | None) -> datetime | None:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00").replace(" ", "T")
        if not (s.endswith("Z") or "+" in s[10:] or "-" in s[10:]):
            s = s + "+00:00"
        return datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None


def _hold_minutes(opened_at: str | None, closed_at: str | None) -> int | None:
    a = _safe_dt(opened_at)
    b = _safe_dt(closed_at)
    if a is None or b is None:
        return None
    delta = (b - a).total_seconds()
    return int(delta / 60) if delta >= 0 else None


def evaluated_trades(db_path: Path, days: int | None = 30) -> list[EvaluatedTrade]:
    """Closed trades joined with their evaluator output.

    Joined via `positions.action_id = actions.id` and filtered to action
    types the evaluator scores. Ordered newest-first so the per-trade
    table shows recent rows at the top.

    Days parameter controls the lookback window via `positions.closed_at`.
    None means "all time" — useful once enough history accumulates that
    the calibration buckets are statistically meaningful (>50 trades).
    """
    if not db_path.exists():
        return []
    since = _since_iso(days)
    placeholders = ",".join("?" * len(_EVAL_ACTION_TYPES))
    sql = (
        "SELECT p.mt5_ticket, p.action_id, a.action_type, p.side, "
        "       p.original_volume, p.entry_price, p.exit_price, "
        "       p.realized_pnl, p.close_reason, p.opened_at, p.closed_at, "
        "       a.payload_json, a.source_channel_id "
        "FROM positions p "
        "JOIN actions a ON a.id = p.action_id "
        "WHERE p.status='closed' "
        f"  AND a.action_type IN ({placeholders}) "
    )
    params: list = list(_EVAL_ACTION_TYPES)
    if since is not None:
        sql += " AND p.closed_at >= ? "
        params.append(since)
    sql += " ORDER BY p.closed_at DESC"

    out: list[EvaluatedTrade] = []
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(sql, params).fetchall()
    for r in rows:
        try:
            payload = json.loads(r["payload_json"]) if r["payload_json"] else {}
        except (TypeError, ValueError):
            payload = {}
        evaluation = payload.get("evaluation")
        if not isinstance(evaluation, dict):
            evaluation = None
        try:
            sci = r["source_channel_id"]
        except (IndexError, KeyError):
            sci = None
        out.append(EvaluatedTrade(
            ticket=int(r["mt5_ticket"]),
            action_id=int(r["action_id"]),
            action_type=str(r["action_type"]),
            side=str(r["side"]).upper(),
            original_volume=float(r["original_volume"] or 0.0),
            entry_price=float(r["entry_price"] or 0.0),
            exit_price=float(r["exit_price"] or 0.0),
            realized_pnl=float(r["realized_pnl"] or 0.0),
            close_reason=str(r["close_reason"] or ""),
            opened_at=str(r["opened_at"] or ""),
            closed_at=str(r["closed_at"] or ""),
            hold_minutes=_hold_minutes(r["opened_at"], r["closed_at"]),
            evaluation=evaluation,
            source_channel_id=str(sci) if sci is not None else "",
        ))
    return out


# Bands chosen to match the verdict bands so dashboard color and
# calibration histogram align visually. The 0-39 band is intentionally
# sparse — those trades are "avoid" and there shouldn't be many; merging
# everything below 40 keeps the histogram readable.
_CALIBRATION_BANDS: tuple[tuple[int, int, str], ...] = (
    (0, 40, "0-39"),
    (40, 50, "40-49"),
    (50, 60, "50-59"),
    (60, 70, "60-69"),
    (70, 80, "70-79"),
    (80, 101, "80+"),
)


def calibration_buckets(trades: list[EvaluatedTrade]) -> list[CalibrationBucket]:
    """Group trades by score band and compute realized stats per band.

    Trades without an evaluation are dropped from this aggregation — the
    point is to compare claimed-score-to-realized-outcome, and an absent
    score has nothing to compare. The per-trade view still shows them.
    """
    by_band: dict[str, list[EvaluatedTrade]] = defaultdict(list)
    for t in trades:
        if t.evaluation is None:
            continue
        try:
            score = int(t.evaluation.get("score") or 0)
        except (TypeError, ValueError):
            continue
        for lo, hi, label in _CALIBRATION_BANDS:
            if lo <= score < hi:
                by_band[label].append(t)
                break
    out: list[CalibrationBucket] = []
    for lo, hi, label in _CALIBRATION_BANDS:
        bucket_trades = by_band.get(label, [])
        n = len(bucket_trades)
        wins = sum(1 for t in bucket_trades if t.realized_pnl > 0)
        # Mean realized P&L per trade in this band.
        avg = (
            sum(t.realized_pnl for t in bucket_trades) / n if n > 0 else 0.0
        )
        # Synthesizer's claimed p_profit when present — lets us compare
        # claimed-vs-realized win rate, the core calibration metric.
        p_values = [
            float(t.evaluation.get("sizing", {}).get("p_profit_used"))
            for t in bucket_trades
            if t.evaluation
            and isinstance(t.evaluation.get("sizing"), dict)
            and t.evaluation["sizing"].get("p_profit_used") is not None
        ]
        p_avg = sum(p_values) / len(p_values) if p_values else None
        out.append(CalibrationBucket(
            band_label=label,
            band_lo=lo,
            band_hi=hi,
            n=n,
            wins=wins,
            win_rate=wins / n if n > 0 else 0.0,
            avg_pnl=avg,
            p_profit_avg=p_avg,
        ))
    return out


def regime_buckets(
    trades: list[EvaluatedTrade], min_samples: int = 3
) -> list[RegimeBucket]:
    """Group trades by their regime.summary tag.

    `min_samples` filters out one-off regimes that aren't statistically
    interpretable. Default 3 — anything less than that and the win rate
    is noise. Operator can drop the threshold later when more history
    accumulates.

    Returned sorted by sample count descending so the most-encountered
    regimes appear first.
    """
    by_regime: dict[str, list[EvaluatedTrade]] = defaultdict(list)
    for t in trades:
        if t.evaluation is None:
            continue
        regime = t.evaluation.get("regime")
        if not isinstance(regime, dict):
            continue
        summary = regime.get("summary")
        if not isinstance(summary, str) or not summary:
            continue
        by_regime[summary].append(t)
    out: list[RegimeBucket] = []
    for tag, bucket in by_regime.items():
        if len(bucket) < min_samples:
            continue
        wins = sum(1 for t in bucket if t.realized_pnl > 0)
        avg_pnl = sum(t.realized_pnl for t in bucket) / len(bucket)
        scores = [
            float(t.evaluation.get("score") or 0)
            for t in bucket
            if t.evaluation
        ]
        avg_score = sum(scores) / len(scores) if scores else 0.0
        out.append(RegimeBucket(
            regime_summary=tag,
            n=len(bucket),
            wins=wins,
            win_rate=wins / len(bucket),
            avg_pnl=avg_pnl,
            avg_score=avg_score,
        ))
    out.sort(key=lambda r: r.n, reverse=True)
    return out
