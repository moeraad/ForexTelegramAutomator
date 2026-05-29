"""Non-Qt logic for the Suggestions tab: ranking + accept orchestration.
Kept separate from the Qt view so it is unit-testable."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from src import profile_writer, suggestion_store
from src.suggestion_store import Suggestion

# Accuracy-gain kinds rank above cost-saving kinds.
_KIND_RANK = {"keep_trigger": 0, "action_trigger": 1, "noise": 2, "context_drop": 3}


def resolve_profile_path(db_path: Path | str, legacy_path: Path | str | None = None) -> Path:
    """Resolve the profile.json the stack's live interpreter + matcher read.

    The authoritative runtime location is ``<stack db dir>/profile.json`` — the
    same path `trigger_matcher._resolve_profile_path` and `ai._resolve_profile_path`
    compute from `config.DB_PATH` when the listener/bot run for this stack. The
    stack registry's `profile_path` points instead at the legacy bundled
    `channels/<name>.json`, which the live layers do NOT read — so writing there
    would silently never take effect. Prefer the db-adjacent live file; fall back
    to the legacy path only when the live one is absent, and otherwise return the
    live location (so a write targets where the matcher will look).
    """
    live = Path(db_path).parent / "profile.json"
    if live.exists():
        return live
    if legacy_path and Path(legacy_path).exists():
        return Path(legacy_path)
    return live


def rank_suggestions(items: list[Suggestion]) -> list[Suggestion]:
    """Order: accuracy gains first; within a kind, one-tap-safe first, then by
    estimated savings (would_suppress/support) descending."""
    def key(s: Suggestion) -> tuple[int, int, int]:
        safe = 0 if s.evidence.get("one_tap_safe") else 1
        savings = -(s.evidence.get("would_suppress")
                    or s.evidence.get("support") or 0)
        return (_KIND_RANK.get(s.rule_kind, 9), safe, savings)
    return sorted(items, key=key)


def accept_suggestion(conn: sqlite3.Connection, sid: int, *, profile_path: Path) -> None:
    """Write the rule into profile.json then mark the suggestion accepted.
    Order matters: only flip status if the profile write succeeds."""
    s = suggestion_store.get(conn, sid)
    if s is None:
        raise ValueError(f"suggestion {sid} not found")
    profile_writer.apply_suggestion(Path(profile_path), s)
    suggestion_store.set_status(conn, sid, "accepted")


def dismiss_suggestion(conn: sqlite3.Connection, sid: int) -> None:
    suggestion_store.set_status(conn, sid, "dismissed")
