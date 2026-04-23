"""Rolling deliberation memory for the AI loop.

Instead of feeding the last 20 raw Telegram messages into every AI call, we
keep a distilled log of prior non-ignore AI responses. Each entry is a short
human-readable summary of what the model concluded about that message
(context / signal / partial_signal). On OPEN, the memory is cleared — the
deliberation resolved into an executable trade.

Cap is applied two ways: entry count and age, whichever kicks in first.
"""
from __future__ import annotations

import sqlite3
from typing import Iterable

from src.validators import AIResponse, AlertAction, OpenAction


def summarize(response: AIResponse) -> str:
    """Render an AI response into a one-line memory entry.

    Primary OPEN → trade line; ALERT → alert text; other action types →
    type + payload. Falls back to the reasoning's first sentence.
    """
    category = response.category or "context"
    if response.actions:
        primary = response.actions[0]
        if isinstance(primary, OpenAction):
            tps = ",".join(f"{t:g}" for t in primary.tps)
            body = (
                f"{primary.side} {primary.symbol} "
                f"entry={primary.entry_low:g}-{primary.entry_high:g} "
                f"sl={primary.sl:g} tps={tps}"
            )
            return f"{category}: {body}"
        if isinstance(primary, AlertAction):
            return f"{category}: {primary.text.strip()}"
        dumped = primary.model_dump()
        dumped.pop("type", None)
        return f"{category}: {primary.type} {dumped}"
    first_sentence = (response.reasoning or "").split(".", 1)[0].strip()
    return f"{category}: {first_sentence}" if first_sentence else f"{category}: (no detail)"


def record(
    conn: sqlite3.Connection,
    message_id: int,
    chat_id: int,
    category: str,
    summary: str,
) -> int:
    """Insert a memory entry scoped to a chat. Returns row id. Ignores 'ignore'."""
    if category == "ignore":
        return 0
    if category not in ("context", "signal", "partial_signal"):
        raise ValueError(f"unexpected category: {category}")
    cur = conn.execute(
        "INSERT INTO signal_memory(message_id, chat_id, category, summary) VALUES(?,?,?,?)",
        (message_id, chat_id, category, summary),
    )
    return cur.lastrowid


def load_active(
    conn: sqlite3.Connection,
    chat_id: int,
    max_entries: int,
    max_age_hours: int,
) -> list[sqlite3.Row]:
    """Return active entries for a chat (not cleared, within age cap), oldest first.

    When the active set exceeds max_entries we keep the newest ones and drop
    the oldest so the model always sees the most recent deliberation.
    """
    rows = conn.execute(
        "SELECT id, message_id, chat_id, category, summary, created_at "
        "FROM signal_memory "
        "WHERE chat_id=? "
        "  AND cleared_at IS NULL "
        "  AND created_at > datetime('now', ?) "
        "ORDER BY id DESC "
        "LIMIT ?",
        (chat_id, f"-{max_age_hours} hours", max_entries),
    ).fetchall()
    return list(reversed(rows))


def render(entries: Iterable[sqlite3.Row]) -> str:
    """Format entries for the AI prompt."""
    lines = ["DELIBERATION MEMORY (prior non-ignore messages, oldest first):"]
    entries = list(entries)
    if not entries:
        lines.append("  (empty — no prior deliberation)")
        return "\n".join(lines)
    for r in entries:
        lines.append(f"  [{r['created_at']}] {r['summary']}")
    return "\n".join(lines)


def clear_on_open(conn: sqlite3.Connection, chat_id: int) -> int:
    """Mark active memory for one chat as cleared. Called after a successful OPEN.

    Scoped per chat so a resolved signal in channel A doesn't wipe the
    in-flight deliberation in channel B. Returns number of rows cleared.
    """
    cur = conn.execute(
        "UPDATE signal_memory SET cleared_at = CURRENT_TIMESTAMP "
        "WHERE chat_id=? AND cleared_at IS NULL",
        (chat_id,),
    )
    return cur.rowcount
