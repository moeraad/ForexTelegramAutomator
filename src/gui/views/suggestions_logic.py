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


def _load_v2_config():
    """Load the v2 config, or None if missing / not v2 / unreadable.

    Tolerant by design — the GUI must never crash because the config is
    absent or still v1; callers fall back to the active-stack hint.
    """
    try:
        from src import config_v2
        path = config_v2.config_path()
        if not config_v2.is_v2(path):
            return None
        return config_v2.load_v2(path)
    except Exception:
        return None


def resolve_channel_profile_path(
    source_channel_id: str,
    *,
    config=None,
    fallback_db_path: Path | str | None = None,
    fallback_legacy: Path | str | None = None,
) -> Path:
    """Resolve the live profile.json for a channel via the v2 entity model.

    Walks Channel(``source_channel_id``) → its Route → Destination → that
    destination's DB dir → db-adjacent ``profile.json`` — the exact file the
    listener/bot's ``trigger_matcher`` and triage read at runtime
    (``Path(config.DB_PATH).parent / "profile.json"``). This keys the write to
    the suggestion's OWN channel rather than the GUI's active "stack" handle, so
    it stays correct under multi-channel routing and doesn't depend on the
    legacy stack shim.

    The v2 ``Profile.path`` is deliberately NOT the primary source: it points at
    the bundled ``channels/<name>.json`` location, which the runtime ignores in
    favour of the db-adjacent file (and is frequently stale). It is consulted
    only as a fallback. When the v2 config / channel / route can't be resolved,
    fall back to the caller-supplied active-stack ``db_path`` / ``legacy`` so
    single-stack behaviour is preserved.
    """
    cfg = config if config is not None else _load_v2_config()
    if cfg is not None:
        ch = cfg.channel(source_channel_id)
        if ch is not None:
            prof = cfg.profile(ch.profile_id)
            legacy = prof.path if (prof is not None and prof.path) else None
            for route in cfg.routes_for_channel(ch.id):
                dest = cfg.destination(route.destination_id)
                if dest is not None and dest.db_path:
                    return resolve_profile_path(dest.db_path, legacy)
            # Channel known but not routed anywhere — best effort on legacy.
            if legacy and Path(legacy).exists():
                return Path(legacy)
    if fallback_db_path is not None:
        return resolve_profile_path(fallback_db_path, fallback_legacy)
    if fallback_legacy is not None:
        return Path(fallback_legacy)
    raise FileNotFoundError(
        f"cannot resolve a profile.json for channel {source_channel_id!r}"
    )


def rank_suggestions(items: list[Suggestion]) -> list[Suggestion]:
    """Order: accuracy gains first; within a kind, one-tap-safe first, then by
    estimated savings (would_suppress/support) descending."""
    def key(s: Suggestion) -> tuple[int, int, int]:
        safe = 0 if s.evidence.get("one_tap_safe") else 1
        savings = -(s.evidence.get("would_suppress")
                    or s.evidence.get("support") or 0)
        return (_KIND_RANK.get(s.rule_kind, 9), safe, savings)
    return sorted(items, key=key)


def accept_suggestion(
    conn: sqlite3.Connection,
    sid: int,
    *,
    profile_path: Path | str | None = None,
    config=None,
    fallback_db_path: Path | str | None = None,
    fallback_legacy: Path | str | None = None,
) -> Path:
    """Write the rule into the channel's live profile.json, then mark accepted.

    The write target is resolved from the suggestion's own ``source_channel_id``
    via the v2 model (channel → route → destination → db-adjacent profile.json),
    unless an explicit ``profile_path`` override is supplied. Returns the path
    written. Order matters: only flip status if the profile write succeeds.
    """
    s = suggestion_store.get(conn, sid)
    if s is None:
        raise ValueError(f"suggestion {sid} not found")
    target = (
        Path(profile_path)
        if profile_path is not None
        else resolve_channel_profile_path(
            s.source_channel_id,
            config=config,
            fallback_db_path=fallback_db_path,
            fallback_legacy=fallback_legacy,
        )
    )
    profile_writer.apply_suggestion(target, s)
    suggestion_store.set_status(conn, sid, "accepted")
    return target


def dismiss_suggestion(conn: sqlite3.Connection, sid: int) -> None:
    suggestion_store.set_status(conn, sid, "dismissed")
