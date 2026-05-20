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


# ---- Channel-profile rendering -----------------------------------------
#
# These tests build a synthetic profile in tmp_path and feed it to the
# rendering functions through a monkeypatched `_resolve_profile_path` /
# `_load_profile`. That decouples them from whatever profile happens to
# be installed on the dev machine — earlier hardcoded references to a
# "fxengineer-gold" / "SMC" profile broke whenever the operator's APPDATA
# didn't carry those exact files, and the assertions referenced legacy
# fields (price_range_hint, RULE A) that have since been removed from
# the prompt template.


_SYNTH_SENTINELS = {
    "name": "synthtest",
    "symbol": "XAUUSD",
    "promo_indicators": "SYNTH_PROMO_INDICATOR_TOKEN",
    "header": "SYNTH_HEADER_TOKEN",
    "vocabulary_table": "SYNTH_VOCAB_TOKEN",
    "compound_messages": "SYNTH_COMPOUND_TOKEN",
    "commentary_filter": "SYNTH_COMMENTARY_TOKEN",
    "directional_command_flow": "SYNTH_DIRECTION_TOKEN",
    "worked_examples": "SYNTH_EXAMPLES_TOKEN",
    "shorthand_decode_example": "SYNTH_SHORTHAND_TOKEN",
    "triage_keep_triggers": "SYNTH_TRIGGER_TOKEN",
    "noise_patterns": "SYNTH_NOISE_TOKEN",
}


def _write_synth_profile(tmp_path, **overrides) -> "pathlib.Path":
    import json
    profile = dict(_SYNTH_SENTINELS)
    profile.update(overrides)
    p = tmp_path / "profile.json"
    p.write_text(json.dumps(profile), encoding="utf-8")
    return p


def test_render_system_prompt_interpolates_all_profile_fields(tmp_path, monkeypatch):
    """Every templated slot from the profile JSON should land in the
    rendered SYSTEM prompt — header, vocabulary table, examples, promo
    indicators, etc. Universal blocks (CANCEL_PENDING definition, HARD
    INVARIANTS) come from the template body, not the profile."""
    p = _write_synth_profile(tmp_path)
    from src import ai
    monkeypatch.setattr(ai, "_resolve_profile_path", lambda name=None: p)
    rendered = ai._render_system_prompt()
    # Each sentinel proves its slot was substituted.
    for key in ("header", "vocabulary_table", "compound_messages",
                "commentary_filter", "directional_command_flow",
                "worked_examples", "shorthand_decode_example",
                "promo_indicators"):
        assert _SYNTH_SENTINELS[key] in rendered, f"slot {key!r} not interpolated"
    # Universal template body must still be present (not pulled from profile).
    assert "CANCEL_PENDING" in rendered
    assert "HARD INVARIANTS" in rendered
    # Symbol substituted from profile.
    assert "XAUUSD" in rendered


def test_render_system_prompt_isolates_channel_specific_strings(tmp_path, monkeypatch):
    """Sentinels from one synthetic profile must not leak into another
    profile's rendered output — confirms there's no module-level state
    cross-contaminating between calls."""
    p1 = _write_synth_profile(tmp_path, header="HEADER_FROM_FIRST_PROFILE",
                              vocabulary_table="VOCAB_FROM_FIRST")
    p2_path = tmp_path / "second.json"
    p2_dict = dict(_SYNTH_SENTINELS)
    p2_dict.update(header="HEADER_FROM_SECOND_PROFILE",
                   vocabulary_table="VOCAB_FROM_SECOND")
    import json
    p2_path.write_text(json.dumps(p2_dict), encoding="utf-8")

    from src import ai
    monkeypatch.setattr(ai, "_resolve_profile_path", lambda name=None: p1)
    first = ai._render_system_prompt()
    monkeypatch.setattr(ai, "_resolve_profile_path", lambda name=None: p2_path)
    second = ai._render_system_prompt()

    assert "HEADER_FROM_FIRST_PROFILE" in first
    assert "VOCAB_FROM_FIRST" in first
    assert "HEADER_FROM_SECOND_PROFILE" not in first

    assert "HEADER_FROM_SECOND_PROFILE" in second
    assert "VOCAB_FROM_SECOND" in second
    assert "HEADER_FROM_FIRST_PROFILE" not in second


def test_render_system_prompt_missing_profile_raises_clear_error(tmp_path, monkeypatch):
    """Pointing at a nonexistent profile path must raise FileNotFoundError
    with the missing path in the message (not a generic KeyError)."""
    nonexistent = tmp_path / "does-not-exist" / "profile.json"
    from src import ai
    import pytest
    monkeypatch.setattr(ai, "_resolve_profile_path", lambda name=None: nonexistent)
    with pytest.raises(FileNotFoundError, match="profile not found"):
        ai._render_system_prompt()


def test_render_triage_prompt_uses_channel_triggers(tmp_path, monkeypatch):
    """Triage prompt should interpolate the profile's
    `triage_keep_triggers`, `promo_indicators`, and `noise_patterns`
    slots — the channel-specific filters live there, not in the universal
    triage template body."""
    p = _write_synth_profile(tmp_path)
    from src import ai_triage
    import json
    profile_dict = json.loads(p.read_text(encoding="utf-8"))
    monkeypatch.setattr(ai_triage, "_load_profile", lambda name=None: profile_dict)
    rendered = ai_triage._render_triage_prompt()
    assert _SYNTH_SENTINELS["triage_keep_triggers"] in rendered
    assert _SYNTH_SENTINELS["promo_indicators"] in rendered
    assert _SYNTH_SENTINELS["noise_patterns"] in rendered
    # Universal triage rules still present.
    assert "Direction words" in rendered or "UNIVERSAL" in rendered.upper()
