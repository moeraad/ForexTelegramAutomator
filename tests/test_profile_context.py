"""Tests for ProfileContext + AIClient/TriageClient/process_message wiring.

See ``docs/plans/2026-05-23-multi-channel-routing.md`` Step 3. Verifies:

  - ProfileContext loads profile JSON and pre-renders both prompts
  - Cache returns the same object on repeated lookups
  - AIClient.call honors per-call and per-instance system_prompt overrides
  - TriageClient.classify honors per-call system_prompt overrides
  - render_open_positions accepts a symbol arg
  - process_message routes profile.symbol / profile.system_prompt /
    profile.triage_prompt to the right downstream calls
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src import config_v2  # noqa: F401  (ensure src package loaded)
from src.profile_context import (
    ProfileContext,
    clear_cache,
    get_profile_context,
    load_profile_context,
)


# A minimal but complete profile JSON the prompt template can substitute.
_VALID_PROFILE = {
    "header": "TEST HEADER",
    "vocabulary_table": "VOCAB",
    "compound_messages": "COMPOUND",
    "commentary_filter": "FILTER",
    "directional_command_flow": "FLOW",
    "worked_examples": "EXAMPLES",
    "shorthand_decode_example": "SHORTHAND",
    "promo_indicators": "BUY NOW",
    "noise_patterns": "lol",
    "triage_keep_triggers": "buy | sell",
    "symbol": "EURUSD",
}


@pytest.fixture
def profile_path(tmp_path: Path) -> Path:
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(_VALID_PROFILE), encoding="utf-8")
    return p


@pytest.fixture(autouse=True)
def _clear_profile_cache():
    clear_cache()
    yield
    clear_cache()


# ---- ProfileContext --------------------------------------------------------


def test_load_profile_context_renders_both_prompts(profile_path: Path) -> None:
    ctx = load_profile_context(profile_path, name="test_chan")
    assert ctx.name == "test_chan"
    assert ctx.path == profile_path
    assert ctx.symbol == "EURUSD"
    # Symbol templated into both prompts.
    assert "EURUSD" in ctx.system_prompt
    assert "EURUSD" in ctx.triage_prompt
    # Channel-specific bits flow through.
    assert "VOCAB" in ctx.system_prompt
    assert "buy | sell" in ctx.triage_prompt


def test_load_profile_context_defaults_symbol_to_xauusd(tmp_path: Path) -> None:
    profile = dict(_VALID_PROFILE)
    profile.pop("symbol")
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(profile), encoding="utf-8")
    ctx = load_profile_context(p)
    assert ctx.symbol == "XAUUSD"
    assert "XAUUSD" in ctx.system_prompt


def test_load_profile_context_name_defaults_to_path_stem(profile_path: Path) -> None:
    ctx = load_profile_context(profile_path)
    assert ctx.name == "profile"


def test_get_profile_context_caches_by_path(profile_path: Path) -> None:
    a = get_profile_context(profile_path)
    b = get_profile_context(profile_path)
    assert a is b


def test_clear_cache_forces_reload(profile_path: Path) -> None:
    a = get_profile_context(profile_path)
    clear_cache()
    b = get_profile_context(profile_path)
    assert a is not b
    # But contents are equal.
    assert a.system_prompt == b.system_prompt


# ---- mtime-based invalidation (Day-2 cleanup) -----------------------------


def test_get_profile_context_reloads_when_mtime_advances(profile_path: Path) -> None:
    """Editing the file then re-reading should pick up the new contents."""
    import os
    a = get_profile_context(profile_path)
    assert "TEST HEADER" in a.system_prompt

    # Rewrite with a different header + bump mtime to a future second.
    new_profile = dict(_VALID_PROFILE)
    new_profile["header"] = "REWRITTEN HEADER"
    profile_path.write_text(json.dumps(new_profile), encoding="utf-8")
    future = a.path.stat().st_mtime + 5.0
    os.utime(profile_path, (future, future))

    b = get_profile_context(profile_path)
    assert b is not a
    assert "REWRITTEN HEADER" in b.system_prompt
    assert "TEST HEADER" not in b.system_prompt


def test_get_profile_context_returns_same_object_when_mtime_unchanged(
    profile_path: Path,
) -> None:
    """The hot-path read MUST be cheap: no reload when nothing changed."""
    a = get_profile_context(profile_path)
    b = get_profile_context(profile_path)
    c = get_profile_context(profile_path)
    assert a is b is c


def test_refresh_if_stale_returns_same_when_unchanged(profile_path: Path) -> None:
    from src.profile_context import refresh_if_stale
    a = get_profile_context(profile_path)
    refreshed = refresh_if_stale(a)
    assert refreshed is a


def test_refresh_if_stale_returns_new_when_changed(profile_path: Path) -> None:
    import os
    from src.profile_context import refresh_if_stale
    a = get_profile_context(profile_path)

    new_profile = dict(_VALID_PROFILE)
    new_profile["vocabulary_table"] = "REVISED VOCAB"
    profile_path.write_text(json.dumps(new_profile), encoding="utf-8")
    future = a.path.stat().st_mtime + 5.0
    os.utime(profile_path, (future, future))

    refreshed = refresh_if_stale(a)
    assert refreshed is not a
    assert "REVISED VOCAB" in refreshed.system_prompt


def test_refresh_if_stale_swallows_missing_file_via_get_profile_context(
    profile_path: Path,
) -> None:
    """If the file vanishes between loads, get_profile_context falls through
    to load_profile_context which raises FileNotFoundError. refresh_if_stale
    propagates that so the caller (api_helpers / listener) can log + use the
    cached version."""
    a = get_profile_context(profile_path)
    profile_path.unlink()
    from src.profile_context import refresh_if_stale
    with pytest.raises(FileNotFoundError):
        refresh_if_stale(a)


# ---- AIClient prompt resolution --------------------------------------------


def test_ai_client_per_instance_system_prompt(monkeypatch) -> None:
    from src import ai
    from src.ai import AIClient

    captured: dict = {}

    class _FakeProvider:
        def interpret(self, *, system_prompt, cached_prefix, volatile_suffix,
                      max_output_tokens, reasoning_level):
            captured["system_prompt"] = system_prompt
            from src.llm_provider import LLMCallResult
            return LLMCallResult(raw_text='{"category":"ignore","actions":[]}',
                             usage={}, latency_ms=1)

    client = AIClient(provider=_FakeProvider(), system_prompt="PER_INSTANCE")
    monkeypatch.setattr(ai, "SYSTEM_PROMPT", "MODULE_GLOBAL")
    client.call("recent", "open", "msg")
    assert captured["system_prompt"] == "PER_INSTANCE"


def test_ai_client_per_call_overrides_instance(monkeypatch) -> None:
    from src import ai
    from src.ai import AIClient

    captured: dict = {}

    class _FakeProvider:
        def interpret(self, *, system_prompt, **kwargs):
            captured["system_prompt"] = system_prompt
            from src.llm_provider import LLMCallResult
            return LLMCallResult(raw_text='{"category":"ignore","actions":[]}',
                             usage={}, latency_ms=1)

    client = AIClient(provider=_FakeProvider(), system_prompt="INSTANCE")
    monkeypatch.setattr(ai, "SYSTEM_PROMPT", "MODULE")
    client.call("recent", "open", "msg", system_prompt="PER_CALL")
    assert captured["system_prompt"] == "PER_CALL"


def test_ai_client_falls_back_to_module_global(monkeypatch) -> None:
    from src import ai
    from src.ai import AIClient

    captured: dict = {}

    class _FakeProvider:
        def interpret(self, *, system_prompt, **kwargs):
            captured["system_prompt"] = system_prompt
            from src.llm_provider import LLMCallResult
            return LLMCallResult(raw_text='{"category":"ignore","actions":[]}',
                             usage={}, latency_ms=1)

    client = AIClient(provider=_FakeProvider())  # no system_prompt
    monkeypatch.setattr(ai, "SYSTEM_PROMPT", "MODULE_FALLBACK")
    client.call("recent", "open", "msg")
    assert captured["system_prompt"] == "MODULE_FALLBACK"


# ---- TriageClient prompt resolution ---------------------------------------


def test_triage_client_per_call_overrides_instance(monkeypatch) -> None:
    from src import ai_triage
    from src.ai_triage import TriageClient

    captured: dict = {}

    class _FakeProvider:
        def triage(self, *, system_prompt, user_content, max_output_tokens):
            captured["system_prompt"] = system_prompt
            from src.llm_provider import LLMCallResult
            return LLMCallResult(raw_text='{"decision":"keep"}', usage={}, latency_ms=1)

    client = TriageClient(provider=_FakeProvider(), system_prompt="INSTANCE")
    monkeypatch.setattr(ai_triage, "TRIAGE_SYSTEM_PROMPT", "MODULE")
    client.classify("test", open_count=0, system_prompt="PER_CALL")
    assert captured["system_prompt"] == "PER_CALL"


# ---- render_open_positions accepts symbol ----------------------------------


def test_render_open_positions_uses_explicit_symbol():
    from src.db import connect, init_schema
    from src.state_summary import render_open_positions

    conn = connect(":memory:")
    init_schema(conn)
    # Set a fake market quote for EURUSD so the MARKET line includes it.
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)",
                 ("market_EURUSD_bid", "1.0500"))
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)",
                 ("market_EURUSD_ask", "1.0502"))
    conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)",
                 ("market_EURUSD_at", now))

    out = render_open_positions(conn, symbol="EURUSD")
    assert "EURUSD" in out
    assert "XAUUSD" not in out


# ---- process_message threads profile through ------------------------------


def test_process_message_uses_profile_prompts(monkeypatch, profile_path: Path) -> None:
    from src.db import connect, init_schema
    from src.orchestrator import process_message
    from src.profile_context import load_profile_context

    captured: dict = {}

    class _FakeAIProvider:
        def interpret(self, *, system_prompt, **kwargs):
            captured["ai_system_prompt"] = system_prompt
            from src.llm_provider import LLMCallResult
            return LLMCallResult(raw_text='{"category":"ignore","actions":[]}',
                             usage={}, latency_ms=1)

    class _FakeTriageProvider:
        def triage(self, *, system_prompt, **kwargs):
            captured["triage_system_prompt"] = system_prompt
            from src.llm_provider import LLMCallResult
            return LLMCallResult(raw_text='{"decision":"keep"}', usage={}, latency_ms=1)

    from src.ai import AIClient
    from src.ai_triage import TriageClient

    ai = AIClient(provider=_FakeAIProvider())
    triage = TriageClient(provider=_FakeTriageProvider())
    profile = load_profile_context(profile_path, name="eurusd_chan")

    conn = connect(":memory:")
    init_schema(conn)

    process_message(
        conn, ai,
        tg_message_id=1, chat_id=-42,
        sender="ch", text="hello",
        ai_log_path=Path("/tmp/ai_log.jsonl"),
        auto_execute_delay_sec=0,
        triage=triage,
        profile=profile,
    )

    # Both prompts came from the ProfileContext, not module globals.
    assert "EURUSD" in captured["ai_system_prompt"]
    assert "EURUSD" in captured["triage_system_prompt"]
    assert "VOCAB" in captured["ai_system_prompt"]
