"""Non-Qt logic for the Suggestions tab: ranking + accept orchestration.
Kept separate from the Qt view so it is unit-testable."""
from __future__ import annotations

from pathlib import Path

from src import profile_writer, suggestion_store
from src.suggestion_store import Suggestion

# Accuracy-gain kinds rank above cost-saving kinds.
_KIND_RANK = {"keep_trigger": 0, "action_trigger": 1, "noise": 2, "context_drop": 3}


def rank_suggestions(items: list[Suggestion]) -> list[Suggestion]:
    """Order: accuracy gains first; within a kind, one-tap-safe first, then by
    estimated savings (would_suppress/support) descending."""
    def key(s: Suggestion):
        safe = 0 if s.evidence.get("one_tap_safe") else 1
        savings = -(s.evidence.get("would_suppress")
                    or s.evidence.get("support") or 0)
        return (_KIND_RANK.get(s.rule_kind, 9), safe, savings)
    return sorted(items, key=key)


def accept_suggestion(conn, sid: int, *, profile_path: Path) -> None:
    """Write the rule into profile.json then mark the suggestion accepted.
    Order matters: only flip status if the profile write succeeds."""
    s = suggestion_store.get(conn, sid)
    if s is None:
        raise ValueError(f"suggestion {sid} not found")
    profile_writer.apply_suggestion(Path(profile_path), s)
    suggestion_store.set_status(conn, sid, "accepted")


def dismiss_suggestion(conn, sid: int) -> None:
    suggestion_store.set_status(conn, sid, "dismissed")
