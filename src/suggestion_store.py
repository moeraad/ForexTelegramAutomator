"""Storage for proposed cheap-layer rules awaiting operator approval.

Nothing here activates a rule — the GUI Accept handler (profile_writer) is
the sole writer into profile.json. This module only persists, dedups, lists,
and expires proposals.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass


@dataclass(frozen=True)
class Suggestion:
    id: int
    source_channel_id: str
    rule_kind: str
    target_layer: str
    payload: dict
    evidence: dict
    status: str
    created_at: str


def dedupe_key(rule_kind: str, payload: dict) -> str:
    """Stable hash of rule_kind + normalized payload so the same proposal
    isn't re-queued while pending."""
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(f"{rule_kind}|{blob}".encode("utf-8")).hexdigest()


def _row(r: sqlite3.Row) -> Suggestion:
    return Suggestion(
        id=r["id"], source_channel_id=r["source_channel_id"],
        rule_kind=r["rule_kind"], target_layer=r["target_layer"],
        payload=json.loads(r["payload_json"]),
        evidence=json.loads(r["evidence_json"]),
        status=r["status"], created_at=r["created_at"],
    )


def add(conn: sqlite3.Connection, *, source_channel_id: str, rule_kind: str,
        target_layer: str, payload: dict, evidence: dict) -> int | None:
    """Insert a proposal. Returns None if an identical proposal is already
    pending (unique partial index on (channel, dedupe_key) where proposed)."""
    key = dedupe_key(rule_kind, payload)
    cur = conn.execute(
        "INSERT OR IGNORE INTO learning_suggestions("
        "  source_channel_id, rule_kind, target_layer, payload_json, "
        "  evidence_json, dedupe_key) VALUES(?,?,?,?,?,?)",
        (source_channel_id, rule_kind, target_layer,
         json.dumps(payload, ensure_ascii=False),
         json.dumps(evidence, ensure_ascii=False), key),
    )
    return cur.lastrowid if cur.rowcount else None


def list_proposed(conn: sqlite3.Connection,
                  source_channel_id: str | None = None) -> list[Suggestion]:
    if source_channel_id is None:
        rows = conn.execute(
            "SELECT * FROM learning_suggestions WHERE status='proposed' "
            "ORDER BY created_at DESC, id DESC").fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM learning_suggestions "
            "WHERE status='proposed' AND source_channel_id=? "
            "ORDER BY created_at DESC, id DESC", (source_channel_id,)).fetchall()
    return [_row(r) for r in rows]


def get(conn: sqlite3.Connection, sid: int) -> Suggestion | None:
    r = conn.execute(
        "SELECT * FROM learning_suggestions WHERE id=?", (sid,)).fetchone()
    return _row(r) if r else None


def set_status(conn: sqlite3.Connection, sid: int, status: str) -> bool:
    cur = conn.execute(
        "UPDATE learning_suggestions SET status=?, decided_at=CURRENT_TIMESTAMP "
        "WHERE id=?", (status, sid))
    return (cur.rowcount or 0) > 0


def expire_old(conn: sqlite3.Connection, ttl_days: int) -> int:
    cur = conn.execute(
        "UPDATE learning_suggestions SET status='expired', "
        "decided_at=CURRENT_TIMESTAMP "
        "WHERE status='proposed' AND created_at < datetime('now', ?)",
        (f"-{ttl_days} days",))
    return cur.rowcount or 0
