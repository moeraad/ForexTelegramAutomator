from types import SimpleNamespace
from unittest.mock import MagicMock

from src.llm_provider import (
    AnthropicProvider,
    OpenAIProvider,
    reasoning_level,
)


# -- reasoning_level mapping -------------------------------------------------

def test_reasoning_level_disabled_returns_none():
    assert reasoning_level(False, 4000) is None
    assert reasoning_level(False, 10000) is None


def test_reasoning_level_enabled_medium_below_6000():
    assert reasoning_level(True, 0) == "medium"
    assert reasoning_level(True, 4000) == "medium"
    assert reasoning_level(True, 5999) == "medium"


def test_reasoning_level_enabled_high_at_or_above_6000():
    assert reasoning_level(True, 6000) == "high"
    assert reasoning_level(True, 32000) == "high"


# -- AnthropicProvider -------------------------------------------------------

def _anthropic_resp(text: str = "{}", cr: int = 0, cc: int = 0,
                    inp: int = 100, out: int = 20):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=inp, output_tokens=out,
            cache_read_input_tokens=cr, cache_creation_input_tokens=cc,
        ),
    )


def test_anthropic_interpret_with_medium_reasoning_sets_thinking():
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp(text='{"x":1}', cr=50)
    p = AnthropicProvider(client=client, model="claude-sonnet-4-6")

    result = p.interpret(
        system_prompt="SYS",
        cached_prefix="chat-prefix",
        volatile_suffix="volatile-msg",
        max_output_tokens=1024,
        reasoning_level="medium",
    )

    kw = client.messages.create.call_args.kwargs
    assert kw["model"] == "claude-sonnet-4-6"
    assert kw["thinking"] == {"type": "enabled", "budget_tokens": 4000}
    assert kw["temperature"] == 1
    assert kw["max_tokens"] == 4000 + 1024
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["system"][0]["text"] == "SYS"
    blocks = kw["messages"][0]["content"]
    assert len(blocks) == 2
    assert blocks[0]["cache_control"] == {"type": "ephemeral"}
    assert blocks[0]["text"] == "chat-prefix"
    assert "cache_control" not in blocks[1]
    assert blocks[1]["text"] == "volatile-msg"
    assert result.raw_text == '{"x":1}'
    assert result.usage["cache_read_tokens"] == 50
    assert result.usage["input_tokens"] == 100
    assert result.usage["cache_creation_tokens"] == 0


def test_anthropic_interpret_without_reasoning_omits_thinking_block():
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp()
    p = AnthropicProvider(client=client, model="m")

    p.interpret(system_prompt="s", cached_prefix="c", volatile_suffix="v",
                max_output_tokens=1024, reasoning_level=None)

    kw = client.messages.create.call_args.kwargs
    assert "thinking" not in kw
    assert "temperature" not in kw
    assert kw["max_tokens"] == 1024


def test_anthropic_interpret_high_reasoning_bumps_budget_to_8000():
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp()
    p = AnthropicProvider(client=client, model="m")

    p.interpret(system_prompt="s", cached_prefix="c", volatile_suffix="v",
                max_output_tokens=512, reasoning_level="high")

    kw = client.messages.create.call_args.kwargs
    assert kw["thinking"]["budget_tokens"] == 8000
    assert kw["max_tokens"] == 8000 + 512


def test_anthropic_triage_cache_control_and_string_user_content():
    client = MagicMock()
    client.messages.create.return_value = _anthropic_resp(
        text='{"decision":"keep"}'
    )
    p = AnthropicProvider(client=client, model="haiku")

    result = p.triage(system_prompt="SYS", user_content="hello",
                      max_output_tokens=32)

    kw = client.messages.create.call_args.kwargs
    assert kw["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert kw["system"][0]["text"] == "SYS"
    assert kw["messages"][0] == {"role": "user", "content": "hello"}
    assert kw["max_tokens"] == 32
    assert result.raw_text == '{"decision":"keep"}'
    assert result.latency_ms >= 0


def test_anthropic_usage_tolerates_missing_cache_fields():
    client = MagicMock()
    client.messages.create.return_value = SimpleNamespace(
        content=[SimpleNamespace(type="text", text="ok")],
        usage=SimpleNamespace(input_tokens=7, output_tokens=2),
    )
    p = AnthropicProvider(client=client, model="m")
    result = p.interpret(system_prompt="s", cached_prefix="c",
                         volatile_suffix="v", max_output_tokens=10,
                         reasoning_level=None)
    assert result.usage == {
        "input_tokens": 7,
        "output_tokens": 2,
        "cache_read_tokens": 0,
        "cache_creation_tokens": 0,
    }


# -- OpenAIProvider ----------------------------------------------------------

def _openai_resp(text: str = "{}", prompt: int = 100, completion: int = 20,
                 cached: int = 0):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(
            prompt_tokens=prompt,
            completion_tokens=completion,
            prompt_tokens_details=SimpleNamespace(cached_tokens=cached),
        ),
    )


def test_openai_interpret_passes_reasoning_effort_when_set():
    client = MagicMock()
    client.chat.completions.create.return_value = _openai_resp(
        text='{"x":1}', cached=77
    )
    p = OpenAIProvider(client=client, model="gpt-5")

    result = p.interpret(
        system_prompt="SYS",
        cached_prefix="chat",
        volatile_suffix="msg",
        max_output_tokens=1024,
        reasoning_level="high",
    )

    kw = client.chat.completions.create.call_args.kwargs
    assert kw["model"] == "gpt-5"
    assert kw["reasoning_effort"] == "high"
    # gpt-5 reasoning models charge max_completion_tokens for BOTH hidden
    # reasoning AND visible output. With effort=high the provider reserves
    # an extra 8000-token reasoning budget on top of max_output_tokens so
    # the visible JSON isn't starved (root cause of historical empty-content
    # / finish_reason="length" failures in the evaluator).
    assert kw["max_completion_tokens"] == 1024 + 8000
    assert kw["messages"][0] == {"role": "system", "content": "SYS"}
    assert kw["messages"][1]["role"] == "user"
    user_content = kw["messages"][1]["content"]
    assert "chat" in user_content
    assert "msg" in user_content
    # Cached prefix must precede volatile suffix so prefix caching stays valid.
    assert user_content.index("chat") < user_content.index("msg")
    assert result.raw_text == '{"x":1}'
    assert result.usage == {
        "input_tokens": 100,
        "output_tokens": 20,
        "cache_read_tokens": 77,
        "cache_creation_tokens": 0,
    }


def test_openai_interpret_omits_reasoning_when_none():
    client = MagicMock()
    client.chat.completions.create.return_value = _openai_resp()
    p = OpenAIProvider(client=client, model="gpt-5-mini")

    p.interpret(system_prompt="s", cached_prefix="c", volatile_suffix="v",
                max_output_tokens=1024, reasoning_level=None)

    kw = client.chat.completions.create.call_args.kwargs
    assert "reasoning_effort" not in kw


def test_openai_triage_shape():
    client = MagicMock()
    client.chat.completions.create.return_value = _openai_resp(
        text='{"decision":"ignore"}'
    )
    p = OpenAIProvider(client=client, model="gpt-5-nano")

    result = p.triage(system_prompt="SYS", user_content="hello",
                      max_output_tokens=32)

    kw = client.chat.completions.create.call_args.kwargs
    assert kw["messages"] == [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "hello"},
    ]
    # gpt-5 reasoning models need headroom above the raw 32-token budget and
    # an explicit minimal effort, otherwise the visible output comes back empty.
    assert kw["max_completion_tokens"] == 256
    assert kw["reasoning_effort"] == "minimal"
    assert result.raw_text == '{"decision":"ignore"}'
    assert result.usage["input_tokens"] == 100


def test_openai_usage_handles_missing_prompt_details():
    client = MagicMock()
    client.chat.completions.create.return_value = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='ok'))],
        usage=SimpleNamespace(
            prompt_tokens=10, completion_tokens=5,
            prompt_tokens_details=None,
        ),
    )
    p = OpenAIProvider(client=client, model="gpt-5")

    result = p.interpret(system_prompt="s", cached_prefix="c",
                         volatile_suffix="v", max_output_tokens=1024,
                         reasoning_level=None)
    assert result.usage["cache_read_tokens"] == 0
    assert result.usage["input_tokens"] == 10
