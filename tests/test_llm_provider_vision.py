"""Tests that llm_provider.interpret() builds correct content blocks for vision."""
import base64
from unittest.mock import MagicMock


def _fake_bytes() -> bytes:
    return b"\xff\xd8\xff" + b"\x00" * 100  # minimal JPEG header


def test_anthropic_interpret_includes_image_block_when_image_bytes_given():
    from src.llm_provider import AnthropicProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.usage.input_tokens = 10
        resp.usage.output_tokens = 5
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        block = MagicMock(); block.type = "text"; block.text = '{"category":"signal"}'
        resp.content = [block]
        return resp
    mock_client.messages.create.side_effect = fake_create
    provider = AnthropicProvider(client=mock_client, model="claude-sonnet-4-6")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=_fake_bytes(),
    )
    content = captured["kwargs"]["messages"][0]["content"]
    types = [b["type"] for b in content]
    assert "image" in types
    assert types[0] == "image", "image block must come before text blocks"
    img_block = next(b for b in content if b["type"] == "image")
    assert img_block["source"]["type"] == "base64"
    assert img_block["source"]["media_type"] == "image/jpeg"
    assert img_block["source"]["data"] == base64.b64encode(_fake_bytes()).decode()


def test_anthropic_interpret_no_image_block_when_image_bytes_none():
    from src.llm_provider import AnthropicProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        resp = MagicMock()
        resp.usage.input_tokens = 10
        resp.usage.output_tokens = 5
        resp.usage.cache_read_input_tokens = 0
        resp.usage.cache_creation_input_tokens = 0
        block = MagicMock(); block.type = "text"; block.text = '{"category":"signal"}'
        resp.content = [block]
        return resp
    mock_client.messages.create.side_effect = fake_create
    provider = AnthropicProvider(client=mock_client, model="claude-sonnet-4-6")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=None,
    )
    content = captured["kwargs"]["messages"][0]["content"]
    types = [b["type"] for b in content]
    assert "image" not in types


def test_openai_interpret_includes_image_url_block_when_image_bytes_given():
    from src.llm_provider import OpenAIProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        choice = MagicMock()
        choice.message.content = '{"category":"signal"}'
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        resp.usage.prompt_tokens_details = MagicMock(cached_tokens=0)
        return resp
    mock_client.chat.completions.create.side_effect = fake_create
    provider = OpenAIProvider(client=mock_client, model="gpt-5")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=_fake_bytes(),
    )
    messages = captured["kwargs"]["messages"]
    user_msg = next(m for m in messages if m["role"] == "user")
    content = user_msg["content"]
    assert isinstance(content, list)
    types = [b["type"] for b in content]
    assert "image_url" in types
    assert types[0] == "image_url", "image_url block must come before text block"
    img_block = next(b for b in content if b["type"] == "image_url")
    expected_url = f"data:image/jpeg;base64,{base64.b64encode(_fake_bytes()).decode()}"
    assert img_block["image_url"]["url"] == expected_url


def test_openai_interpret_no_image_block_when_image_bytes_none():
    from src.llm_provider import OpenAIProvider
    captured = {}
    mock_client = MagicMock()
    def fake_create(**kwargs):
        captured["kwargs"] = kwargs
        choice = MagicMock()
        choice.message.content = '{"category":"signal"}'
        choice.finish_reason = "stop"
        resp = MagicMock()
        resp.choices = [choice]
        resp.usage.prompt_tokens = 10
        resp.usage.completion_tokens = 5
        resp.usage.prompt_tokens_details = MagicMock(cached_tokens=0)
        return resp
    mock_client.chat.completions.create.side_effect = fake_create
    provider = OpenAIProvider(client=mock_client, model="gpt-5")
    provider.interpret(
        system_prompt="sys",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        max_output_tokens=256,
        reasoning_level=None,
        image_bytes=None,
    )
    user_msg = next(m for m in captured["kwargs"]["messages"] if m["role"] == "user")
    # Non-image path: content must be a plain string, not a list
    assert isinstance(user_msg["content"], str)
