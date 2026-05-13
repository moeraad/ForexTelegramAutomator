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


# ---- Phase 5: channel-profile rendering -------------------------------

def test_default_profile_renders_fxengineer_gold():
    """Default CHANNEL_PROFILE='fxengineer-gold' interpolates the Arabic
    vocabulary, examples, and price-range hint into SYSTEM_PROMPT."""
    from src.ai import SYSTEM_PROMPT
    assert "Arabic-language gold (XAUUSD)" in SYSTEM_PROMPT
    assert "أمن دخولك" in SYSTEM_PROMPT
    assert "اشتري الذهب" in SYSTEM_PROMPT
    assert "Gold trades roughly 4000-5500" in SYSTEM_PROMPT
    # Universal blocks still present (NOT from profile):
    assert "RULE A — SIDE FLIP" in SYSTEM_PROMPT
    assert "CANCEL_PENDING" in SYSTEM_PROMPT
    assert '"pending":<bool, default false>' in SYSTEM_PROMPT


def test_smc_profile_renders_english_vocabulary():
    """Switching CHANNEL_PROFILE to 'SMC' renders a different prompt
    with English vocabulary and SMC-specific worked examples."""
    from src.ai import _render_system_prompt
    smc = _render_system_prompt("SMC")
    assert "SMC_XAUUSD" in smc
    assert "buy limit" in smc.lower()
    assert "Delete Limit" in smc
    assert "Half close use BE" in smc
    # Arabic content is absent (no leakage from default):
    assert "أمن دخولك" not in smc
    # Universal blocks still present:
    assert "RULE A — SIDE FLIP" in smc
    assert "CANCEL_PENDING" in smc


def test_unknown_profile_raises_clear_error():
    """Mistyping the CHANNEL_PROFILE name should produce a clear
    FileNotFoundError pointing at the missing channels/<name>.json."""
    import pytest
    from src.ai import _render_system_prompt
    with pytest.raises(FileNotFoundError, match="does-not-exist"):
        _render_system_prompt("does-not-exist")


def test_triage_prompt_loads_channel_triggers():
    """The triage prompt's high-signal trigger list also comes from the
    active channel profile."""
    from src.ai_triage import TRIAGE_SYSTEM_PROMPT, _render_triage_prompt
    assert "أمن دخولك" in TRIAGE_SYSTEM_PROMPT
    smc_triage = _render_triage_prompt("SMC")
    assert "XAUUSD buy limit" in smc_triage
    assert "Delete Limit" in smc_triage
    assert "أمن دخولك" not in smc_triage
