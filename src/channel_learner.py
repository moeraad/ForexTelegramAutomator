"""Batch learner: cluster a channel's labeled corpus and propose cheap-layer
rules with replay evidence. Pure logic — no DB, no network. The bot loop
supplies embeddings (already on each Sample) and a synth_fn callback, and
persists the returned drafts via suggestion_store.

Generalization = greedy cosine clustering within a category group, then one
LLM synthesis per dense cluster, then a replay of the candidate rule over the
whole corpus to compute evidence (the accuracy guarantee).
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from src.trigger_matcher import _cosine

# Categories that mean "no trade-relevant action" — candidates for suppression.
_NOISE_CATEGORIES = frozenset({"ignore"})
# Action types the matcher can deterministically emit (mirror of
# trigger_matcher._ALLOWED_ACTION_TYPES minus reply-intent meta types).
_TRIGGERABLE = frozenset({
    "CLOSE_FULL", "MOVE_SL_BE", "CLOSE_PARTIAL", "REOPEN_LAST",
    "REINFORCE", "TIGHTEN_SL", "CANCEL_PENDING",
})
# Verdicts that mean "a real trade would have been missed" if suppressed.
_SIGNAL_CATEGORIES = frozenset({"signal", "partial_signal"})


@dataclass(frozen=True)
class LearnParams:
    min_cluster_size: int
    embed_threshold: float
    min_support: int
    min_purity: float


@dataclass(frozen=True)
class SuggestionDraft:
    rule_kind: str
    target_layer: str
    payload: dict
    evidence: dict


def _greedy_clusters(samples: list, threshold: float) -> list[list]:
    """Greedy single-pass clustering by cosine vs each cluster's first member.
    Returns list[list[Sample]]. Samples without embeddings are skipped."""
    clusters: list[list] = []
    for s in samples:
        if not s.embedding:
            continue
        placed = False
        for c in clusters:
            if _cosine(s.embedding, c[0].embedding) >= threshold:
                c.append(s)
                placed = True
                break
        if not placed:
            clusters.append([s])
    return clusters


def _suppression_evidence(cluster: list, all_samples: list, threshold: float) -> dict:
    """Replay: how many corpus messages does this cluster's centroid match,
    and how many of those were actually trade-relevant (false suppression)."""
    centroid = cluster[0].embedding
    would_suppress = 0
    false_suppression = 0
    breakdown: Counter = Counter()
    for s in all_samples:
        if not s.embedding:
            continue
        if _cosine(s.embedding, centroid) >= threshold:
            would_suppress += 1
            breakdown[s.category] += 1
            if s.category in _SIGNAL_CATEGORIES or s.action_types:
                false_suppression += 1
    return {
        "would_suppress": would_suppress,
        "false_suppression_count": false_suppression,
        "verdict_breakdown": dict(breakdown),
        "support": sum(s.seen_count for s in cluster),
    }


def learn(samples, *, synth_fn, params: LearnParams) -> list[SuggestionDraft]:
    drafts: list[SuggestionDraft] = []
    embedded = [s for s in samples if s.embedding]
    if not embedded:
        return drafts

    noise = [s for s in embedded if s.category in _NOISE_CATEGORIES]
    signal = [s for s in embedded if s.category in _SIGNAL_CATEGORIES]
    context = [s for s in embedded if s.category == "context"]

    # --- Suppression (noise) rules ---
    for cluster in _greedy_clusters(noise, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        ev = _suppression_evidence(cluster, embedded, params.embed_threshold)
        syn = synth_fn([s.text for s in cluster], "ignore", "")
        drafts.append(SuggestionDraft(
            rule_kind="noise", target_layer="triage",
            payload={"phrase": syn["phrase"],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={**ev, "rationale": syn.get("rationale", ""),
                      "one_tap_safe": ev["false_suppression_count"] == 0
                      and ev["support"] >= params.min_support},
        ))

    # --- Deterministic action triggers ---
    triggerable = [s for s in signal
                   if any(t in _TRIGGERABLE for t in s.action_types.split(",") if t)]
    for cluster in _greedy_clusters(triggerable, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        types = Counter()
        for s in cluster:
            for t in s.action_types.split(","):
                if t in _TRIGGERABLE:
                    types[t] += 1
        if not types:
            continue
        top_type, top_n = types.most_common(1)[0]
        purity = top_n / len(cluster)
        conflicts = {t: n for t, n in types.items() if t != top_type}
        syn = synth_fn([s.text for s in cluster], "signal", top_type)
        drafts.append(SuggestionDraft(
            rule_kind="action_trigger", target_layer="matcher",
            payload={"phrase": syn["phrase"], "action_types": [top_type],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={"support": sum(s.seen_count for s in cluster),
                      "purity": purity, "conflicts": conflicts,
                      "rationale": syn.get("rationale", ""),
                      "one_tap_safe": purity >= params.min_purity
                      and not conflicts
                      and sum(s.seen_count for s in cluster) >= params.min_support},
        ))

    # --- Triage keep-triggers (accuracy gain): signals triage marked ignore ---
    # v1 LIMITATION: this branch is correct but its data precondition is rarely
    # met in healthy operation. Capture runs only on the interpret path, and a
    # triage="ignore" message returns BEFORE the interpreter — so a sample with
    # triage_decision="ignore" is only recorded when triage itself errored and
    # fell through to Sonnet. Detecting real triage false-negatives needs triage
    # shadow-sampling (occasionally interpreting triage-dropped messages), which
    # is a v2 fast-follow. The branch stays so it lights up automatically once
    # that lands; until then triage_misses is normally empty.
    triage_misses = [s for s in signal if s.triage_decision == "ignore"]
    for cluster in _greedy_clusters(triage_misses, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        syn = synth_fn([s.text for s in cluster], "signal", "")
        drafts.append(SuggestionDraft(
            rule_kind="keep_trigger", target_layer="triage",
            payload={"phrase": syn["phrase"],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={"support": sum(s.seen_count for s in cluster),
                      "triage_false_negatives": len(cluster),
                      "rationale": syn.get("rationale", ""),
                      "one_tap_safe": True},
        ))

    # --- Context-drop tuning (lower-risk cost saving) ---
    for cluster in _greedy_clusters(context, params.embed_threshold):
        if len(cluster) < params.min_cluster_size:
            continue
        ev = _suppression_evidence(cluster, embedded, params.embed_threshold)
        syn = synth_fn([s.text for s in cluster], "context", "")
        drafts.append(SuggestionDraft(
            rule_kind="context_drop", target_layer="triage",
            payload={"phrase": syn["phrase"],
                     "samples": [s.text for s in cluster[:5]]},
            evidence={**ev, "rationale": syn.get("rationale", ""),
                      "one_tap_safe": ev["false_suppression_count"] == 0
                      and ev["support"] >= params.min_support},
        ))

    return drafts
