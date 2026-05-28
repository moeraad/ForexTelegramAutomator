"""Parse logs/ai_calls.jsonl and aggregate token + cost metrics.

The log file is provider-agnostic (Anthropic + OpenAI both normalize usage
into input_tokens / output_tokens / cache_read_tokens / cache_creation_tokens)
so we can sum totals without knowing the model. Cost estimates use rough
"reference" rates that the caller can override.
"""
from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_INPUT_PER_M = 3.0
DEFAULT_OUTPUT_PER_M = 15.0
DEFAULT_CACHE_READ_PER_M = 0.30


@dataclass(frozen=True)
class CallRecord:
    ts: datetime
    msg_id: int | None
    stage: str
    decision: str | None
    latency_ms: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    error: str | None
    # Step 18: cost attribution tags. Both blank ("") on pre-Step-18 rows.
    source_channel_id: str = ""
    route_id: str = ""

    @property
    def is_error(self) -> bool:
        return bool(self.error)

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.cache_read_tokens + self.cache_creation_tokens

    def estimated_cost(
        self,
        input_per_m: float = DEFAULT_INPUT_PER_M,
        output_per_m: float = DEFAULT_OUTPUT_PER_M,
        cache_read_per_m: float = DEFAULT_CACHE_READ_PER_M,
    ) -> float:
        return (
            self.input_tokens * input_per_m
            + self.output_tokens * output_per_m
            + self.cache_read_tokens * cache_read_per_m
        ) / 1_000_000.0


@dataclass
class CostSummary:
    calls: int = 0
    triage_calls: int = 0
    interpret_calls: int = 0
    errors: int = 0
    triage_keep: int = 0
    triage_ignore: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    estimated_cost_usd: float = 0.0
    per_day: dict[str, float] = field(default_factory=dict)

    @property
    def cache_hit_rate(self) -> float:
        denom = self.input_tokens + self.cache_read_tokens
        if denom == 0:
            return 0.0
        return self.cache_read_tokens / denom

    @property
    def triage_keep_rate(self) -> float:
        denom = self.triage_keep + self.triage_ignore
        if denom == 0:
            return 0.0
        return self.triage_keep / denom

    @property
    def error_rate(self) -> float:
        if self.calls == 0:
            return 0.0
        return self.errors / self.calls

    @property
    def total_tokens(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )


def _parse_ts(raw: str) -> datetime | None:
    if not raw:
        return None
    try:
        s = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except ValueError:
        return None


def _parse_line(line: str) -> CallRecord | None:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    ts = _parse_ts(obj.get("ts", ""))
    if ts is None:
        return None
    stage = str(obj.get("stage") or ("error" if obj.get("error") else "(unknown)"))
    return CallRecord(
        ts=ts,
        msg_id=int(obj["msg_id"]) if isinstance(obj.get("msg_id"), int) else None,
        stage=stage,
        decision=obj.get("decision"),
        latency_ms=int(obj.get("latency_ms") or 0),
        input_tokens=int(obj.get("input_tokens") or 0),
        output_tokens=int(obj.get("output_tokens") or 0),
        cache_read_tokens=int(obj.get("cache_read_tokens") or 0),
        cache_creation_tokens=int(obj.get("cache_creation_tokens") or 0),
        error=obj.get("error"),
        source_channel_id=str(obj.get("source_channel_id") or ""),
        route_id=str(obj.get("route_id") or ""),
    )


def load_records(log_path: Path, days: int | None) -> list[CallRecord]:
    if not log_path.exists():
        return []
    cutoff: datetime | None = None
    if days is not None:
        if days == 0:
            now = datetime.now(timezone.utc)
            cutoff = now.replace(hour=0, minute=0, second=0, microsecond=0)
        else:
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    out: list[CallRecord] = []
    with log_path.open("r", encoding="utf-8", errors="replace") as f:
        for line in f:
            rec = _parse_line(line)
            if rec is None:
                continue
            if cutoff is not None and rec.ts < cutoff:
                continue
            out.append(rec)
    out.sort(key=lambda r: r.ts)
    return out


def summarize(
    records: list[CallRecord],
    input_per_m: float = DEFAULT_INPUT_PER_M,
    output_per_m: float = DEFAULT_OUTPUT_PER_M,
    cache_read_per_m: float = DEFAULT_CACHE_READ_PER_M,
) -> CostSummary:
    s = CostSummary()
    per_day: dict[str, float] = defaultdict(float)
    for r in records:
        s.calls += 1
        if r.is_error:
            s.errors += 1
        if r.stage == "triage":
            s.triage_calls += 1
            if r.decision == "keep":
                s.triage_keep += 1
            elif r.decision == "ignore":
                s.triage_ignore += 1
        elif r.stage == "interpret":
            s.interpret_calls += 1
        s.input_tokens += r.input_tokens
        s.output_tokens += r.output_tokens
        s.cache_read_tokens += r.cache_read_tokens
        s.cache_creation_tokens += r.cache_creation_tokens
        cost = r.estimated_cost(input_per_m, output_per_m, cache_read_per_m)
        s.estimated_cost_usd += cost
        per_day[r.ts.date().isoformat()] += cost
    s.per_day = dict(sorted(per_day.items()))
    return s


def summarize_by_key(
    records: list[CallRecord],
    key_fn,
    *,
    input_per_m: float = DEFAULT_INPUT_PER_M,
    output_per_m: float = DEFAULT_OUTPUT_PER_M,
    cache_read_per_m: float = DEFAULT_CACHE_READ_PER_M,
) -> dict[str, CostSummary]:
    """Group records by a callable key, then ``summarize`` each group.

    Step 18: backs ``summarize_by_channel`` / ``summarize_by_route`` /
    any future per-axis breakdown without duplicating the loop.

    Records whose ``key_fn(record)`` returns a falsy value (empty string,
    None) are bucketed under the literal key ``"(unattributed)"`` so the
    operator can see cost that's missing channel/route tags (legacy rows
    or pre-Step-18 entries). Returning the empty dict on no records.
    """
    buckets: dict[str, list[CallRecord]] = {}
    for r in records:
        key = key_fn(r) or "(unattributed)"
        buckets.setdefault(key, []).append(r)
    return {
        k: summarize(v, input_per_m, output_per_m, cache_read_per_m)
        for k, v in buckets.items()
    }


def summarize_by_channel(
    records: list[CallRecord],
    *,
    input_per_m: float = DEFAULT_INPUT_PER_M,
    output_per_m: float = DEFAULT_OUTPUT_PER_M,
    cache_read_per_m: float = DEFAULT_CACHE_READ_PER_M,
) -> dict[str, CostSummary]:
    """Cost breakdown per ``source_channel_id``.

    Useful for "which channel costs me the most?" — particularly relevant
    once aggregate routing (Step 12) puts multiple channels through one
    destination, making the per-destination total ambiguous.
    """
    return summarize_by_key(
        records, lambda r: r.source_channel_id,
        input_per_m=input_per_m, output_per_m=output_per_m,
        cache_read_per_m=cache_read_per_m,
    )


def summarize_by_route(
    records: list[CallRecord],
    *,
    input_per_m: float = DEFAULT_INPUT_PER_M,
    output_per_m: float = DEFAULT_OUTPUT_PER_M,
    cache_read_per_m: float = DEFAULT_CACHE_READ_PER_M,
) -> dict[str, CostSummary]:
    """Cost breakdown per ``route_id``.

    Useful with mirror routing (Step 11): each leg's cost separately.
    Note that AI is called ONCE per message regardless of how many routes
    fan out — so per-route totals are derived from the leg's
    ``route_id`` tag at log time, not a literal re-multiplication.
    """
    return summarize_by_key(
        records, lambda r: r.route_id,
        input_per_m=input_per_m, output_per_m=output_per_m,
        cache_read_per_m=cache_read_per_m,
    )


def expensive_calls(records: list[CallRecord], top: int = 10) -> list[CallRecord]:
    ranked = sorted(
        records,
        key=lambda r: r.estimated_cost(),
        reverse=True,
    )
    return ranked[:top]
