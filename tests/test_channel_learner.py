from __future__ import annotations

from src import channel_learner as cl
from src.learning_store import Sample


def _s(i, text, category, action_types, vec, triage="keep", seen=1):
    return Sample(id=i, source_channel_id="c", text=text, norm_text=text,
                  category=category, action_types=action_types,
                  triage_decision=triage, seen_count=seen, embedding=vec)


PARAMS = cl.LearnParams(min_cluster_size=3, embed_threshold=0.9,
                        min_support=3, min_purity=0.9)


def _synth(texts, category, action_types_csv):
    return {"phrase": texts[0], "rationale": f"{len(texts)} like this"}


def test_noise_cluster_with_zero_signal_is_clean(tmp_path):
    samples = [_s(i, "مبروك على الارباح", "ignore", "", [1.0, 0.0]) for i in range(4)]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    noise = [d for d in drafts if d.rule_kind == "noise"]
    assert len(noise) == 1
    ev = noise[0].evidence
    assert ev["would_suppress"] >= 4
    assert ev["false_suppression_count"] == 0
    assert noise[0].target_layer == "triage"


def test_noise_cluster_flags_false_suppression(tmp_path):
    samples = [_s(i, "x", "ignore", "", [1.0, 0.0]) for i in range(3)]
    samples.append(_s(99, "x signal", "signal", "OPEN", [1.0, 0.0]))
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    noise = [d for d in drafts if d.rule_kind == "noise"]
    assert len(noise) == 1
    assert noise[0].evidence["false_suppression_count"] == 1


def test_action_trigger_cluster(tmp_path):
    samples = [_s(i, "احجز نصف", "signal", "CLOSE_PARTIAL", [0.0, 1.0])
               for i in range(3)]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    trig = [d for d in drafts if d.rule_kind == "action_trigger"]
    assert len(trig) == 1
    assert trig[0].payload["action_types"] == ["CLOSE_PARTIAL"]
    assert trig[0].evidence["purity"] == 1.0
    assert trig[0].target_layer == "matcher"


def test_impure_action_cluster_is_blocked_not_silent(tmp_path):
    samples = [_s(0, "x", "signal", "CLOSE_PARTIAL", [0.0, 1.0]),
               _s(1, "x", "signal", "CLOSE_PARTIAL", [0.0, 1.0]),
               _s(2, "x", "signal", "MOVE_SL_BE", [0.0, 1.0])]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    trig = [d for d in drafts if d.rule_kind == "action_trigger"]
    assert len(trig) == 1
    assert trig[0].evidence["purity"] < 0.9
    assert trig[0].evidence["conflicts"]


def test_keep_trigger_from_triage_false_negative(tmp_path):
    samples = [_s(i, "ادخل شراء الان", "signal", "OPEN_INSTANT", [0.5, 0.5],
                  triage="ignore") for i in range(3)]
    drafts = cl.learn(samples, synth_fn=_synth, params=PARAMS)
    keep = [d for d in drafts if d.rule_kind == "keep_trigger"]
    assert len(keep) == 1
    assert keep[0].target_layer == "triage"


def test_small_cluster_below_min_is_ignored(tmp_path):
    samples = [_s(0, "x", "ignore", "", [1.0, 0.0]),
               _s(1, "x", "ignore", "", [1.0, 0.0])]
    assert cl.learn(samples, synth_fn=_synth, params=PARAMS) == []
