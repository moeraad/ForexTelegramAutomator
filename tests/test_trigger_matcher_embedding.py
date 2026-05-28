"""Layer 2 (embedding) robustness + observability.

Covers the two fixes:
  1. A populate failure must NOT permanently disable Layer 2 — it retries
     after a cooldown instead of treating one blip as a process-lifetime
     kill switch.
  2. An embeddings/rules length mismatch must be skipped defensively, never
     raise (a raise would be swallowed by the orchestrator, silently killing
     Layer 2 on every message).
Plus the no-hit observability log that distinguishes "ran, nothing cleared
the bar" from "never ran".
"""
from __future__ import annotations

import logging
from pathlib import Path

from src import trigger_matcher


def _rule() -> trigger_matcher.TriggerRule:
    return trigger_matcher.TriggerRule(
        action_type="CLOSE_FULL", phrase="close", norm_phrase="close",
        norm_context_tokens=(), match_text="close the trade",
        norm_match_text="close the trade", source="sample",
    )


def _cache() -> trigger_matcher._ProfileCache:
    return trigger_matcher._ProfileCache(
        path=Path("x"), mtime=0.0, rules=[_rule()], symbol="XAUUSD",
        embeddings=None,
    )


def _ctx() -> trigger_matcher.MatchContext:
    # CLOSE_FULL precondition requires an open position.
    return trigger_matcher.MatchContext(
        has_open_position=True, open_position_side="BUY",
        has_closed_within_24h=False, has_pending_open=False,
    )


def test_populate_failure_retries_after_cooldown_not_permanent(monkeypatch):
    # Pin the cooldown so the "within cooldown" assertion can't depend on the
    # env default (COPYTRADES_EMBED_RETRY_COOLDOWN_SEC) during test runs.
    monkeypatch.setattr(trigger_matcher, "EMBED_RETRY_COOLDOWN_SEC", 9999.0)
    cache = _cache()
    ctx = _ctx()
    calls = {"n": 0}

    def failing(inputs, db_path=None):
        calls["n"] += 1
        return None  # endpoint down

    monkeypatch.setattr(trigger_matcher, "_embed_batch", failing)

    # 1) First attempt: populate fails → None, embeddings stays None (NOT []),
    #    cooldown armed.
    assert trigger_matcher._layer2_embedding("close now", cache, ctx) is None
    assert cache.embeddings is None
    assert cache.embeddings_retry_after > 0.0
    n_after_first = calls["n"]

    # 2) Within cooldown: must skip without hitting the endpoint again.
    assert trigger_matcher._layer2_embedding("close now", cache, ctx) is None
    assert calls["n"] == n_after_first  # no per-message hammering

    # 3) Cooldown elapsed + endpoint recovered → Layer 2 heals itself.
    cache.embeddings_retry_after = 0.0

    def healthy(inputs, db_path=None):
        calls["n"] += 1
        return [[1.0, 0.0] for _ in inputs]

    monkeypatch.setattr(trigger_matcher, "_embed_batch", healthy)
    hit = trigger_matcher._layer2_embedding("close now", cache, ctx)
    assert cache.embeddings is not None          # rebuilt
    assert hit is not None                        # and now matches
    assert hit[0].action_type == "CLOSE_FULL"


def test_length_mismatch_is_skipped_without_raising(monkeypatch):
    cache = _cache()
    cache.embeddings = []          # desynced: 0 vectors, 1 rule
    monkeypatch.setattr(trigger_matcher, "_embed_batch",
                        lambda *a, **k: [[1.0, 0.0]])
    # Old code did zip(..., strict=True) → ValueError here. Must be graceful.
    assert trigger_matcher._layer2_embedding("x", cache, _ctx()) is None
    assert cache.embeddings is None  # reset so the next call rebuilds


def test_no_hit_logs_best_score(monkeypatch, caplog):
    cache = _cache()

    def embed(inputs, db_path=None):
        # Rule sample → [1,0]; the message → [1,1] (cosine ≈ 0.707 < 0.78).
        return [([1.0, 1.0] if s == "message text" else [1.0, 0.0])
                for s in inputs]

    monkeypatch.setattr(trigger_matcher, "_embed_batch", embed)
    # Per-message tuning detail is DEBUG (INFO would spam the live log).
    with caplog.at_level(logging.DEBUG, logger=trigger_matcher.log.name):
        result = trigger_matcher._layer2_embedding("message text", cache, _ctx())
    assert result is None
    assert "Layer 2 no hit" in caplog.text
    assert "scored=1" in caplog.text


def test_populate_success_logs_dimensions(monkeypatch, caplog):
    cache = _cache()
    monkeypatch.setattr(trigger_matcher, "_embed_batch",
                        lambda *a, **k: [[1.0, 0.0]])
    with caplog.at_level(logging.INFO, logger=trigger_matcher.log.name):
        trigger_matcher._populate_embeddings(cache)
    assert "embeddings built" in caplog.text
    assert "rules=1" in caplog.text
