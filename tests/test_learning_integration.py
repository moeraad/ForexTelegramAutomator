"""End-to-end: capture a realistic mixed corpus, run the loop, assert the
right suggestions appear with safe evidence — no live API."""
from __future__ import annotations

from src import learning_store, suggestion_store
from src.bot_loops.learning_loop import run_channel_once
from src.channel_learner import LearnParams
from src.db import connect, init_schema


PARAMS = LearnParams(min_cluster_size=3, embed_threshold=0.9,
                     min_support=3, min_purity=0.9)


def _embed(texts):
    out = []
    for t in texts:
        if "مبروك" in t:
            out.append([1.0, 0.0, 0.0])
        elif "احجز" in t:
            out.append([0.0, 1.0, 0.0])
        else:
            out.append([0.0, 0.0, 1.0])
    return out


def _synth(texts, category, action_types_csv):
    return {"phrase": texts[0], "rationale": "cluster"}


def test_loop_proposes_safe_noise_and_action_trigger(tmp_path):
    conn = connect(str(tmp_path / "s.db")); init_schema(conn)
    for i in range(5):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=i, text=f"مبروك على الارباح اليوم {i}",
                               category="ignore", action_types="",
                               triage_decision="keep")
    for i in range(4):
        learning_store.capture(conn, source_channel_id="c", route_id="",
                               source_msg_id=100 + i, text=f"احجز نصف ارباحك {i}",
                               category="signal", action_types="CLOSE_PARTIAL",
                               triage_decision="keep")
    run_channel_once(conn, "c", embed_fn=_embed, synth_fn=_synth,
                     params=PARAMS, corpus_max=2000)
    sugg = suggestion_store.list_proposed(conn, "c")

    noise = [s for s in sugg if s.rule_kind == "noise"]
    assert noise and noise[0].evidence["false_suppression_count"] == 0
    assert noise[0].evidence["one_tap_safe"] is True

    trig = [s for s in sugg if s.rule_kind == "action_trigger"]
    assert trig and trig[0].payload["action_types"] == ["CLOSE_PARTIAL"]
    assert trig[0].evidence["purity"] == 1.0
    assert trig[0].evidence["one_tap_safe"] is True
