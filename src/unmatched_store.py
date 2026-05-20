"""Capture trigger-matcher misses so the operator can promote them to triggers.

When a live Telegram message slips past both deterministic and embedding
layers of `trigger_matcher.match`, the orchestrator falls through to
Sonnet. If Sonnet emits an action type that *could* legitimately become a
deterministic trigger (CLOSE_PARTIAL, MOVE_SL_BE, etc.), this module
records the message so the operator can later add it as a trigger sample
under the right action_type — closing the loop between AI inference and
curated rules.

What's deliberately NOT captured:
  - Messages Sonnet classified as OPEN / OPEN_INSTANT / ATTACH_SIGNAL /
    MODIFY_TPS / MODIFY / CLOSE / CLOSE_ALL — these aren't
    deterministic-emittable from the matcher (they need price parsing,
    side inference, etc.), so adding them as triggers wouldn't help.
  - Messages Sonnet classified only as ALERT — those are operator-
    informational, not actionable patterns.
  - Trigger matcher *hits* — by definition those already have an
    operator-curated rule.

Lifecycle:
  - INSERT OR IGNORE on `norm_text` so identical resends collapse into
    one row.
  - On promote or dismiss, the row is DELETEd. The table stays
    queue-shaped — pending only, no terminal rows.
  - The cap (`MAX_ROWS`) bounds growth; on insert, oldest rows past the
    cap are pruned so a long-running listener can't fill the DB even if
    the operator never triages.
"""
from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from src.text_normalize import normalize
from src.trigger_matcher import _ALLOWED_ACTION_TYPES

log = logging.getLogger(__name__)

# Cap on rows in the queue. Generous enough that an operator who triages
# once a day won't lose anything, small enough that a runaway listener
# can't bloat the DB. Tunable if needed; pruning is O(rows-MAX_ROWS) on
# each over-cap insert so don't push it into the tens of thousands.
MAX_ROWS = 200


@dataclass(frozen=True)
class UnmatchedRow:
    """One queued candidate for the operator's Triggers tab."""
    id: int
    text: str
    suggested_action_type: str | None
    source_msg_id: int | None
    created_at: str


def _suggested_action_type(actions: list[object]) -> str | None:
    """First action type in the emission list that the deterministic
    matcher could fire. Sonnet often emits compound responses
    (e.g. [CLOSE_PARTIAL, MOVE_SL_BE]) — picking the first emittable one
    is enough to pre-select a bucket in the Add Trigger dialog; the
    operator can change it if they disagree.

    Each action exposes `.type` (a string) via the validators.Action
    Union; we look that up directly rather than importing every concrete
    class to keep this module decoupled.
    """
    for a in actions:
        atype = getattr(a, "type", None)
        if isinstance(atype, str) and atype in _ALLOWED_ACTION_TYPES:
            return atype
    return None


def record(
    conn: sqlite3.Connection,
    *,
    text: str,
    source_msg_id: int | None,
    actions: list[object],
) -> int | None:
    """Capture a Sonnet emission as a trigger candidate when promotable.

    Returns the row id when inserted, None when skipped (no emittable
    action type, empty text, or duplicate norm_text).

    Failures inside this function MUST NOT break the live path —
    callers (orchestrator) treat exceptions as no-ops. Use cautious
    error handling rather than letting an INSERT collision or a missing
    column knock the listener offline.
    """
    if not text or not text.strip():
        return None
    suggested = _suggested_action_type(actions)
    if suggested is None:
        return None
    norm = normalize(text)
    if not norm:
        return None
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO unmatched_messages("
            "  text, norm_text, suggested_action_type, source_msg_id"
            ") VALUES(?, ?, ?, ?)",
            (text, norm, suggested, source_msg_id),
        )
        if cur.rowcount == 0:
            # Duplicate norm_text — same canonical message already queued.
            return None
        inserted_id = cur.lastrowid
        _prune(conn)
        return inserted_id
    except sqlite3.Error as e:
        # Schema mismatch / DB locked / etc. — log and swallow; the
        # capture queue is augmentation, never required for the live
        # pipeline to function.
        log.warning("unmatched_store.record failed: %s", e)
        return None


def _prune(conn: sqlite3.Connection) -> None:
    """Drop oldest rows beyond MAX_ROWS.

    Cheap single DELETE keyed on created_at — the index on
    `unmatched_messages(created_at)` makes the lookup O(log n) and the
    delete proportional to overflow. Idempotent: at-or-below cap is a
    no-op."""
    overflow = conn.execute(
        "SELECT COUNT(*) AS n FROM unmatched_messages"
    ).fetchone()["n"] - MAX_ROWS
    if overflow <= 0:
        return
    conn.execute(
        "DELETE FROM unmatched_messages WHERE id IN ("
        "  SELECT id FROM unmatched_messages "
        "  ORDER BY created_at ASC, id ASC LIMIT ?"
        ")",
        (overflow,),
    )


def list_pending(conn: sqlite3.Connection, limit: int = MAX_ROWS) -> list[UnmatchedRow]:
    """Return queued rows newest-first.

    UI reads this on demand. Order matches what an operator naturally
    expects when triaging — most recent at the top, so freshly captured
    surprises surface immediately.
    """
    rows = conn.execute(
        "SELECT id, text, suggested_action_type, source_msg_id, created_at "
        "FROM unmatched_messages "
        "ORDER BY created_at DESC, id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [
        UnmatchedRow(
            id=r["id"],
            text=r["text"],
            suggested_action_type=r["suggested_action_type"],
            source_msg_id=r["source_msg_id"],
            created_at=r["created_at"],
        )
        for r in rows
    ]


def delete(conn: sqlite3.Connection, row_id: int) -> bool:
    """Remove a queued row (promotion or dismissal both call this).

    Returns True when the row existed; False when it had already been
    deleted (e.g. a concurrent operator action). The boolean is mostly
    diagnostic — neither caller currently changes behavior on the
    result, but it makes the test for "this was a no-op" trivial.
    """
    cur = conn.execute(
        "DELETE FROM unmatched_messages WHERE id=?", (row_id,)
    )
    return cur.rowcount > 0


def count_pending(conn: sqlite3.Connection) -> int:
    """Convenience for badge counts in the GUI."""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM unmatched_messages"
    ).fetchone()
    return int(row["n"]) if row else 0
