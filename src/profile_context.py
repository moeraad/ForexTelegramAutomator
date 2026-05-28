"""ProfileContext: per-channel AI configuration loaded once, passed by reference.

A ``ProfileContext`` bundles everything the AI pipeline needs to interpret a
specific channel: the parsed profile JSON, the pre-rendered interpreter and
triage system prompts, and the target trading symbol. It is constructed once
per channel at process startup and threaded through ``orchestrator.process_message``
to ``AIClient.call``, ``TriageClient.classify``, ``render_open_positions``, and
the Stage-0 prefilter.

Why this exists (Step 3 of the multi-channel plan): the v1 system relied on
module-level globals (``ai.SYSTEM_PROMPT``, ``ai_triage.TRIAGE_SYSTEM_PROMPT``,
``config.CHANNEL_PROFILE``) that are read at process startup and never change.
Step 12 (deferred) needs the SAME process to serve multiple channels — one API
process receives messages for N channels and must render the right prompt per
message. Routing the profile as an explicit parameter (not a global) is the
foundation.

The module-level globals stay in place for compatibility during the transition
(callers that don't pass a ``profile=`` arg get the same behavior as before).
Step 5 (new shared listener) will be the first caller to always pass a
ProfileContext explicitly.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProfileContext:
    """Everything the AI pipeline needs to interpret one channel.

    Attributes:
        name: Profile identifier (the slug used in v2 config, e.g. "forex_engineer").
        path: Absolute path the profile JSON was loaded from. Useful for
            logging and for refresh-on-mtime-change in a future step.
        data: Parsed JSON contents (``header``, ``vocabulary_table``,
            ``worked_examples``, ``promo_indicators``, etc.). Direct dict
            access lets callers reach niche fields without ProfileContext
            growing a property for each one.
        system_prompt: Pre-rendered interpreter system prompt. Built once
            from ``data`` at construction time.
        triage_prompt: Pre-rendered triage system prompt. Built once.
        symbol: Convenience accessor for ``data.get("symbol", "XAUUSD")``.
    """
    name: str
    path: Path
    data: dict[str, Any] = field(default_factory=dict)
    system_prompt: str = ""
    triage_prompt: str = ""
    symbol: str = "XAUUSD"


def load_profile_context(path: Path, *, name: str | None = None) -> ProfileContext:
    """Load a profile JSON from disk and pre-render both prompts.

    Imports of ``src.ai`` and ``src.ai_triage`` are deferred to avoid a
    circular dependency at module-import time (those modules import from
    here when wired up in subsequent steps).
    """
    raw = json.loads(path.read_text(encoding="utf-8"))
    # Defer imports — `ai` and `ai_triage` will import this module too in
    # later steps; lazy import here breaks the cycle.
    from src.ai import _render_system_prompt_from_data
    from src.ai_triage import _render_triage_prompt_from_data

    system_prompt = _render_system_prompt_from_data(raw)
    triage_prompt = _render_triage_prompt_from_data(raw)
    return ProfileContext(
        name=name or path.stem,
        path=path,
        data=raw,
        system_prompt=system_prompt,
        triage_prompt=triage_prompt,
        symbol=str(raw.get("symbol") or "XAUUSD"),
    )


import os  # noqa: E402 - placed near _cache to keep concerns together

# Memoized loader. Key on absolute path; value carries the file's mtime_ns
# at parse time so subsequent lookups can detect operator edits and reload.
# Day-2 cleanup: previously the cache never expired, so a profile.json
# edit via the GUI required service restart to take effect. The mtime
# check turns that into a per-message check (microseconds) — operator's
# next message after Save picks up the new prompt automatically.
_cache: dict[str, tuple[int, ProfileContext]] = {}


def _file_mtime_ns(path: Path) -> int:
    """Return mtime_ns or 0 if the file is gone (signals 'reload needed')."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def get_profile_context(path: Path, *, name: str | None = None) -> ProfileContext:
    """Cached ``load_profile_context`` with mtime-based reload.

    Cache key is the absolute path. The cached entry records the file's
    mtime_ns at parse time; ``get_profile_context`` re-reads disk only
    when the file's current mtime advances (operator edited it). Same
    mtime → same cached object reference.

    ``os.stat`` on the profile file is the only per-call disk hit
    (microseconds on Windows/Linux). The render cost (string.Template
    substitution + JSON parse) only fires on actual change.
    """
    key = str(path.resolve())
    current_mtime = _file_mtime_ns(path)
    cached = _cache.get(key)
    if cached is not None and cached[0] == current_mtime and current_mtime != 0:
        return cached[1]
    ctx = load_profile_context(path, name=name)
    _cache[key] = (current_mtime, ctx)
    return ctx


def refresh_if_stale(profile: ProfileContext) -> ProfileContext:
    """Return ``profile`` if its on-disk file is unchanged, else a freshly
    loaded ProfileContext from the same path.

    Callers that hold a startup-loaded ProfileContext can call this just
    before passing it down to the orchestrator to pick up live profile
    edits without restarting the service. Returns the SAME object when
    the file hasn't changed (no allocation, no parse).
    """
    return get_profile_context(profile.path, name=profile.name)


def clear_cache() -> None:
    """Invalidate the cache. Tests use this between fixtures."""
    _cache.clear()
