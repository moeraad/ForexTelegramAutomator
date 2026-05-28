"""Deterministic IGNORE path in trigger_matcher.

An IGNORE-typed trigger whose normalised sample EQUALS the whole
normalised incoming message suppresses it before triage + interpreter.
Equality (never substring) is the safety contract — a real signal that
quotes a curated noise phrase as a fragment must NOT be dropped.

The matcher is channel- and language-agnostic: it compares whatever
IGNORE samples the active profile carries via the shared `normalize()`.
These tests use generic, synthetic samples (no channel-specific
vocabulary) and an explicit multi-language case to prove that.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db import connect, init_schema
from src import trigger_matcher


@pytest.fixture(autouse=True)
def _isolate_module_state():
    trigger_matcher._cache = None
    yield
    trigger_matcher._cache = None


def _write_profile(tmp_path: Path, *, triggers: list[dict],
                   symbol: str = "XAUUSD") -> Path:
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({"symbol": symbol, "triggers": triggers}),
                 encoding="utf-8")
    return p


def _setup(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(str(tmp_path / "db.sqlite"))
    init_schema(conn)
    return conn


def _ignore(sample: str, phrase: str = "noise") -> dict:
    return {"phrase": phrase, "action_type": "IGNORE",
            "samples": [sample], "context_tokens": []}


def test_exact_equality_suppresses(tmp_path, monkeypatch):
    """Whole-message normalised equality with an IGNORE sample → []."""
    profile = _write_profile(tmp_path, triggers=[
        _ignore("thanks for the trust"),
    ])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    meta: dict = {}
    # Same text, different decoration — normalisation collapses both.
    result = trigger_matcher.match("Thanks for the trust!!! 🙏", conn,
                                   meta_out=meta)
    assert result == []
    assert meta["layer"] == "deterministic_ignore"


@pytest.mark.parametrize("sample,message", [
    ("good morning everyone", "Good morning everyone 👋"),   # Latin
    ("شكرا على الثقة", "شكرا على الثقة ❤️"),                  # Arabic
    ("早上好各位", "早上好各位！"),                              # CJK
    ("доброе утро всем", "Доброе утро всем!"),               # Cyrillic
])
def test_language_agnostic_equality(tmp_path, monkeypatch, sample, message):
    """The path works identically in any script — nothing is hardcoded to
    a channel's language."""
    profile = _write_profile(tmp_path, triggers=[_ignore(sample)])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    assert trigger_matcher.match(message, conn) == []


def test_fragment_of_longer_message_is_NOT_suppressed(tmp_path, monkeypatch):
    """The crux: an IGNORE sample that is a *substring* of a longer real
    message must NOT drop it — substring matching would suppress trades."""
    profile = _write_profile(tmp_path, triggers=[
        _ignore("stay tuned"),  # common noise phrase
    ])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    # Real-ish message that merely contains the noise phrase as a fragment.
    result = trigger_matcher.match(
        "looking for a strong setup, stay tuned", conn,
    )
    assert result is None  # falls through to AI, not suppressed


def test_emittable_trigger_wins_over_ignore(tmp_path, monkeypatch):
    """A message that both contains an emittable trigger and equals an
    IGNORE sample emits the action — emittable Layer 1 runs first."""
    msg = "secure your entry and book half"
    profile = _write_profile(tmp_path, triggers=[
        # IGNORE sample equal to the whole message...
        _ignore(msg),
        # ...but an emittable CLOSE_PARTIAL trigger also matches it.
        {"phrase": "book half", "action_type": "CLOSE_PARTIAL",
         "samples": ["book half"], "context_tokens": []},
    ])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    # CLOSE_PARTIAL precondition: an open position must exist.
    conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status, original_volume) "
        "VALUES(1, 99, 'XAUUSD', 'BUY', 0.1, 4500, 4490, 4520, 'open', 0.1)"
    )
    result = trigger_matcher.match(msg, conn)
    assert result is not None
    assert result[0].type == "CLOSE_PARTIAL"


def test_pure_number_sample_does_not_suppress(tmp_path, monkeypatch):
    """A bare-number IGNORE sample normalises to 'n' and is screened out —
    it must never equality-match unrelated bare-number messages (a lone
    number can be a price / SL shorthand)."""
    profile = _write_profile(tmp_path, triggers=[_ignore("4696")])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    # Another bare number normalises to "n" too, but the sample is ineligible.
    result = trigger_matcher.match("5021", conn)
    assert result is None


def test_no_ignore_match_falls_through_to_none(tmp_path, monkeypatch):
    profile = _write_profile(tmp_path, triggers=[_ignore("good luck")])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    result = trigger_matcher.match("buy gold and trust the plan", conn)
    assert result is None


def test_eligibility_helper_is_language_agnostic():
    elig = trigger_matcher._ignore_sample_eligible
    # Pure digit placeholder(s) → not eligible (numbers may be meaningful).
    assert not elig("n")
    assert not elig("n n")
    # A real token in any script → eligible, even short or with "n" letters.
    assert elig("in")                  # English word containing 'n'
    assert elig("ok")
    assert elig("好")                   # single CJK glyph (a full word)
    assert elig("gold n")              # word + number token


def test_normalize_digit_placeholder_contract():
    """Pins the cross-module contract `_ignore_sample_eligible` relies on:
    `normalize` must collapse a digit run to the single token 'n'. If the
    placeholder ever changes in text_normalize.py, this fails loudly here
    instead of silently letting bare-number samples become eligible (which
    could suppress a price/SL-shorthand message)."""
    from src.text_normalize import normalize
    assert normalize("4696") == "n"
    assert normalize("4696 4700") == "n n"
    assert not trigger_matcher._ignore_sample_eligible(normalize("4696"))


def test_context_tokens_narrow_an_ignore_rule(tmp_path, monkeypatch):
    """An IGNORE rule with a context token only suppresses when that token
    is also present — parity with the emittable deterministic path."""
    profile = _write_profile(tmp_path, triggers=[{
        "phrase": "noise", "action_type": "IGNORE",
        "samples": ["closed in profit"], "context_tokens": ["congrats"],
    }])
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    # Equal to the sample but the context token isn't present → not suppressed.
    assert trigger_matcher.match("closed in profit", conn) is None


def test_test_pane_reports_suppression_not_emittable(tmp_path):
    """Regression for the operator-facing Test pane: an IGNORE sample must
    surface as `suppressed_by`, NOT a misleading 'not emittable' diagnostic."""
    triggers = [{"phrase": "thanks", "action_type": "IGNORE",
                 "samples": ["thanks for the trust"], "context_tokens": []}]
    ctx = trigger_matcher.MatchContext(
        has_open_position=False, open_position_side=None,
        has_closed_within_24h=False, has_pending_open=False,
    )
    result = trigger_matcher.test_match(
        "Thanks for the trust!", triggers, ctx, "XAUUSD",
    )
    assert result.suppressed_by is not None
    assert result.suppressed_by.action_type == "IGNORE"
    assert result.actions == []
    diag = next(d for d in result.layer1 if d.rule.action_type == "IGNORE")
    assert diag.matched
    assert "suppress" in diag.reason.lower()
