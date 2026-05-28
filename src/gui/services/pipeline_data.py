"""Pipeline-view queries: per-bucket counts + message listing.

Powers src/gui/views/pipeline_view.py. Reads directly from the stack's
SQLite — no API roundtrip — same pattern as journal_data.py.

The orchestrator writes one row of the 7 terminal-stage decisions
(prefilter_drop, trigger_text, trigger_embedding, trigger_ignore,
triage_ignored, interpreted_signal, interpreted_ignore) onto every
message it sees.
Messages without a decision (pre-migration history or in-flight) appear
under the "unknown" bucket so the operator can spot orphans.
"""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


PIPELINE_BUCKETS: tuple[str, ...] = (
    "prefilter_drop",
    "trigger_text",
    "trigger_embedding",
    "trigger_ignore",
    "triage_ignored",
    "interpreted_signal",
    "interpreted_ignore",
)

# Display labels + colors, kept in one place so the chart and the table
# badges stay in sync. Colors picked to match the existing dashboard
# palette family (cyan/lime/amber/coral/magenta/slate).
BUCKET_LABELS: dict[str, str] = {
    "prefilter_drop":     "Pre-filter drop",
    "trigger_text":       "Trigger (text)",
    "trigger_embedding":  "Trigger (embedding)",
    "trigger_ignore":     "Trigger ignore",
    "triage_ignored":     "Triage ignored",
    "interpreted_signal": "Interpreter signal",
    "interpreted_ignore": "Interpreter ignore",
    "unknown":            "Unknown / in-flight",
}

BUCKET_COLORS: dict[str, str] = {
    "prefilter_drop":     "#5A7090",  # muted slate — dropped at the door
    "trigger_text":       "#00E5FF",  # cyan — operator-curated text match
    "trigger_embedding":  "#39FF14",  # lime — embedding match
    "trigger_ignore":     "#3FA796",  # teal — operator-curated noise drop
    "triage_ignored":     "#FFB300",  # amber — LLM dropped it
    "interpreted_signal": "#FF1F8F",  # magenta — full interpreter signal
    "interpreted_ignore": "#A8302F",  # coral — interpreter saw nothing
    "unknown":            "#1A2240",  # near-black — pre-migration / NULL
}


@dataclass(frozen=True)
class PipelineMessage:
    id: int
    received_at: str
    sender: str
    text: str
    source_channel_id: str
    decided_stage: str | None
    decided_outcome: str | None
    decided_at: str | None
    pipeline_meta: dict | None
    action_ids: tuple[int, ...]


def _since_clause(hours: int | None) -> tuple[str, tuple]:
    if hours is None or hours <= 0:
        return "", ()
    return "received_at >= datetime('now', ?)", (f"-{int(hours)} hours",)


def bucket_counts(db_path: str | Path, since_hours: int | None) -> dict[str, int]:
    """Return {bucket_name: count} for all 7 buckets + 'unknown'.

    Every bucket is present in the result (zero if no rows), so the chart
    can render a stable legend across refreshes.
    """
    where_pred, params = _since_clause(since_hours)
    where_sql = f"WHERE {where_pred}" if where_pred else ""
    counts: dict[str, int] = {b: 0 for b in PIPELINE_BUCKETS}
    counts["unknown"] = 0
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT COALESCE(decided_stage, 'unknown') AS bucket, "
            f"COUNT(*) AS n FROM messages {where_sql} GROUP BY bucket",
            params,
        ).fetchall()
        for r in rows:
            counts[r["bucket"]] = r["n"]
    finally:
        conn.close()
    return counts


def messages(
    db_path: str | Path,
    *,
    since_hours: int | None = 24,
    stage: str | None = None,
    limit: int = 500,
) -> list[PipelineMessage]:
    """Return up to `limit` messages, newest first, filtered by stage.

    `stage="unknown"` returns rows with `decided_stage IS NULL`.
    `stage=None` returns every row in the window regardless of decision.
    """
    clauses: list[str] = []
    params: list = []
    where_pred, ws_params = _since_clause(since_hours)
    if where_pred:
        clauses.append(where_pred)
        params.extend(ws_params)
    if stage == "unknown":
        clauses.append("decided_stage IS NULL")
    elif stage:
        clauses.append("decided_stage = ?")
        params.append(stage)
    where_sql = ("WHERE " + " AND ".join(clauses)) if clauses else ""

    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            f"SELECT m.id, m.received_at, m.sender, m.text, "
            f"m.source_channel_id, m.decided_stage, m.decided_outcome, "
            f"m.decided_at, m.pipeline_meta_json, "
            f"(SELECT GROUP_CONCAT(a.id) FROM actions a "
            f" WHERE a.source_msg_id = m.id) AS action_ids "
            f"FROM messages m {where_sql} "
            f"ORDER BY m.received_at DESC LIMIT ?",
            (*params, int(limit)),
        ).fetchall()
    finally:
        conn.close()

    out: list[PipelineMessage] = []
    for r in rows:
        meta: dict | None = None
        if r["pipeline_meta_json"]:
            try:
                meta = json.loads(r["pipeline_meta_json"])
            except (TypeError, ValueError):
                meta = None
        ids_csv = r["action_ids"] or ""
        action_ids = tuple(int(x) for x in ids_csv.split(",")) if ids_csv else ()
        out.append(PipelineMessage(
            id=r["id"],
            received_at=r["received_at"] or "",
            sender=r["sender"] or "",
            text=r["text"] or "",
            source_channel_id=r["source_channel_id"] or "",
            decided_stage=r["decided_stage"],
            decided_outcome=r["decided_outcome"],
            decided_at=r["decided_at"],
            pipeline_meta=meta,
            action_ids=action_ids,
        ))
    return out
