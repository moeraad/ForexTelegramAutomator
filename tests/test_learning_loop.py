from __future__ import annotations

from src import learning_store, suggestion_store
from src.bot_loops.learning_loop import run_channel_once
from src.channel_learner import LearnParams
from src.db import connect, init_schema


PARAMS = LearnParams(min_cluster_size=3, embed_threshold=0.9,
                     min_support=3, min_purity=0.9)


def _fake_embed(texts):
    return [[1.0, 0.0] if "مبروك" in t else [0.0, 1.0] for t in texts]


def _fake_synth(texts, category, action_types_csv):
    return {"phrase": texts[0], "rationale": "x"}


def test_run_channel_once_embeds_and_proposes(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    for i in range(4):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text=f"مبروك على الارباح {i}",
                               category="ignore", action_types="",
                               triage_decision="keep")
    n = run_channel_once(conn, "c", embed_fn=_fake_embed, synth_fn=_fake_synth,
                         params=PARAMS, corpus_max=2000)
    assert n >= 1
    sugg = suggestion_store.list_proposed(conn, "c")
    assert any(s.rule_kind == "noise" for s in sugg)
    assert learning_store.without_embedding(conn, "c", 100) == []


def test_run_channel_once_is_idempotent_on_suggestions(tmp_path):
    conn = connect(str(tmp_path / "s.db"))
    init_schema(conn)
    for i in range(4):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text=f"مبروك على الارباح {i}",
                               category="ignore", action_types="",
                               triage_decision="keep")
    run_channel_once(conn, "c", embed_fn=_fake_embed, synth_fn=_fake_synth,
                     params=PARAMS, corpus_max=2000)
    before = len(suggestion_store.list_proposed(conn, "c"))
    run_channel_once(conn, "c", embed_fn=_fake_embed, synth_fn=_fake_synth,
                     params=PARAMS, corpus_max=2000)
    after = len(suggestion_store.list_proposed(conn, "c"))
    assert before == after
