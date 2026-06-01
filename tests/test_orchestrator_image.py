"""Unit tests for the image fallback helpers in orchestrator."""
import json
from unittest.mock import MagicMock


def _make_alert(text: str) -> dict:
    return {"type": "ALERT", "level": "warning", "text": text}


def test_fallback_fires_on_inconsistency_alert_with_image():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup: SL 4451 is below entry 4471")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is True


def test_fallback_fires_on_wrong_side_text():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("BUY SL is on the wrong side of entry")]
    assert _should_attempt_image_fallback(
        category="signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is True


def test_fallback_skipped_when_no_image():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=None,
        fallback_enabled=True,
    ) is False


def test_fallback_skipped_when_disabled():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=False,
    ) is False


def test_fallback_skipped_on_non_alert_actions():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [
        {"type": "OPEN", "side": "BUY"},
        _make_alert("[partial] inconsistent"),
    ]
    assert _should_attempt_image_fallback(
        category="signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is False


def test_fallback_skipped_on_non_signal_category():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] inconsistent SELL setup")]
    assert _should_attempt_image_fallback(
        category="context",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is False


def test_fallback_skipped_when_alert_text_not_inconsistency():
    from src.orchestrator import _should_attempt_image_fallback
    actions = [_make_alert("[partial] missing TP")]
    assert _should_attempt_image_fallback(
        category="partial_signal",
        actions=actions,
        image_bytes=b"fake",
        fallback_enabled=True,
    ) is False


def test_image_fallback_pass_returns_actions_on_success(tmp_path):
    """_image_fallback_pass returns (category, [Action]) when provider succeeds."""
    from src.orchestrator import _image_fallback_pass

    fake_result = MagicMock()
    fake_result.raw_text = json.dumps({
        "category": "signal",
        "actions": [{
            "type": "OPEN", "symbol": "XAUUSD", "side": "SELL",
            "entry_low": 4471.27, "entry_high": 4471.27,
            "sl": 4481.57, "tps": [4419.63],
        }],
    })
    fake_result.usage = {"input_tokens": 20, "output_tokens": 10,
                         "cache_read_tokens": 0, "cache_creation_tokens": 0}
    fake_result.latency_ms = 200

    mock_provider = MagicMock()
    mock_provider.interpret.return_value = fake_result

    ai_mock = MagicMock()
    ai_mock._provider = mock_provider

    result = _image_fallback_pass(
        ai=ai_mock,
        original_alert_text="[partial] inconsistent SELL setup",
        image_bytes=b"fake_jpeg",
        system_prompt="system",
        cached_prefix="prefix",
        volatile_suffix="suffix",
        reasoning_level=None,
        ai_log_path=tmp_path / "ai.jsonl",
    )

    assert result is not None
    category, actions = result
    assert category == "signal"
    from src.validators import OpenAction
    assert any(isinstance(a, OpenAction) for a in actions)
    call_kwargs = mock_provider.interpret.call_args.kwargs
    assert call_kwargs["image_bytes"] == b"fake_jpeg"


def test_image_fallback_pass_returns_none_on_provider_error(tmp_path):
    """_image_fallback_pass returns None when the provider raises."""
    from src.orchestrator import _image_fallback_pass

    mock_provider = MagicMock()
    mock_provider.interpret.side_effect = RuntimeError("API down")
    ai_mock = MagicMock()
    ai_mock._provider = mock_provider

    result = _image_fallback_pass(
        ai=ai_mock,
        original_alert_text="[partial] inconsistent",
        image_bytes=b"bytes",
        system_prompt="sys",
        cached_prefix="pfx",
        volatile_suffix="sfx",
        reasoning_level=None,
        ai_log_path=tmp_path / "ai.jsonl",
    )
    assert result is None
