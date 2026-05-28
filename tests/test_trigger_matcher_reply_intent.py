"""Reply-intent dispatch in trigger_matcher.

Reply intents are first-class triggers with action_types
TRIGGER_PARENT and IGNORE_PARENT. When the current message is a
Telegram reply, the matcher scans the rules for these meta-types
first:
  - TRIGGER_PARENT hit → re-run the matcher against the PARENT's
    text, emit whatever it produces.
  - IGNORE_PARENT hit  → return [] (matched + suppressed); the
    orchestrator skips the AI fallback too.
  - Neither             → fall through to legacy current-text matching.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from src.db import connect, init_schema
from src import trigger_matcher


@pytest.fixture(autouse=True)
def _isolate_module_state(monkeypatch):
    """Force a fresh cache load per test so a previous test's profile
    doesn't leak through the module-level _cache."""
    trigger_matcher._cache = None
    yield
    trigger_matcher._cache = None


def _write_profile(
    tmp_path: Path,
    *,
    triggers: list[dict],
    symbol: str = "XAUUSD",
) -> Path:
    p = tmp_path / "profile.json"
    p.write_text(json.dumps({
        "symbol": symbol,
        "triggers": triggers,
    }), encoding="utf-8")
    return p


def _setup(tmp_path: Path) -> sqlite3.Connection:
    conn = connect(str(tmp_path / "db.sqlite"))
    init_schema(conn)
    return conn


def test_trigger_parent_recurses_on_parent(tmp_path, monkeypatch):
    """A TRIGGER_PARENT meta-rule matches the current reply → matcher
    re-runs against the parent's text and emits whatever the parent's
    own trigger produces."""
    profile = _write_profile(
        tmp_path,
        triggers=[
            {
                "phrase": "yes do it",
                "action_type": "TRIGGER_PARENT",
                "samples": ["yes do it"],
                "context_tokens": [],
            },
            {
                "phrase": "out",
                "action_type": "CLOSE_FULL",
                "samples": ["خرجنا من الصفقة"],
                "context_tokens": [],
            },
        ],
    )
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 99, 'XAUUSD', 'BUY', 0.1, 4500, 4490, 4520, 'open')"
    )
    result = trigger_matcher.match(
        "yes do it", conn, parent_text="خرجنا من الصفقة",
    )
    assert result is not None
    assert len(result) == 1
    assert result[0].type == "CLOSE_FULL"


def test_ignore_parent_returns_empty_list(tmp_path, monkeypatch):
    """IGNORE_PARENT match returns [] — orchestrator skips AI too."""
    profile = _write_profile(
        tmp_path,
        triggers=[
            {
                "phrase": "cancel that",
                "action_type": "IGNORE_PARENT",
                "samples": ["cancel that"],
                "context_tokens": [],
            },
            {
                "phrase": "out",
                "action_type": "CLOSE_FULL",
                "samples": ["خرجنا"],
                "context_tokens": [],
            },
        ],
    )
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    result = trigger_matcher.match(
        "cancel that", conn, parent_text="خرجنا",
    )
    assert result == []


def test_trigger_parent_wins_over_ignore_when_both_match(tmp_path, monkeypatch):
    """If both meta-types could match (operator typo), TRIGGER_PARENT
    wins — system errs on executing the parent rather than silently
    dropping a real signal."""
    profile = _write_profile(
        tmp_path,
        triggers=[
            {
                "phrase": "do it",
                "action_type": "TRIGGER_PARENT",
                "samples": ["do it"],
                "context_tokens": [],
            },
            {
                "phrase": "do it",  # same phrase, both action_types
                "action_type": "IGNORE_PARENT",
                "samples": ["do it"],
                "context_tokens": [],
            },
            {
                "phrase": "x",
                "action_type": "CLOSE_FULL",
                "samples": ["خرجنا"],
                "context_tokens": [],
            },
        ],
    )
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 99, 'XAUUSD', 'BUY', 0.1, 4500, 4490, 4520, 'open')"
    )
    result = trigger_matcher.match(
        "do it", conn, parent_text="خرجنا",
    )
    assert result is not None
    assert result[0].type == "CLOSE_FULL"


def test_neither_meta_falls_through_to_current_match(tmp_path, monkeypatch):
    """Reply with no meta-rule hits → matcher tries the reply's own
    text against regular triggers; preserves legacy behaviour."""
    profile = _write_profile(
        tmp_path,
        triggers=[
            {
                "phrase": "active",
                "action_type": "TRIGGER_PARENT",
                "samples": ["active"],
                "context_tokens": [],
            },
            {
                "phrase": "use BE",
                "action_type": "MOVE_SL_BE",
                "samples": ["use BE on this one"],
                "context_tokens": [],
            },
        ],
    )
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    conn.execute(
        "INSERT INTO actions(action_type, payload_json) VALUES('OPEN','{}')"
    )
    conn.execute(
        "INSERT INTO positions(action_id, mt5_ticket, symbol, side, volume, "
        "entry_price, sl, tp, status) "
        "VALUES(1, 99, 'XAUUSD', 'BUY', 0.1, 4500, 4490, 4520, 'open')"
    )
    result = trigger_matcher.match(
        "use BE on this one",
        conn,
        parent_text="some unrelated parent text",
    )
    assert result is not None
    assert len(result) == 1
    assert result[0].type == "MOVE_SL_BE"


def test_trigger_parent_when_parent_has_no_match_returns_none(
    tmp_path, monkeypatch,
):
    """Activate fires but parent doesn't match any trigger → None
    (orchestrator falls through to AI which has REPLY CONTEXT)."""
    profile = _write_profile(
        tmp_path,
        triggers=[
            {
                "phrase": "active",
                "action_type": "TRIGGER_PARENT",
                "samples": ["active"],
                "context_tokens": [],
            },
            {
                "phrase": "out",
                "action_type": "CLOSE_FULL",
                "samples": ["خرجنا"],
                "context_tokens": [],
            },
        ],
    )
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    result = trigger_matcher.match(
        "active", conn, parent_text="completely unrelated text",
    )
    assert result is None


def test_no_parent_text_ignores_meta_rules(tmp_path, monkeypatch):
    """Non-reply messages must NOT match TRIGGER_PARENT / IGNORE_PARENT
    rules even if their phrases appear in the text — these are
    dispatch signals only when parent_text is provided."""
    profile = _write_profile(
        tmp_path,
        triggers=[{
            "phrase": "yes do it",
            "action_type": "TRIGGER_PARENT",
            "samples": ["yes do it"],
            "context_tokens": [],
        }],
    )
    monkeypatch.setattr(trigger_matcher, "_resolve_profile_path",
                        lambda: profile)
    conn = _setup(tmp_path)
    # Non-reply message: meta rules shouldn't fire.
    result = trigger_matcher.match("yes do it", conn)
    assert result is None


def test_legacy_reply_intent_keys_migrate_to_triggers(tmp_path):
    """Old profiles with reply_intent_activate / reply_intent_ignore
    lists must be converted to triggers rows on load — single shot,
    idempotent. Operator never sees the legacy keys again."""
    from src.gui.services.profile_io import _migrate_reply_intent_keys
    data = {
        "triggers": [],
        "reply_intent_activate": ["active", "still valid"],
        "reply_intent_ignore": ["skip this"],
    }
    _migrate_reply_intent_keys(data)
    assert "reply_intent_activate" not in data
    assert "reply_intent_ignore" not in data
    types = [(t["action_type"], t["phrase"]) for t in data["triggers"]]
    assert ("TRIGGER_PARENT", "active") in types
    assert ("TRIGGER_PARENT", "still valid") in types
    assert ("IGNORE_PARENT", "skip this") in types
