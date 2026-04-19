from unittest.mock import MagicMock, patch
from src.ai import build_messages, call_ai, AIClient


def test_build_messages_structure():
    msgs = build_messages(
        recent_chat="[14:30] Yusuf: gold pumping",
        open_positions_block="OPEN POSITIONS:\n  (none)",
        new_message="[14:35] Yusuf: BUY GOLD 4866-4864 SL 4855 TP 4880",
    )
    assert msgs[0]["role"] == "user"
    blocks = msgs[0]["content"]
    assert isinstance(blocks, list)
    assert any("recent_chat".lower() in str(b).lower() or "14:30" in str(b) for b in blocks)
    cached = [b for b in blocks if b.get("cache_control")]
    assert len(cached) >= 1


def test_call_ai_returns_parsed_response():
    fake_client = MagicMock()
    fake_resp = MagicMock()
    fake_resp.content = [MagicMock(text='{"actions":[{"type":"ALERT","level":"info","text":"ok"}],"reasoning":"x"}')]
    fake_resp.usage.input_tokens = 100
    fake_resp.usage.output_tokens = 20
    fake_resp.usage.cache_read_input_tokens = 80
    fake_resp.usage.cache_creation_input_tokens = 0
    fake_client.messages.create.return_value = fake_resp

    client = AIClient(client=fake_client, model="claude-sonnet-4-6")
    result = client.call(
        recent_chat="...",
        open_positions_block="OPEN POSITIONS:\n  (none)",
        new_message="hi",
    )
    assert result.response.actions[0].type == "ALERT"
    assert result.usage["cache_read_tokens"] == 80


def test_call_ai_retries_on_transient_error():
    fake_client = MagicMock()
    fake_client.messages.create.side_effect = [
        Exception("boom"),
        Exception("boom2"),
        MagicMock(content=[MagicMock(text='{"actions":[],"reasoning":""}')],
                  usage=MagicMock(input_tokens=1, output_tokens=1,
                                  cache_read_input_tokens=0,
                                  cache_creation_input_tokens=0)),
    ]
    client = AIClient(client=fake_client, model="claude-sonnet-4-6", max_retries=3, retry_sleep=0.0)
    result = client.call("", "", "hi")
    assert result.response.actions == []
    assert fake_client.messages.create.call_count == 3
