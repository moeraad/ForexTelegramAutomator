from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from src.ai_triage import TriageClient, _parse_decision


def _fake_response(text: str):
    return SimpleNamespace(
        content=[SimpleNamespace(type="text", text=text)],
        usage=SimpleNamespace(
            input_tokens=50, output_tokens=5,
            cache_read_input_tokens=0, cache_creation_input_tokens=50,
        ),
    )


def test_parse_decision_plain_json():
    assert _parse_decision('{"decision": "ignore"}') == "ignore"
    assert _parse_decision('{"decision": "keep"}') == "keep"


def test_parse_decision_markdown_fenced():
    assert _parse_decision('```json\n{"decision": "ignore"}\n```') == "ignore"
    assert _parse_decision('```\n{"decision": "keep"}\n```') == "keep"


def test_parse_decision_regex_fallback():
    assert _parse_decision('here is my answer: "decision": "ignore"') == "ignore"


def test_parse_decision_malformed_defaults_to_keep():
    assert _parse_decision("completely unparseable garbage") == "keep"
    assert _parse_decision("") == "keep"


def test_parse_decision_unknown_value_defaults_to_keep():
    assert _parse_decision('{"decision": "maybe"}') == "keep"


def test_classify_returns_ignore():
    client = MagicMock()
    client.messages.create.return_value = _fake_response('{"decision": "ignore"}')
    tri = TriageClient(client=client)
    result = tri.classify("promo spam", open_count=0)
    assert result.decision == "ignore"
    assert result.usage["input_tokens"] == 50
    assert result.latency_ms >= 0


def test_classify_returns_keep():
    client = MagicMock()
    client.messages.create.return_value = _fake_response('{"decision": "keep"}')
    tri = TriageClient(client=client)
    result = tri.classify("BUY XAUUSD 4800", open_count=1)
    assert result.decision == "keep"


def test_classify_passes_open_count_into_user_content():
    client = MagicMock()
    client.messages.create.return_value = _fake_response('{"decision": "keep"}')
    tri = TriageClient(client=client)
    tri.classify("close it", open_count=3)
    kwargs = client.messages.create.call_args.kwargs
    user_content = kwargs["messages"][0]["content"]
    assert "OPEN_POSITIONS: 3" in user_content
    assert "close it" in user_content


def test_classify_uses_cache_control_on_system():
    client = MagicMock()
    client.messages.create.return_value = _fake_response('{"decision": "keep"}')
    tri = TriageClient(client=client)
    tri.classify("x", open_count=0)
    kwargs = client.messages.create.call_args.kwargs
    assert kwargs["system"][0]["cache_control"] == {"type": "ephemeral"}


def test_classify_retries_then_succeeds():
    client = MagicMock()
    client.messages.create.side_effect = [
        RuntimeError("transient"),
        _fake_response('{"decision": "keep"}'),
    ]
    tri = TriageClient(client=client, retry_sleep=0.0)
    result = tri.classify("x", open_count=0)
    assert result.decision == "keep"
    assert client.messages.create.call_count == 2


def test_classify_raises_after_max_retries():
    client = MagicMock()
    client.messages.create.side_effect = RuntimeError("persistent")
    tri = TriageClient(client=client, max_retries=2, retry_sleep=0.0)
    with pytest.raises(RuntimeError, match="triage call failed"):
        tri.classify("x", open_count=0)
