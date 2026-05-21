"""Tests for the field-aware search parser used by the Actions table."""
from __future__ import annotations

from src.gui.panels._search_parser import (
    NumericPredicate,
    ParsedQuery,
    apply,
    parse,
)


def test_parse_empty_returns_empty_query():
    q = parse("")
    assert q.is_empty
    q2 = parse("   ")
    assert q2.is_empty


def test_parse_bare_substring():
    q = parse("rejected")
    assert q.substrings == ["rejected"]
    assert not q.str_filters
    assert not q.numeric_filters


def test_parse_multiple_substrings_and():
    q = parse("rejected duplicate")
    assert q.substrings == ["rejected", "duplicate"]


def test_parse_field_filter():
    q = parse("type:OPEN")
    assert q.str_filters == {"type": ["OPEN"]}
    assert not q.substrings


def test_parse_multiple_field_filters():
    q = parse("type:OPEN side:BUY")
    assert q.str_filters == {"type": ["OPEN"], "side": ["BUY"]}


def test_parse_numeric_gt():
    q = parse("score>70")
    assert q.numeric_filters == [NumericPredicate("score", ">", 70.0)]


def test_parse_numeric_gte():
    """The longest-first operator search must pick >= before >."""
    q = parse("score>=70")
    assert q.numeric_filters == [NumericPredicate("score", ">=", 70.0)]


def test_parse_numeric_neq():
    q = parse("mult!=1.0")
    assert q.numeric_filters == [NumericPredicate("mult", "!=", 1.0)]


def test_parse_numeric_colon_treated_as_equals():
    q = parse("score:70")
    assert q.numeric_filters == [NumericPredicate("score", "=", 70.0)]


def test_parse_negative_pnl():
    q = parse("pnl<-10")
    assert q.numeric_filters == [NumericPredicate("pnl", "<", -10.0)]


def test_parse_quoted_substring_preserves_spaces():
    """A double-quoted substring captures the whole phrase including
    spaces, separately from any field-style tokens elsewhere in the
    query."""
    q = parse('type:OPEN "sl too wide"')
    assert q.str_filters == {"type": ["OPEN"]}
    assert "sl too wide" in q.substrings


def test_parse_mixed_field_and_substring():
    q = parse("type:OPEN duplicate")
    assert q.str_filters == {"type": ["OPEN"]}
    assert q.substrings == ["duplicate"]


def test_parse_invalid_number_falls_back_to_substring():
    q = parse("score>seventy")
    assert q.numeric_filters == []
    assert "score>seventy" in q.substrings


def test_parse_unknown_field_falls_back_to_substring():
    q = parse("foo:bar")
    assert q.numeric_filters == []
    assert q.str_filters == {}
    assert "foo:bar" in q.substrings


def test_parse_empty_value_after_colon_falls_back():
    q = parse("type:")
    # No value -> bare substring.
    assert "type:" in q.substrings


def test_apply_empty_query_returns_true():
    assert apply(parse(""), {"type": "OPEN", "status": "executed"}) is True


def test_apply_field_filter_matches_exact():
    q = parse("type:OPEN")
    assert apply(q, {"type": "OPEN"}) is True
    assert apply(q, {"type": "CLOSE_PARTIAL"}) is False


def test_apply_field_filter_is_case_insensitive():
    q = parse("status:executed")
    assert apply(q, {"status": "EXECUTED"}) is True


def test_apply_field_filter_substring_match():
    """`reason:tp` matches both 'tp1' and 'tp2'."""
    q = parse("reason:tp")
    assert apply(q, {"reason": "tp1"}) is True
    assert apply(q, {"reason": "tp2"}) is True
    assert apply(q, {"reason": "sl"}) is False


def test_apply_numeric_gt_matches():
    q = parse("score>=70")
    assert apply(q, {"score": 75}) is True
    assert apply(q, {"score": 70}) is True
    assert apply(q, {"score": 69}) is False


def test_apply_numeric_with_missing_field_fails():
    """Filter on a numeric field that's None must not match."""
    q = parse("score>50")
    assert apply(q, {"score": None}) is False
    assert apply(q, {}) is False


def test_apply_substring_matches_against_all_text_fields():
    q = parse("duplicate")
    row = {"type": "OPEN", "status": "rejected", "reason": "duplicate_signal"}
    assert apply(q, row) is True


def test_apply_substring_not_found():
    q = parse("missing_token")
    row = {"type": "OPEN", "status": "executed"}
    assert apply(q, row) is False


def test_apply_combined_predicates_must_all_match():
    q = parse("type:OPEN side:BUY score>=60")
    matching = {"type": "OPEN", "side": "BUY", "score": 72}
    assert apply(q, matching) is True
    failing_score = {"type": "OPEN", "side": "BUY", "score": 55}
    assert apply(q, failing_score) is False
    failing_side = {"type": "OPEN", "side": "SELL", "score": 72}
    assert apply(q, failing_side) is False


def test_apply_str_field_with_multiple_values_or_within_field():
    """Two `type:` tokens with different values are OR within the
    field (a "type matching A or B"), still ANDed across other fields.

    Currently our parser pushes both into the same list and apply()
    uses `any(...)` over them — which IS the OR semantic. Lock this
    in with a test."""
    q = ParsedQuery(str_filters={"type": ["OPEN", "OPEN_INSTANT"]})
    assert apply(q, {"type": "OPEN"}) is True
    assert apply(q, {"type": "OPEN_INSTANT"}) is True
    assert apply(q, {"type": "CLOSE_PARTIAL"}) is False
