"""Provider abstraction so the AI pipeline can run on Anthropic or OpenAI.

All SDK-specific shape (message construction, usage-dict keys, reasoning
knobs) lives here. AIClient and TriageClient just call `interpret()` or
`triage()` and work with a normalized `LLMCallResult` regardless of vendor.

Usage dict keys are Anthropic-flavored because downstream logging
(`logs/ai_calls.jsonl`) was built against that shape first:
    input_tokens, output_tokens, cache_read_tokens, cache_creation_tokens.
OpenAI's `prompt_tokens` / `completion_tokens` / `cached_tokens` are
normalized into the same four keys (cache_creation_tokens stays 0 since
OpenAI exposes no equivalent).
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from src import config


@dataclass
class LLMCallResult:
    raw_text: str
    usage: dict[str, int]
    latency_ms: int


class LLMProvider(Protocol):
    def interpret(
        self,
        *,
        system_prompt: str,
        cached_prefix: str,
        volatile_suffix: str,
        max_output_tokens: int,
        reasoning_level: str | None,
    ) -> LLMCallResult: ...

    def triage(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_output_tokens: int,
    ) -> LLMCallResult: ...


# Anthropic needs a concrete thinking budget in tokens; OpenAI accepts
# "low" | "medium" | "high". We funnel both off a single helper driven by
# the existing AI_THINKING_ENABLED + AI_THINKING_BUDGET_TOKENS knobs.
_ANTHROPIC_BUDGETS = {"low": 2000, "medium": 4000, "high": 8000}


def reasoning_level(thinking_enabled: bool, budget_tokens: int) -> str | None:
    if not thinking_enabled:
        return None
    return "high" if budget_tokens >= 6000 else "medium"


# -- Anthropic ---------------------------------------------------------------


def _extract_anthropic_text(content: Any) -> str:
    """Find the assistant's text output among mixed content blocks.

    Real SDK blocks set .type == 'text'. Test MagicMocks usually only set .text.
    """
    if not content:
        raise ValueError("empty response content")
    for block in content:
        if getattr(block, "type", None) == "text":
            return block.text
    for block in content:
        t = getattr(block, "text", None)
        if isinstance(t, str) and t:
            return t
    raise ValueError("no text block in response content")


def _anthropic_usage(resp: Any) -> dict[str, int]:
    u = resp.usage
    return {
        "input_tokens": getattr(u, "input_tokens", 0) or 0,
        "output_tokens": getattr(u, "output_tokens", 0) or 0,
        "cache_read_tokens": getattr(u, "cache_read_input_tokens", 0) or 0,
        "cache_creation_tokens": getattr(u, "cache_creation_input_tokens", 0) or 0,
    }


class AnthropicProvider:
    def __init__(self, *, client: Any = None, model: str = "") -> None:
        if client is None:
            import anthropic  # local import so openai-only deploys don't need it

            client = anthropic.Anthropic()
        self._client = client
        self._model = model or config.ANTHROPIC_MODEL

    def interpret(
        self,
        *,
        system_prompt: str,
        cached_prefix: str,
        volatile_suffix: str,
        max_output_tokens: int,
        reasoning_level: str | None,
    ) -> LLMCallResult:
        cached_block = {
            "type": "text",
            "text": cached_prefix,
            "cache_control": {"type": "ephemeral"},
        }
        volatile_block = {"type": "text", "text": volatile_suffix}
        system = [
            {
                "type": "text",
                "text": system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]
        kwargs: dict[str, Any] = {
            "model": self._model,
            "system": system,
            "messages": [
                {"role": "user", "content": [cached_block, volatile_block]}
            ],
        }
        if reasoning_level is not None:
            budget = _ANTHROPIC_BUDGETS.get(reasoning_level, 4000)
            kwargs["max_tokens"] = budget + max_output_tokens
            kwargs["thinking"] = {"type": "enabled", "budget_tokens": budget}
            # Extended thinking requires temperature=1.
            kwargs["temperature"] = 1
        else:
            kwargs["max_tokens"] = max_output_tokens
        start = time.monotonic()
        resp = self._client.messages.create(**kwargs)
        raw_text = _extract_anthropic_text(resp.content)
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMCallResult(
            raw_text=raw_text, usage=_anthropic_usage(resp), latency_ms=latency_ms
        )

    def triage(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_output_tokens: int,
    ) -> LLMCallResult:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": max_output_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system_prompt,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": user_content}],
        }
        start = time.monotonic()
        resp = self._client.messages.create(**kwargs)
        raw_text = _extract_anthropic_text(resp.content)
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMCallResult(
            raw_text=raw_text, usage=_anthropic_usage(resp), latency_ms=latency_ms
        )


# -- OpenAI ------------------------------------------------------------------


def _openai_text(resp: Any) -> str:
    try:
        msg = resp.choices[0].message
    except (AttributeError, IndexError) as e:
        raise ValueError(f"unexpected OpenAI response shape: {e}")
    content = getattr(msg, "content", None)
    if not isinstance(content, str) or not content:
        raise ValueError("empty OpenAI message content")
    return content


def _openai_usage(resp: Any) -> dict[str, int]:
    u = resp.usage
    cached = 0
    details = getattr(u, "prompt_tokens_details", None)
    if details is not None:
        cached = getattr(details, "cached_tokens", 0) or 0
    return {
        "input_tokens": getattr(u, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(u, "completion_tokens", 0) or 0,
        "cache_read_tokens": cached,
        "cache_creation_tokens": 0,
    }


class OpenAIProvider:
    def __init__(self, *, client: Any = None, model: str = "") -> None:
        if client is None:
            import openai  # local import

            client = openai.OpenAI()
        self._client = client
        self._model = model or config.OPENAI_MODEL

    def interpret(
        self,
        *,
        system_prompt: str,
        cached_prefix: str,
        volatile_suffix: str,
        max_output_tokens: int,
        reasoning_level: str | None,
    ) -> LLMCallResult:
        # OpenAI uses automatic prefix caching: identical leading tokens are
        # billed as cached without any cache_control annotation. Keep the
        # cached-prefix first so it survives the cache hit, then append the
        # volatile suffix as part of the same user turn.
        user_content = f"{cached_prefix}\n\n{volatile_suffix}"
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_completion_tokens": max_output_tokens,
        }
        if reasoning_level is not None:
            kwargs["reasoning_effort"] = reasoning_level
        start = time.monotonic()
        resp = self._client.chat.completions.create(**kwargs)
        raw_text = _openai_text(resp)
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMCallResult(
            raw_text=raw_text, usage=_openai_usage(resp), latency_ms=latency_ms
        )

    def triage(
        self,
        *,
        system_prompt: str,
        user_content: str,
        max_output_tokens: int,
    ) -> LLMCallResult:
        # gpt-5 family are reasoning models: max_completion_tokens covers
        # hidden reasoning tokens AND the visible output. With the Anthropic-
        # tuned 32-token budget and default (medium) effort, all tokens get
        # spent on reasoning and `content` comes back empty. Pin effort to
        # "minimal" (triage is a one-shot classification, no chain-of-thought
        # needed) and floor the budget so a short JSON answer always fits.
        kwargs: dict[str, Any] = {
            "model": self._model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content},
            ],
            "max_completion_tokens": max(max_output_tokens, 256),
            "reasoning_effort": "minimal",
        }
        start = time.monotonic()
        resp = self._client.chat.completions.create(**kwargs)
        raw_text = _openai_text(resp)
        latency_ms = int((time.monotonic() - start) * 1000)
        return LLMCallResult(
            raw_text=raw_text, usage=_openai_usage(resp), latency_ms=latency_ms
        )


# -- Factories ---------------------------------------------------------------


def default_interpreter_model() -> str:
    return (
        config.OPENAI_MODEL if config.AI_PROVIDER == "openai" else config.ANTHROPIC_MODEL
    )


def default_triage_model() -> str:
    return (
        config.OPENAI_TRIAGE_MODEL
        if config.AI_PROVIDER == "openai"
        else config.AI_TRIAGE_MODEL
    )


def build_interpreter_provider(model: str = "") -> LLMProvider:
    resolved = model or default_interpreter_model()
    if config.AI_PROVIDER == "openai":
        return OpenAIProvider(model=resolved)
    return AnthropicProvider(model=resolved)


def build_triage_provider(model: str = "") -> LLMProvider:
    resolved = model or default_triage_model()
    if config.AI_PROVIDER == "openai":
        return OpenAIProvider(model=resolved)
    return AnthropicProvider(model=resolved)
