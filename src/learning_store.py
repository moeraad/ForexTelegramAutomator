"""Labeled-corpus capture for the Channel Learning Loop.

Every interpreted message is appended here with the interpreter's verdict
(category + emitted action types) and the triage decision. The batch
learner reads this corpus per channel to propose cheap-layer rules.

Capture MUST NEVER break the live trade path — every public function that
runs inline (capture) swallows exceptions and returns None on failure.
Supersedes src/unmatched_store.py.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Sample:
    id: int
    source_channel_id: str
    text: str
    norm_text: str  # stripped raw text used as per-channel dedup key (NOT text_normalize output)
    category: str
    action_types: str
    triage_decision: str
    seen_count: int
    embedding: list[float] | None


def _row_to_sample(r: sqlite3.Row) -> Sample:
    emb = None
    if r["embedding_json"]:
        try:
            emb = json.loads(r["embedding_json"])
        except (TypeError, ValueError):
            emb = None
    return Sample(
        id=r["id"], source_channel_id=r["source_channel_id"], text=r["text"],
        norm_text=r["norm_text"], category=r["category"],
        action_types=r["action_types"], triage_decision=r["triage_decision"],
        seen_count=r["seen_count"], embedding=emb,
    )


def capture(
    conn: sqlite3.Connection,
    *,
    source_channel_id: str,
    route_id: str | None,
    source_msg_id: int | None,
    text: str,
    category: str,
    action_types: str,
    triage_decision: str,
) -> int | None:
    """Append one labeled sample. Dedups on (source_channel_id, norm_text):
    a verbatim resend bumps seen_count + last_seen_at instead of inserting.

    Returns the row id on first insert, None on dedup / blank input / error.
    Exceptions are swallowed — the corpus is augmentation, never required.
    """
    if not source_channel_id or not text or not text.strip():
        return None
    try:
        stripped = text.strip()
        # norm_text stores the raw text so the UNIQUE(source_channel_id, norm_text)
        # constraint deduplicates on exact-text identity. Using the normalized form
        # instead would collapse distinct messages that share the same normalized
        # representation (e.g. "m0".."m9" all normalize to "m n") into a single row,
        # losing corpus diversity. Dedup-on-raw-text is the right semantic here:
        # only byte-identical resends should merge; distinct messages should each
        # get their own row regardless of normalized form.
        norm_text = stripped
        # Check if row already exists before INSERT so we can distinguish
        # first-insert (return id) from dedup (return None) reliably.
        existing = conn.execute(
            "SELECT id FROM learning_samples "
            "WHERE source_channel_id=? AND norm_text=?",
            (source_channel_id, norm_text),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE learning_samples SET seen_count=seen_count+1, "
                "last_seen_at=CURRENT_TIMESTAMP "
                "WHERE source_channel_id=? AND norm_text=?",
                (source_channel_id, norm_text),
            )
            return None
        cur = conn.execute(
            "INSERT INTO learning_samples("
            "  source_channel_id, route_id, source_msg_id, text, norm_text,"
            "  category, action_types, triage_decision, seen_count) "
            "VALUES(?,?,?,?,?,?,?,?,1)",
            (source_channel_id, route_id or None, source_msg_id, stripped, norm_text,
             category, action_types or "", triage_decision or ""),
        )
        return int(cur.lastrowid) if cur.lastrowid else None
    except sqlite3.IntegrityError:
        # Lost a UNIQUE(source_channel_id, norm_text) race with a concurrent
        # writer — first insert won, this is a benign dedup, not a failure.
        log.debug("learning_store.capture: dedup race, row already exists")
        return None
    except Exception as e:  # noqa: BLE001 — live-path safety: never crash msg processing
        log.warning("learning_store.capture failed: %s", e)
        return None


def recent(conn: sqlite3.Connection, source_channel_id: str, limit: int) -> list[Sample]:
    rows = conn.execute(
        "SELECT * FROM learning_samples WHERE source_channel_id=? "
        "ORDER BY id DESC LIMIT ?",
        (source_channel_id, limit),
    ).fetchall()
    return [_row_to_sample(r) for r in rows]


def without_embedding(conn: sqlite3.Connection, source_channel_id: str, limit: int) -> list[Sample]:
    rows = conn.execute(
        "SELECT * FROM learning_samples "
        "WHERE source_channel_id=? AND embedding_json IS NULL "
        "ORDER BY id DESC LIMIT ?",
        (source_channel_id, limit),
    ).fetchall()
    return [_row_to_sample(r) for r in rows]


def set_embedding(conn: sqlite3.Connection, sample_id: int, vec: list[float]) -> None:
    conn.execute(
        "UPDATE learning_samples SET embedding_json=? WHERE id=?",
        (json.dumps(vec), sample_id),
    )


def count_since(conn: sqlite3.Connection, source_channel_id: str, since_id: int) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM learning_samples "
        "WHERE source_channel_id=? AND id>?",
        (source_channel_id, since_id),
    ).fetchone()
    return int(row["n"]) if row else 0


def max_id(conn: sqlite3.Connection, source_channel_id: str) -> int:
    row = conn.execute(
        "SELECT COALESCE(MAX(id),0) AS m FROM learning_samples "
        "WHERE source_channel_id=?",
        (source_channel_id,),
    ).fetchone()
    return int(row["m"]) if row else 0


def prune(conn: sqlite3.Connection, source_channel_id: str, keep: int) -> None:
    """Drop oldest rows beyond `keep` for one channel."""
    conn.execute(
        "DELETE FROM learning_samples WHERE source_channel_id=? AND id NOT IN ("
        "  SELECT id FROM learning_samples WHERE source_channel_id=? "
        "  ORDER BY id DESC LIMIT ?"
        ")",
        (source_channel_id, source_channel_id, keep),
    )
