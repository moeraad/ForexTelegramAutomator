"""Rolling per-channel learning scheduler for the Channel Learning Loop.

Once a channel accumulates `learning_batch_n` new samples, embed the uncached
ones, run channel_learner, and persist suggestions. Runs inside the bot
process alongside the other sweepers. A crash here can never affect trading —
the loop body is fully wrapped.

`run_channel_once` is the testable per-channel core; `learning_loop` is the
asyncio wrapper that polls all channels.
"""
from __future__ import annotations

import asyncio
import logging

from telegram.ext import Application

from src import channel_learner, learning_store, suggestion_store
from src.channel_learner import LearnParams

log = logging.getLogger("bot")

_POLL_SEC = 120.0
_EMBED_BATCH_CAP = 200


def run_channel_once(conn, channel_id, *, embed_fn, synth_fn, params: LearnParams,
                     corpus_max: int) -> int:
    """Embed uncached samples for one channel, learn, persist suggestions.
    Returns number of NEW suggestions written. Pure of asyncio + provider
    construction (both injected) so it unit-tests with fakes."""
    pending = learning_store.without_embedding(conn, channel_id, _EMBED_BATCH_CAP)
    if pending:
        vecs = embed_fn([s.text for s in pending])
        if vecs and len(vecs) == len(pending):
            for s, v in zip(pending, vecs):
                learning_store.set_embedding(conn, s.id, v)

    samples = learning_store.recent(conn, channel_id, corpus_max)
    drafts = channel_learner.learn(samples, synth_fn=synth_fn, params=params)

    written = 0
    for d in drafts:
        sid = suggestion_store.add(
            conn, source_channel_id=channel_id, rule_kind=d.rule_kind,
            target_layer=d.target_layer, payload=d.payload, evidence=d.evidence)
        if sid is not None:
            written += 1

    learning_store.prune(conn, channel_id, corpus_max)
    return written


def _build_embed_fn(db_path):
    """OpenAI embedding callback reusing trigger_matcher's batched call."""
    from pathlib import Path
    from src.trigger_matcher import _embed_batch

    def embed_fn(texts):
        return _embed_batch(texts, db_path=Path(db_path))
    return embed_fn


def _build_synth_fn(db_path):
    """Cheap-LLM synthesis callback via the triage provider (gpt-5-nano/Haiku)."""
    import json
    from src.llm_provider import build_triage_provider

    provider = build_triage_provider(model="")
    system = (
        "You summarize a CLUSTER of similar trading-channel messages into ONE "
        "short, distinctive phrase that characterizes them. Output strict JSON: "
        '{\"phrase\": \"<short distinctive phrase>\", \"rationale\": \"<one line>\"}. '
        "No code fences."
    )

    def synth_fn(texts, category, action_types_csv):
        sample = "\n".join(f"- {t}" for t in texts[:8])
        user = (f"category={category} action_types={action_types_csv}\n"
                f"messages:\n{sample}")
        try:
            res = provider.triage(system_prompt=system, user_content=user,
                                  max_output_tokens=120)
            obj = json.loads(res.raw_text.strip().strip("`"))
            phrase = str(obj.get("phrase") or texts[0])
            return {"phrase": phrase, "rationale": str(obj.get("rationale") or "")}
        except Exception as e:  # noqa: BLE001 — fall back to representative text
            log.warning("CLL synth failed: %s", e)
            return {"phrase": texts[0], "rationale": ""}
    return synth_fn


async def learning_loop(app: Application):
    """Poll channels; when one crosses learning_batch_n new samples, learn.
    Silent on idle ticks. Fully wrapped — never breaks the bot."""
    import sqlite3
    from pathlib import Path
    from src import config, db_settings

    conn: sqlite3.Connection = app.bot_data["conn"]
    db_path = Path(config.DB_PATH)
    last_seen: dict[str, int] = {}
    embed_fn = _build_embed_fn(str(db_path))
    synth_fn = _build_synth_fn(str(db_path))

    while True:
        try:
            if not db_settings.get_bool(db_path, "learning_enabled", True):
                await asyncio.sleep(_POLL_SEC)
                continue
            batch_n = db_settings.get_int(db_path, "learning_batch_n", 50)
            corpus_max = db_settings.get_int(db_path, "learning_corpus_max", 2000)
            ttl_days = db_settings.get_int(db_path, "learning_suggestion_ttl_days", 14)
            params = LearnParams(
                min_cluster_size=db_settings.get_int(db_path, "learning_min_cluster_size", 4),
                embed_threshold=db_settings.get_float(db_path, "learning_embed_threshold", 0.82),
                min_support=db_settings.get_int(db_path, "learning_min_support", 5),
                min_purity=db_settings.get_float(db_path, "learning_min_purity", 0.9),
            )
            suggestion_store.expire_old(conn, ttl_days)

            channels = [r["source_channel_id"] for r in conn.execute(
                "SELECT DISTINCT source_channel_id FROM learning_samples"
            ).fetchall()]
            for ch in channels:
                cur_max = learning_store.max_id(conn, ch)
                seen = last_seen.get(ch)
                if seen is None:
                    last_seen[ch] = cur_max
                    continue
                if learning_store.count_since(conn, ch, seen) < batch_n:
                    continue
                last_seen[ch] = cur_max
                try:
                    n = run_channel_once(conn, ch, embed_fn=embed_fn,
                                         synth_fn=synth_fn, params=params,
                                         corpus_max=corpus_max)
                    if n:
                        log.info("CLL: %d new suggestions for channel %s", n, ch)
                        from src.notify import notify_owner
                        notify_owner(
                            f"💡 {n} new classification suggestion(s) for "
                            f"{ch}. Review in the Suggestions tab.")
                except Exception:  # noqa: BLE001
                    log.exception("CLL run_channel_once failed for %s", ch)
        except Exception as e:  # noqa: BLE001
            log.exception("learning_loop error: %s", e)
        await asyncio.sleep(_POLL_SEC)
