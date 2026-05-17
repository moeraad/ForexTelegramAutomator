"""Tests for the wizard's stateless message classifier (`ai_discovery`).

Covers schema patches added in the wizard re-engineering:
  - CANCEL_PENDING and CONTEXT are in the bucket vocabulary
  - Classification carries `action_types: tuple` (compound support) +
    backwards-compat `.action_type` shim
  - Tolerant parsing of both old (`action_type: str`) and new
    (`action_types: [...]`) response shapes
  - `pending` field parsed only when OPEN is among the buckets
"""
from __future__ import annotations

from src.ai_discovery import (
    CONFIDENCE_FLOOR,
    Classification,
    _ACTION_TYPES,
    _apply_confidence_floor,
    _classification_from_item,
    _parse_action_types,
    _parse_pending,
)


def test_cancel_pending_in_bucket_list():
    assert "CANCEL_PENDING" in _ACTION_TYPES


def test_context_in_bucket_list():
    assert "CONTEXT" in _ACTION_TYPES


def test_classification_backwards_compat_action_type():
    c = Classification(
        action_types=("CLOSE_PARTIAL", "MOVE_SL_BE"),
        phrase="half close BE",
        reasoning="compound",
        confidence=0.9,
    )
    assert c.action_type == "CLOSE_PARTIAL"
    assert c.action_types == ("CLOSE_PARTIAL", "MOVE_SL_BE")


def test_classification_empty_action_types_falls_back_to_unknown():
    c = Classification(
        action_types=(),
        phrase="",
        reasoning="",
        confidence=0.0,
    )
    assert c.action_type == "UNKNOWN"


def test_parse_action_types_new_shape():
    item = {"action_types": ["CLOSE_PARTIAL", "MOVE_SL_BE"]}
    assert _parse_action_types(item) == ("CLOSE_PARTIAL", "MOVE_SL_BE")


def test_parse_action_types_old_shape():
    item = {"action_type": "OPEN"}
    assert _parse_action_types(item) == ("OPEN",)


def test_parse_action_types_unknown_bucket_coerced():
    item = {"action_types": ["BOGUS_BUCKET"]}
    assert _parse_action_types(item) == ("UNKNOWN",)


def test_parse_action_types_dedup_preserves_order():
    item = {"action_types": ["OPEN", "open", "MOVE_SL_BE", "OPEN"]}
    assert _parse_action_types(item) == ("OPEN", "MOVE_SL_BE")


def test_parse_action_types_missing_falls_back():
    assert _parse_action_types({}) == ("UNKNOWN",)


def test_parse_pending_only_meaningful_for_open():
    assert _parse_pending({"pending": True}, ("CLOSE_FULL",)) is None
    assert _parse_pending({"pending": True}, ("OPEN",)) is True
    assert _parse_pending({"pending": False}, ("OPEN",)) is False
    assert _parse_pending({"pending": None}, ("OPEN",)) is None
    assert _parse_pending({}, ("OPEN",)) is None


def test_parse_pending_string_coercion():
    assert _parse_pending({"pending": "true"}, ("OPEN",)) is True
    assert _parse_pending({"pending": "false"}, ("OPEN",)) is False
    assert _parse_pending({"pending": "garbage"}, ("OPEN",)) is None


def test_classification_from_item_compound_with_pending():
    item = {
        "action_types": ["OPEN"],
        "pending": True,
        "phrase": "buy limit 4670",
        "reasoning": "limit pending",
        "confidence": 0.92,
    }
    c = _classification_from_item(item)
    assert c.action_types == ("OPEN",)
    assert c.pending is True
    assert c.confidence == 0.92


def test_classification_from_item_old_shape_no_pending():
    item = {
        "action_type": "CLOSE_FULL",
        "phrase": "exit now",
        "reasoning": "explicit close",
        "confidence": 0.85,
    }
    c = _classification_from_item(item)
    assert c.action_types == ("CLOSE_FULL",)
    assert c.pending is None


def test_confidence_floor_forces_unknown():
    c = Classification(
        action_types=("OPEN",),
        phrase="ambiguous",
        reasoning="model guessed",
        confidence=0.4,
    )
    out = _apply_confidence_floor(c)
    assert out.action_types == ("UNKNOWN",)
    assert out.reasoning.startswith("low_confidence(0.40):")


def test_confidence_floor_passes_high_confidence_through():
    c = Classification(
        action_types=("OPEN",),
        phrase="confident",
        reasoning="clear signal",
        confidence=0.95,
    )
    out = _apply_confidence_floor(c)
    assert out is c  # unchanged
    assert out.action_types == ("OPEN",)


def test_confidence_floor_at_threshold_passes():
    c = Classification(
        action_types=("OPEN",),
        phrase="",
        reasoning="",
        confidence=CONFIDENCE_FLOOR,
    )
    out = _apply_confidence_floor(c)
    assert out.action_types == ("OPEN",)


def test_confidence_floor_does_not_double_annotate_unknown():
    c = Classification(
        action_types=("UNKNOWN",),
        phrase="",
        reasoning="parse failed",
        confidence=0.0,
    )
    out = _apply_confidence_floor(c)
    assert out is c
    assert out.reasoning == "parse failed"


def test_classification_from_item_low_confidence_routed_to_unknown():
    item = {
        "action_types": ["OPEN"],
        "phrase": "ambiguous",
        "reasoning": "guess",
        "confidence": 0.5,
    }
    c = _classification_from_item(item)
    assert c.action_types == ("UNKNOWN",)
    assert "low_confidence" in c.reasoning
