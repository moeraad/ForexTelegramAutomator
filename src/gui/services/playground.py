"""Prompt Playground: run triage + interpreter on a synthetic message.

Re-renders the system prompts from the active stack's profile (so unsaved
profile edits are NOT applied — user must Save first), reads the active
stack's live state from its SQLite DB, and invokes the configured AI
provider. No DB or filesystem writes.
"""
from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import dataclass

from src import ai, ai_triage, config
from src.gui.services.stack_registry import Stack
from src.state_summary import render_open_positions


@dataclass(frozen=True)
class PlaygroundContext:
    profile: str
    provider: str
    interpreter_model: str
    triage_model: str
    open_count: int
    open_positions_block_preview: str
    elapsed_total_ms: int


@dataclass(frozen=True)
class TriageOutcome:
    decision: str
    raw_text: str
    usage: dict[str, int]
    latency_ms: int


@dataclass(frozen=True)
class InterpretOutcome:
    actions: list[dict]
    reasoning: str
    raw_text: str
    usage: dict[str, int]
    latency_ms: int


@dataclass(frozen=True)
class PlaygroundResult:
    context: PlaygroundContext
    triage: TriageOutcome | None
    interpret: InterpretOutcome | None
    error: str | None


def _refresh_prompts_for(stack: Stack, profile_name: str | None = None) -> None:
    """Reload SYSTEM_PROMPT + TRIAGE_SYSTEM_PROMPT for the playground.

    Without ``profile_name``: legacy behavior — use the stack's default
    profile (config.CHANNEL_PROFILE) loaded via ``ai._render_system_prompt()``.

    With ``profile_name`` (post-v2 Prompts/Playground gap fix): load
    THAT profile's JSON via profile_io and pass to the ``_from_data``
    renderers. Operator editing an aggregate-routing setup can pick any
    profile and the playground will use it.
    """
    # Point config.DB_PATH at the stack's DB so _load_profile resolves
    # profile.json from the correct APPDATA directory rather than doing
    # a name-based lookup that breaks for APPDATA-resident profiles.
    config.DB_PATH = str(stack.db_path)
    config.CHANNEL_PROFILE = profile_name or stack.name
    if profile_name:
        from src.gui.services import profile_io
        data = profile_io.load_profile(profile_name)
        if data:
            ai.SYSTEM_PROMPT = ai._render_system_prompt_from_data(dict(data))  # noqa: SLF001
            ai_triage.TRIAGE_SYSTEM_PROMPT = (
                ai_triage._render_triage_prompt_from_data(dict(data))  # noqa: SLF001
            )
            return
    ai.SYSTEM_PROMPT = ai._render_system_prompt()  # noqa: SLF001
    ai_triage.TRIAGE_SYSTEM_PROMPT = ai_triage._render_triage_prompt()  # noqa: SLF001


def _state_for(stack: Stack) -> tuple[str, int]:
    if not stack.db_path.exists():
        return "(no DB at active stack — running with empty state)", 0
    with sqlite3.connect(str(stack.db_path)) as conn:
        conn.row_factory = sqlite3.Row
        block = render_open_positions(conn)
        cur = conn.execute("SELECT COUNT(*) FROM positions WHERE status='open'")
        open_count = int(cur.fetchone()[0])
    return block, open_count


def _minimal_context(profile: str, open_count: int, preview: str, start: float) -> PlaygroundContext:
    return PlaygroundContext(
        profile=profile,
        provider=config.AI_PROVIDER,
        interpreter_model=(
            config.ANTHROPIC_MODEL if config.AI_PROVIDER == "anthropic" else config.OPENAI_MODEL
        ),
        triage_model=config.AI_TRIAGE_MODEL,
        open_count=open_count,
        open_positions_block_preview=preview,
        elapsed_total_ms=int((time.monotonic() - start) * 1000),
    )


def run_playground(
    stack: Stack,
    message: str,
    provider_override: str | None = None,
    interpreter_model_override: str | None = None,
    profile_name: str | None = None,
) -> PlaygroundResult:
    """Run the active stack's AI pipeline against ``message``.

    ``profile_name`` (post-v2 Prompts/Playground gap fix): when given,
    the SYSTEM_PROMPT + TRIAGE_SYSTEM_PROMPT come from THAT profile's
    JSON instead of the stack's default. ``None`` preserves legacy
    behavior. Aggregate-routing setups (one destination, N channels with
    different profiles) can pick any profile for playgrounding without
    swapping stacks.
    """
    overall_start = time.monotonic()
    active_profile = profile_name or stack.profile_path.stem
    if provider_override:
        config.AI_PROVIDER = provider_override.lower()

    try:
        _refresh_prompts_for(stack, profile_name=profile_name)
    except Exception as e:
        return PlaygroundResult(
            context=_minimal_context(active_profile, 0, "(could not load profile)", overall_start),
            triage=None,
            interpret=None,
            error=f"failed to render prompts: {type(e).__name__}: {e}",
        )

    try:
        open_positions_block, open_count = _state_for(stack)
    except sqlite3.Error as e:
        return PlaygroundResult(
            context=_minimal_context(active_profile, 0, "(db error)", overall_start),
            triage=None,
            interpret=None,
            error=f"failed to read state: {e}",
        )

    triage_outcome: TriageOutcome | None = None
    interpret_outcome: InterpretOutcome | None = None
    error: str | None = None

    triage_model = (
        config.AI_TRIAGE_MODEL
        if config.AI_PROVIDER == "anthropic"
        else config.OPENAI_TRIAGE_MODEL
    )
    try:
        triage_client = ai_triage.TriageClient(model=triage_model)
        triage_result = triage_client.classify(message, open_count)
        triage_outcome = TriageOutcome(
            decision=triage_result.decision,
            raw_text=triage_result.raw_text,
            usage=triage_result.usage or {},
            latency_ms=triage_result.latency_ms,
        )
        if triage_result.decision == "keep":
            interpreter_model = interpreter_model_override or (
                config.ANTHROPIC_MODEL if config.AI_PROVIDER == "anthropic" else config.OPENAI_MODEL
            )
            interpreter_client = ai.AIClient(model=interpreter_model)
            t0 = time.monotonic()
            ai_result = interpreter_client.call(
                recent_chat="",
                open_positions_block=open_positions_block,
                new_message=message,
            )
            latency_ms = int((time.monotonic() - t0) * 1000)
            actions_json = [a.model_dump() for a in ai_result.response.actions]
            interpret_outcome = InterpretOutcome(
                actions=actions_json,
                reasoning=ai_result.response.reasoning or "",
                raw_text=getattr(ai_result, "raw_text", "") or "",
                usage=ai_result.usage or {},
                latency_ms=latency_ms,
            )
    except Exception as e:
        error = f"{type(e).__name__}: {e}"

    interpreter_model_resolved = interpreter_model_override or (
        config.ANTHROPIC_MODEL if config.AI_PROVIDER == "anthropic" else config.OPENAI_MODEL
    )
    elapsed_total_ms = int((time.monotonic() - overall_start) * 1000)
    return PlaygroundResult(
        context=PlaygroundContext(
            profile=active_profile,
            provider=config.AI_PROVIDER,
            interpreter_model=interpreter_model_resolved,
            triage_model=triage_model,
            open_count=open_count,
            open_positions_block_preview=open_positions_block,
            elapsed_total_ms=elapsed_total_ms,
        ),
        triage=triage_outcome,
        interpret=interpret_outcome,
        error=error,
    )


def format_actions(actions: list[dict]) -> str:
    if not actions:
        return "(no actions emitted)"
    return json.dumps(actions, indent=2, ensure_ascii=False)
